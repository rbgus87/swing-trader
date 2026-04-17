"""Phase 1 Step 4a — 데이터 정합성 최종 검증.

Phase 2 진입 전 데이터 품질 확인. DB 수정 없음 (읽기 전용).
"""
from datetime import date
import json

import pandas as pd
from loguru import logger

from src.data_pipeline.db import get_connection


def check_cross_table_consistency():
    """검증 1: 테이블 간 정합성."""
    with get_connection() as conn:
        results = {}

        # 1-1. stocks에 있는데 daily_candles에 없는 ticker
        cursor = conn.execute("""
            SELECT s.ticker, s.name, s.stock_type, s.delisted_date
            FROM stocks s
            LEFT JOIN (SELECT DISTINCT ticker FROM daily_candles) c ON s.ticker = c.ticker
            WHERE c.ticker IS NULL
        """)
        no_candles = cursor.fetchall()
        results['stocks_without_candles'] = len(no_candles)
        results['stocks_without_candles_samples'] = [
            dict(row) for row in no_candles[:10]
        ]

        # 1-2. daily_candles에 있는데 stocks에 없는 ticker
        cursor = conn.execute("""
            SELECT DISTINCT c.ticker
            FROM daily_candles c
            LEFT JOIN stocks s ON c.ticker = s.ticker
            WHERE s.ticker IS NULL
        """)
        orphan_candles = cursor.fetchall()
        results['candles_without_stocks'] = len(orphan_candles)

        # 1-3. daily_candles 범위 vs stocks listed/delisted 정합
        cursor = conn.execute("""
            SELECT s.ticker, s.name,
                   s.listed_date, s.delisted_date,
                   MIN(c.date) as first_candle,
                   MAX(c.date) as last_candle
            FROM stocks s
            JOIN daily_candles c ON s.ticker = c.ticker
            GROUP BY s.ticker
            HAVING (s.listed_date IS NOT NULL AND first_candle < s.listed_date)
                OR (s.delisted_date IS NOT NULL AND last_candle > s.delisted_date)
        """)
        range_mismatch = cursor.fetchall()
        results['date_range_mismatch'] = len(range_mismatch)
        results['date_range_mismatch_samples'] = [
            dict(row) for row in range_mismatch[:10]
        ]

        # 1-4. market_cap_history coverage vs daily_candles
        cursor = conn.execute("""
            SELECT COUNT(DISTINCT date) as candle_dates FROM daily_candles
        """)
        candle_dates = cursor.fetchone()[0]

        cursor = conn.execute("""
            SELECT COUNT(DISTINCT date) as mcap_dates FROM market_cap_history
        """)
        mcap_dates = cursor.fetchone()[0]
        results['candle_unique_dates'] = candle_dates
        results['mcap_unique_dates'] = mcap_dates

    return results


def check_adjusted_price_continuity():
    """검증 2: 수정주가 연속성 — 분할 케이스 3개 확인."""

    SPLIT_CASES = [
        {
            'ticker': '005930', 'name': '삼성전자',
            'split_date': '2018-05-04', 'ratio': 50,
            'check_range': ('2018-04-25', '2018-05-10'),
        },
        {
            'ticker': '035420', 'name': 'NAVER',
            'split_date': '2018-10-12', 'ratio': 5,
            'check_range': ('2018-10-08', '2018-10-19'),
        },
        {
            'ticker': '035720', 'name': '카카오',
            'split_date': '2021-04-15', 'ratio': 5,
            'check_range': ('2021-04-12', '2021-04-20'),
        },
    ]

    results = []
    with get_connection() as conn:
        for case in SPLIT_CASES:
            cursor = conn.execute("""
                SELECT date, open, high, low, close, volume
                FROM daily_candles
                WHERE ticker = ? AND date BETWEEN ? AND ?
                ORDER BY date
            """, (case['ticker'], case['check_range'][0], case['check_range'][1]))
            rows = cursor.fetchall()

            prices = [row['close'] for row in rows if row['close'] > 0]
            max_daily_change = 0
            for i in range(1, len(prices)):
                change = abs(prices[i] / prices[i-1] - 1)
                max_daily_change = max(max_daily_change, change)

            is_adjusted = max_daily_change < 0.35

            results.append({
                'ticker': case['ticker'],
                'name': case['name'],
                'split_ratio': case['ratio'],
                'max_daily_change': f"{max_daily_change:.2%}",
                'is_adjusted': is_adjusted,
                'data_points': len(rows),
            })

    return results


def check_universe_pool_simulation():
    """검증 3: Universe Pool 필터 시뮬레이션."""
    TEST_DATE = '2020-01-02'
    MCAP_THRESHOLD = 5_000_000_000_000      # 5조원
    TRADING_VALUE_THRESHOLD = 10_000_000_000  # 100억원

    results = {}
    with get_connection() as conn:
        cursor = conn.execute("""
            SELECT m.ticker, s.name, s.stock_type, s.market,
                   m.market_cap
            FROM market_cap_history m
            JOIN stocks s ON m.ticker = s.ticker
            WHERE m.date = ?
              AND m.market_cap >= ?
              AND s.stock_type NOT IN ('SPAC', 'REIT', 'FOREIGN')
              AND s.ticker NOT IN (
                  SELECT ticker FROM stock_status_events
                  WHERE event_type = 'ADMIN_DESIGNATED'
                    AND start_date <= ?
                    AND (end_date IS NULL OR end_date > ?)
              )
            ORDER BY m.market_cap DESC
        """, (TEST_DATE, MCAP_THRESHOLD, TEST_DATE, TEST_DATE))

        mcap_filtered = cursor.fetchall()
        results['mcap_filtered_count'] = len(mcap_filtered)
        results['mcap_filtered_top10'] = [
            {'ticker': row['ticker'], 'name': row['name'],
             'mcap_trillion': round(row['market_cap'] / 1e12, 1)}
            for row in mcap_filtered[:10]
        ]

        if mcap_filtered:
            cursor = conn.execute(f"""
                SELECT c.ticker,
                       AVG(c.close * c.volume) as avg_trading_value
                FROM daily_candles c
                WHERE c.date <= ?
                  AND c.date >= date(?, '-30 day')
                  AND c.ticker IN ({','.join('?' * len(mcap_filtered))})
                GROUP BY c.ticker
                HAVING avg_trading_value >= ?
            """, (TEST_DATE, TEST_DATE,
                  *[r['ticker'] for r in mcap_filtered],
                  TRADING_VALUE_THRESHOLD))

            value_filtered = cursor.fetchall()
        else:
            value_filtered = []

        results['value_filtered_count'] = len(value_filtered)
        results['final_universe_size'] = len(value_filtered)

    return results


def check_indicator_calculation():
    """검증 4: 핵심 지표 계산 테스트."""
    TEST_TICKERS = ['005930', '000660', '035420', '051910', '068270']

    results = []
    with get_connection() as conn:
        for ticker in TEST_TICKERS:
            cursor = conn.execute("""
                SELECT date, open, high, low, close, volume
                FROM daily_candles
                WHERE ticker = ?
                ORDER BY date DESC
                LIMIT 100
            """, (ticker,))
            rows = cursor.fetchall()

            if len(rows) < 20:
                results.append({
                    'ticker': ticker,
                    'status': 'INSUFFICIENT_DATA',
                    'rows': len(rows),
                })
                continue

            df = pd.DataFrame([dict(r) for r in rows])
            df = df.sort_values('date').reset_index(drop=True)

            sma20 = df['close'].rolling(20).mean()
            sma20_valid = sma20.notna().sum()
            sma20_has_inf = (sma20 == float('inf')).any()

            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            rsi_valid = rsi.notna().sum()
            rsi_has_inf = (rsi == float('inf')).any() or (rsi == float('-inf')).any()

            tr = pd.DataFrame({
                'hl': df['high'] - df['low'],
                'hc': (df['high'] - df['close'].shift(1)).abs(),
                'lc': (df['low'] - df['close'].shift(1)).abs(),
            }).max(axis=1)
            atr = tr.rolling(14).mean()
            atr_valid = atr.notna().sum()
            atr_has_inf = (atr == float('inf')).any()
            atr_has_zero = (atr.dropna() == 0).any()

            results.append({
                'ticker': ticker,
                'status': 'OK',
                'rows': len(rows),
                'sma20_valid': int(sma20_valid),
                'rsi_valid': int(rsi_valid),
                'atr_valid': int(atr_valid),
                'has_inf': bool(sma20_has_inf or rsi_has_inf or atr_has_inf),
                'atr_has_zero': bool(atr_has_zero),
                'last_close': float(df['close'].iloc[-1]),
                'last_atr': float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else None,
            })

    return results


def check_anomaly_summary():
    """검증 5: 이상치 종합 분석."""
    results = {}
    with get_connection() as conn:
        cursor = conn.execute("""
            SELECT severity, anomaly_type, COUNT(*) as cnt
            FROM data_anomaly_log
            GROUP BY severity, anomaly_type
            ORDER BY severity DESC, cnt DESC
        """)
        results['distribution'] = [dict(row) for row in cursor.fetchall()]

        cursor = conn.execute("""
            SELECT ticker, date, anomaly_type, details
            FROM data_anomaly_log
            WHERE severity = 'ERROR'
            LIMIT 10
        """)
        results['error_samples'] = [dict(row) for row in cursor.fetchall()]

        cursor = conn.execute("SELECT COUNT(*) FROM data_anomaly_log")
        results['total'] = cursor.fetchone()[0]

    return results


def main():
    logger.info("="*60)
    logger.info("Phase 1 Step 4a — 데이터 정합성 최종 검증")
    logger.info("="*60)

    all_pass = True

    # 검증 1
    logger.info("\n[1/5] 테이블 간 정합성 검증...")
    cross = check_cross_table_consistency()
    logger.info(f"  stocks 중 candles 없는 종목: {cross['stocks_without_candles']}")
    if cross['stocks_without_candles_samples']:
        for s in cross['stocks_without_candles_samples'][:5]:
            logger.info(f"    {s['ticker']} {s['name']} type={s['stock_type']}")
    logger.info(f"  candles 중 stocks 없는 ticker: {cross['candles_without_stocks']}")
    logger.info(f"  날짜 범위 불일치: {cross['date_range_mismatch']}")
    if cross['date_range_mismatch_samples']:
        for s in cross['date_range_mismatch_samples'][:5]:
            logger.info(f"    {s['ticker']} {s['name']}: listed={s['listed_date']}, "
                       f"first_candle={s['first_candle']}, last_candle={s['last_candle']}")
    logger.info(f"  candle 고유 날짜 수: {cross['candle_unique_dates']}")
    logger.info(f"  mcap 고유 날짜 수: {cross['mcap_unique_dates']}")

    if cross['candles_without_stocks'] > 0:
        logger.warning("  ⚠ orphan candles 발견")
        all_pass = False

    # 검증 2
    logger.info("\n[2/5] 수정주가 연속성 검증...")
    splits = check_adjusted_price_continuity()
    for s in splits:
        status = "✓" if s['is_adjusted'] else "✗ FAIL"
        logger.info(f"  {s['ticker']} {s['name']}: 분할 {s['split_ratio']}:1, "
                    f"max_change={s['max_daily_change']}, {status}")
        if not s['is_adjusted']:
            all_pass = False

    # 검증 3
    logger.info("\n[3/5] Universe Pool 필터 시뮬레이션 (2020-01-02)...")
    universe = check_universe_pool_simulation()
    logger.info(f"  시총 5조 이상 (SPAC/REIT/FOREIGN/관리종목 제외): {universe['mcap_filtered_count']}종목")
    logger.info(f"  + 거래대금 100억 이상: {universe['value_filtered_count']}종목")
    logger.info(f"  최종 Universe 크기: {universe['final_universe_size']}종목")
    logger.info(f"  시총 상위 10:")
    for s in universe['mcap_filtered_top10']:
        logger.info(f"    {s['ticker']} {s['name']}: {s['mcap_trillion']}조원")

    if universe['final_universe_size'] < 30 or universe['final_universe_size'] > 300:
        logger.warning(f"  ⚠ Universe 크기가 예상 범위(30~300) 밖")
        all_pass = False

    # 검증 4
    logger.info("\n[4/5] 핵심 지표 계산 테스트...")
    indicators = check_indicator_calculation()
    for ind in indicators:
        if ind['status'] == 'OK':
            logger.info(f"  {ind['ticker']}: SMA20={ind['sma20_valid']}, "
                       f"RSI={ind['rsi_valid']}, ATR={ind['atr_valid']}, "
                       f"inf={ind['has_inf']}, atr_zero={ind['atr_has_zero']}, "
                       f"close={ind['last_close']:,.0f}, atr={ind['last_atr']:,.0f}")
        else:
            logger.warning(f"  {ind['ticker']}: {ind['status']} ({ind['rows']} rows)")
            all_pass = False

        if ind.get('has_inf'):
            logger.warning(f"  ⚠ {ind['ticker']}: inf 값 발견")
            all_pass = False

    # 검증 5
    logger.info("\n[5/5] 이상치 종합 분석...")
    anomaly = check_anomaly_summary()
    logger.info(f"  총 이상치 로그: {anomaly['total']}건")
    logger.info(f"  분포:")
    for d in anomaly['distribution']:
        logger.info(f"    [{d['severity']}] {d['anomaly_type']}: {d['cnt']}")

    if anomaly['error_samples']:
        logger.info(f"  ERROR 등급 샘플:")
        for e in anomaly['error_samples'][:5]:
            logger.info(f"    {e['ticker']} {e['date']}: {e['anomaly_type']}")

    logger.info("\n" + "="*60)
    if all_pass:
        logger.info("✅ 전체 검증 통과. Phase 2 진입 가능.")
    else:
        logger.warning("⚠ 일부 검증 항목에서 경고 발생. 상세 확인 필요.")
    logger.info("="*60)


if __name__ == "__main__":
    main()

"""실험 11: 상대강도 시장별 벤치마크 (KOSPI 통일 vs 시장별 분리).

v2.3은 모든 종목을 KOSPI N일 수익률 대비로 상대강도를 측정한다.
KOSDAQ이 KOSPI보다 약한 구간(현재 +13%p 스프레드)에서 KOSDAQ 종목이
체계적으로 탈락 → 시장별 벤치마크가 실제로 알파를 잃고 있는지 검증.

변형:
  1) v2.3 baseline — 전 종목 KOSPI 대비
  2) v2.4 시장별 분리 — KOSPI종목은 KOSPI, KOSDAQ종목은 KOSDAQ 대비

v2.4 확정 후 backtester에 시장별 분기가 통합됨. 이 스크립트는
참고용으로 보존 (baseline 재현을 위해 precompute_market_aware를 유지).
"""
import sys
import time
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import pandas as pd
from loguru import logger

from src.backtest.portfolio_backtester import (
    UNIVERSE_REFRESH_DAYS,
    build_universe,
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.data_pipeline.db import get_connection
from src.strategy.trend_following_v2 import StrategyParams


TOTAL_COST_PCT = 0.0031


# ── 유틸 ────────────────────────────────────────────────────────

def load_index_ret_map(index_code: str, period: int) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT date, close FROM index_daily WHERE index_code = ? ORDER BY date",
            (index_code,),
        ).fetchall()
    if not rows:
        logger.warning(f"index_daily({index_code}) empty")
        return {}
    df = pd.DataFrame([dict(r) for r in rows])
    df['date'] = pd.to_datetime(df['date'])
    df['ret_n'] = df['close'].pct_change(period)
    return dict(zip(df['date'], df['ret_n']))


def load_ticker_market_map() -> dict:
    with get_connection() as conn:
        rows = conn.execute("SELECT ticker, market FROM stocks").fetchall()
    return {r['ticker']: r['market'] for r in rows}


# ── 시장별 벤치마크 precompute ─────────────────────────────────────

def precompute_market_aware(
    trading_dates, ticker_data, ticker_date_idx, initial_universe,
    params, kospi_ret_map, kosdaq_ret_map, ticker_market,
):
    """precompute_daily_signals와 동일하나 상대강도를 종목 시장에 따라 분기."""
    breadth_by_date = {}
    candidates_by_date = {}
    universe_by_date = {}

    universe = set(initial_universe)
    last_refresh = 0
    refresh_count = 1

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)
        if day_idx - last_refresh >= UNIVERSE_REFRESH_DAYS:
            with get_connection() as conn:
                universe = build_universe(date_str, conn)
            last_refresh = day_idx
            refresh_count += 1

        universe_by_date[date_str] = set(universe)

        above = 0
        total_b = 0
        for tk in universe:
            idx_map_b = ticker_date_idx.get(tk, {})
            bi = idx_map_b.get(ts)
            if bi is None or bi < 199:
                continue
            br_row = ticker_data[tk].iloc[bi]
            ma200_v = br_row.get('ma200')
            if pd.isna(ma200_v):
                continue
            total_b += 1
            if br_row['close'] > ma200_v:
                above += 1
        breadth_by_date[date_str] = above / total_b if total_b > 0 else 0.5

        kospi_ret = kospi_ret_map.get(ts)
        kosdaq_ret = kosdaq_ret_map.get(ts)

        cands = []
        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                continue
            curr_i = idx_map.get(ts)
            if curr_i is None or curr_i < params.ma_long:
                continue
            day = ticker_data[ticker].iloc[curr_i]

            req = ['ma20', 'ma60', 'ma120', 'ma60_slope', 'ma60_dist',
                   'atr', 'adx', 'macd_hist', 'avg_volume_5', 'avg_volume_20',
                   'avg_trading_value_20', 'stock_ret_n']
            if any(pd.isna(day.get(k)) for k in req):
                continue
            if day['atr'] <= 0 or day['close'] <= 0:
                continue

            if not (day['close'] > day['ma20'] > day['ma60'] > day['ma120']):
                continue
            if day['ma60_slope'] <= 0:
                continue
            if not (params.ma60_position_min <= day['ma60_dist'] <= params.ma60_position_max):
                continue
            if day['macd_hist'] <= 0:
                continue
            if day['avg_volume_5'] <= day['avg_volume_20']:
                continue
            if day['adx'] < params.adx_threshold:
                continue
            if day['avg_trading_value_20'] < params.min_trading_value:
                continue
            atr_ratio = day['atr'] / day['close']
            if not (params.atr_price_min <= atr_ratio <= params.atr_price_max):
                continue

            # 시장별 상대강도
            mkt = ticker_market.get(ticker, 'KOSPI')
            if mkt == 'KOSDAQ':
                bench_ret = kosdaq_ret
            else:  # KOSPI, UNKNOWN → KOSPI 기준
                bench_ret = kospi_ret
            if bench_ret is None or pd.isna(bench_ret):
                continue
            if (day['stock_ret_n'] - float(bench_ret)) < params.relative_strength_threshold:
                continue

            cands.append({
                'ticker': ticker,
                'score': float(day['adx']),
                'close': float(day['close']),
                'atr': float(day['atr']),
                'atr_ratio': float(atr_ratio),
                'ma60_dist': float(day['ma60_dist']),
            })
        cands.sort(key=lambda x: x['score'], reverse=True)
        candidates_by_date[date_str] = cands

    return {
        'breadth': breadth_by_date,
        'candidates': candidates_by_date,
        'universe_at': universe_by_date,
        'universe_refresh_count': refresh_count,
    }


# ── 분석 유틸 ──────────────────────────────────────────────────

def fmt_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def pf_gross(trades):
    gp = gl = 0.0
    for t in trades:
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if g > 0: gp += g
        else: gl += abs(g)
    return gp / gl if gl > 0 else float('inf')


def payoff(trades):
    w = [t.pnl_pct for t in trades if t.pnl_amount > 0]
    l = [t.pnl_pct for t in trades if t.pnl_amount <= 0]
    if not w or not l:
        return float('nan')
    return (sum(w) / len(w)) / abs(sum(l) / len(l))


def hold_buckets(trades):
    b = {'1-5d': [0, 0], '6-10d': [0, 0], '11-15d': [0, 0], '16-25d': [0, 0], '26d+': [0, 0]}
    for t in trades:
        hd = t.hold_days
        if hd <= 5: k = '1-5d'
        elif hd <= 10: k = '6-10d'
        elif hd <= 15: k = '11-15d'
        elif hd <= 25: k = '16-25d'
        else: k = '26d+'
        b[k][0] += t.pnl_amount
        b[k][1] += 1
    return b


def market_split(trades, ticker_market):
    kospi = [t for t in trades if ticker_market.get(t.ticker, 'KOSPI') != 'KOSDAQ']
    kosdaq = [t for t in trades if ticker_market.get(t.ticker, 'KOSPI') == 'KOSDAQ']
    return kospi, kosdaq


def yearly_pf(trades):
    by_year = defaultdict(lambda: [0.0, 0.0])  # [gross_profit, gross_loss]
    for t in trades:
        yr = t.exit_date[:4]
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT  # 비용 전
        pnl_net = t.pnl_amount
        if pnl_net > 0:
            by_year[yr][0] += pnl_net
        else:
            by_year[yr][1] += abs(pnl_net)
    result = {}
    for yr, (gp, gl) in sorted(by_year.items()):
        pf = gp / gl if gl > 0 else float('inf')
        result[yr] = pf
    return result


def yearly_count(trades):
    by_year = defaultdict(int)
    for t in trades:
        by_year[t.exit_date[:4]] += 1
    return dict(by_year)


# ── 메인 ────────────────────────────────────────────────────────

def run_all():
    t0 = time.time()
    params = StrategyParams()

    logger.info("데이터 로드")
    preloaded = load_backtest_data(params)

    logger.info("KOSDAQ 지수 ret map 로드")
    kosdaq_ret_map = load_index_ret_map('KOSDAQ', params.relative_strength_period)
    logger.info(f"KOSDAQ ret map entries: {len(kosdaq_ret_map)}")

    logger.info("ticker → market 매핑 로드")
    ticker_market = load_ticker_market_map()
    logger.info(f"ticker_market entries: {len(ticker_market)}")

    # ── 변형 1: baseline ──
    logger.info("[1] baseline precompute (KOSPI 통일)")
    t1 = time.time()
    precomp_baseline = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        params, kospi_ret_map=preloaded['kospi_ret_map'],
    )
    logger.info(f"  precompute {time.time()-t1:.1f}s")

    # ── 변형 2: 시장별 분리 ──
    logger.info("[2] 시장별 precompute (KOSPI/KOSDAQ 분기)")
    t2 = time.time()
    precomp_market = precompute_market_aware(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        params, preloaded['kospi_ret_map'], kosdaq_ret_map, ticker_market,
    )
    logger.info(f"  precompute {time.time()-t2:.1f}s")

    def run(name, precomp):
        logger.info(f"--- 백테스트: {name} ---")
        t = time.time()
        r = run_portfolio_backtest(
            initial_capital=5_000_000, max_positions=4, params=params,
            preloaded_data=preloaded, precomputed=precomp, risk=None,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, PF(전)={fmt_pf(pfg)}, "
            f"CAGR={r.cagr_pct:.1%}, MDD={r.max_drawdown_pct:.1%}, "
            f"net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        return r, net, pfg

    r1, n1, pfg1 = run('baseline (KOSPI 통일)', precomp_baseline)
    r2, n2, pfg2 = run('시장별 분리', precomp_market)

    total_time = time.time() - t0

    # ── 보고 출력 ──
    print("\n" + "=" * 100)
    print("📋 실험 11: 상대강도 시장별 벤치마크 완료 보고")
    print("=" * 100)
    print(f"Period: {r1.period}")
    print(f"총 소요: {total_time:.1f}s")

    print("\n■ 핵심 지표 비교")
    print(f"{'변형':<24} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
    print("-" * 100)
    for name, r, net, pfg in [
        ('v2.3 baseline (KOSPI)', r1, n1, pfg1),
        ('시장별 분리', r2, n2, pfg2),
    ]:
        p = payoff(r.trades)
        p_s = f"{p:.2f}" if p == p else 'n/a'
        print(
            f"{name:<24} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{fmt_pf(r.profit_factor):>6} {fmt_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+12,.0f} {p_s:>7}"
        )

    print("\n■ 시장별 거래 건수")
    print(f"{'변형':<24} {'KOSPI 거래':>12} {'KOSDAQ 거래':>14} {'KOSDAQ 비율':>14}")
    print("-" * 70)
    for name, r in [('baseline', r1), ('시장별 분리', r2)]:
        kp, kd = market_split(r.trades, ticker_market)
        total = len(r.trades)
        ratio = len(kd) / total * 100 if total else 0
        print(f"{name:<24} {len(kp):>12} {len(kd):>14} {ratio:>13.1f}%")

    print("\n■ 시장별 손익")
    print(f"{'변형':<24} {'KOSPI 손익':>14} {'KOSPI WR':>10} {'KOSDAQ 손익':>14} {'KOSDAQ WR':>11}")
    print("-" * 80)
    for name, r in [('baseline', r1), ('시장별 분리', r2)]:
        kp, kd = market_split(r.trades, ticker_market)
        kp_pnl = sum(t.pnl_amount for t in kp)
        kd_pnl = sum(t.pnl_amount for t in kd)
        kp_wr = sum(1 for t in kp if t.pnl_amount > 0) / len(kp) if kp else 0
        kd_wr = sum(1 for t in kd if t.pnl_amount > 0) / len(kd) if kd else 0
        print(f"{name:<24} {kp_pnl:>+14,.0f} {kp_wr:>9.1%} {kd_pnl:>+14,.0f} {kd_wr:>10.1%}")

    print("\n■ 러너 보존 (16-25d / 26d+)")
    print(f"{'변형':<24} {'16-25d 건':>10} {'16-25d 손익':>14} {'26d+ 건':>9} {'26d+ 손익':>13}")
    print("-" * 75)
    for name, r in [('baseline', r1), ('시장별 분리', r2)]:
        b = hold_buckets(r.trades)
        p16, n16 = b['16-25d']
        p26, n26 = b['26d+']
        print(f"{name:<24} {n16:>10} {p16:>+14,.0f} {n26:>9} {p26:>+13,.0f}")

    print("\n■ 연도별 PF")
    yp1 = yearly_pf(r1.trades)
    yp2 = yearly_pf(r2.trades)
    yc1 = yearly_count(r1.trades)
    yc2 = yearly_count(r2.trades)
    years = sorted(set(yp1.keys()) | set(yp2.keys()))
    print(f"{'연도':<6} {'baseline PF':>12} {'건':>4}   {'시장별 PF':>10} {'건':>4}   {'차이':>6}")
    print("-" * 60)
    for yr in years:
        p1 = yp1.get(yr, float('nan'))
        p2 = yp2.get(yr, float('nan'))
        c1 = yc1.get(yr, 0)
        c2 = yc2.get(yr, 0)
        diff = (p2 - p1) if (p1 == p1 and p2 == p2 and p1 != float('inf') and p2 != float('inf')) else float('nan')
        ds = f"{diff:+.2f}" if diff == diff else 'n/a'
        p1_s = fmt_pf(p1) if p1 == p1 else 'n/a'
        p2_s = fmt_pf(p2) if p2 == p2 else 'n/a'
        print(f"{yr:<6} {p1_s:>12} {c1:>4}   {p2_s:>10} {c2:>4}   {ds:>6}")

    # ── 판정 ──
    pf_diff = r2.profit_factor - r1.profit_factor
    net_diff = n2 - n1
    mdd_diff = r2.max_drawdown_pct - r1.max_drawdown_pct

    print(f"\n■ 판정")
    print(f"  PF: {fmt_pf(r1.profit_factor)} → {fmt_pf(r2.profit_factor)} ({pf_diff:+.2f})")
    print(f"  CAGR: {r1.cagr_pct:.1%} → {r2.cagr_pct:.1%}")
    print(f"  MDD: {r1.max_drawdown_pct:.1%} → {r2.max_drawdown_pct:.1%} ({mdd_diff*100:+.1f}%p)")
    print(f"  순손익: {n1:+,.0f} → {n2:+,.0f} ({net_diff:+,.0f})")

    if pf_diff >= 0.05 and net_diff > 0:
        print(f"  ✅ 시장별 분리 채택")
    elif pf_diff >= 0.02 and net_diff > 0:
        print(f"  ⚠ 소폭 개선 (판단 유보)")
    else:
        print(f"  ❌ 현행 유지")


if __name__ == "__main__":
    run_all()

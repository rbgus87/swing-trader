"""전략 비교 백테스트.

4개 전략을 동일 조건에서 실행하여 성과를 비교합니다.

Usage:
    python -m src.backtest.strategy_compare
"""
import sys
import numpy as np
import pandas as pd
from datetime import datetime
from loguru import logger
from src.backtest.engine import BacktestEngine, BacktestResult, COMMISSION_RATE, TAX_RATE, SLIPPAGE_RATE
from src.strategy.signals import calculate_indicators
from src.backtest.report import BacktestReporter

# 30종목 (KOSPI 시총 상위)
CODES = [
    '005930', '000660', '373220', '207940', '005380',
    '000270', '068270', '035420', '035720', '051910',
    '006400', '003670', '105560', '055550', '086790',
    '012330', '066570', '028260', '096770', '003550',
    '034730', '032830', '030200', '017670', '010130',
    '009150', '018260', '011200', '034020', '047050',
]

START_DATE = '20200101'
END_DATE = '20250314'


def strategy_1_momentum_breakout(df: pd.DataFrame, params: dict = None) -> tuple[pd.Series, pd.Series]:
    """전략 1: 모멘텀 브레이크아웃.

    매수: N일 최고가 돌파 + 거래량 증가
    매도: N일 최저가 이탈 또는 신호 기반
    """
    p = params or {}
    high_period = p.get("high_period", 20)
    low_period = p.get("low_period", 10)
    vol_mult = p.get("volume_multiplier", 1.2)

    df_ind = calculate_indicators(df)

    # Entry: N일 최고가 돌파 + 거래량 확인
    highest = df_ind["high"].rolling(high_period).max().shift(1)  # 전일까지의 N일 최고가
    vol_avg = df_ind["volume"].rolling(20).mean()

    raw_entries = (df_ind["close"] > highest) & (df_ind["volume"] >= vol_avg * vol_mult)

    # Exit: N일 최저가 이탈
    lowest = df_ind["low"].rolling(low_period).min().shift(1)
    raw_exits = df_ind["close"] < lowest

    entries = raw_entries.shift(1).fillna(False).astype(bool)
    exits = raw_exits.shift(1).fillna(False).astype(bool)
    entries.index = df_ind.index
    exits.index = df_ind.index

    return entries, exits


def strategy_2_mean_reversion(df: pd.DataFrame, params: dict = None) -> tuple[pd.Series, pd.Series]:
    """전략 2: 평균회귀 (볼린저밴드).

    매수: 볼린저밴드 하단 터치 + RSI 과매도 + 20일선 위
    매도: 볼린저밴드 중간선 도달 또는 상단 터치
    """
    p = params or {}
    rsi_oversold = p.get("rsi_oversold", 30)

    df_ind = calculate_indicators(df)

    # Entry: BB 하단 터치 + RSI 과매도 구간 + 추세 확인(SMA60 위)
    raw_entries = (
        (df_ind["close"] <= df_ind["bb_lower"]) &
        (df_ind["rsi"] <= rsi_oversold) &
        (df_ind["close"] > df_ind["sma60"])
    )

    # Exit: BB 중간선 도달
    raw_exits = df_ind["close"] >= df_ind["bb_mid"]

    entries = raw_entries.shift(1).fillna(False).astype(bool)
    exits = raw_exits.shift(1).fillna(False).astype(bool)
    entries.index = df_ind.index
    exits.index = df_ind.index

    return entries, exits


def strategy_3_dual_momentum(df: pd.DataFrame, params: dict = None) -> tuple[pd.Series, pd.Series]:
    """전략 3: 듀얼 모멘텀.

    매수: 절대 모멘텀(N일 수익률 > 0) + 상대 모멘텀(SMA20 > SMA60) + 거래량
    매도: 절대 모멘텀 음전환 또는 SMA20 < SMA60
    """
    p = params or {}
    momentum_period = p.get("momentum_period", 20)
    vol_mult = p.get("volume_multiplier", 1.2)

    df_ind = calculate_indicators(df)

    # 절대 모멘텀: N일 수익률
    returns_n = df_ind["close"].pct_change(momentum_period)
    vol_avg = df_ind["volume"].rolling(20).mean()

    # Entry: 절대 모멘텀 양수 + 상대 모멘텀(SMA20 > SMA60) + 거래량
    raw_entries = (
        (returns_n > 0) &
        (df_ind["sma20"] > df_ind["sma60"]) &
        (df_ind["volume"] >= vol_avg * vol_mult) &
        (returns_n.shift(1) <= 0)  # 양전환 시점
    )

    # Exit: 절대 모멘텀 음전환 또는 데드크로스
    raw_exits = (returns_n < 0) | (df_ind["sma20"] < df_ind["sma60"])

    entries = raw_entries.shift(1).fillna(False).astype(bool)
    exits = raw_exits.shift(1).fillna(False).astype(bool)
    entries.index = df_ind.index
    exits.index = df_ind.index

    return entries, exits


def strategy_4_golden_cross(df: pd.DataFrame, params: dict = None) -> tuple[pd.Series, pd.Series]:
    """전략 4: 골든크로스 변형.

    매수: SMA5 > SMA20 크로스 + RSI 50 이상 + 거래량 증가 + ADX > 20
    매도: SMA5 < SMA20 데드크로스
    """
    p = params or {}
    vol_mult = p.get("volume_multiplier", 1.2)
    adx_threshold = p.get("adx_threshold", 20)

    df_ind = calculate_indicators(df)
    vol_avg = df_ind["volume"].rolling(20).mean()

    # Entry: 골든크로스 + RSI 확인 + ADX 추세 강도 + 거래량
    golden_cross = (df_ind["sma5"] > df_ind["sma20"]) & (df_ind["sma5"].shift(1) <= df_ind["sma20"].shift(1))
    raw_entries = (
        golden_cross &
        (df_ind["rsi"] >= 50) &
        (df_ind["adx"] >= adx_threshold) &
        (df_ind["volume"] >= vol_avg * vol_mult)
    )

    # Exit: 데드크로스
    dead_cross = (df_ind["sma5"] < df_ind["sma20"]) & (df_ind["sma5"].shift(1) >= df_ind["sma20"].shift(1))
    raw_exits = dead_cross

    entries = raw_entries.shift(1).fillna(False).astype(bool)
    exits = raw_exits.shift(1).fillna(False).astype(bool)
    entries.index = df_ind.index
    exits.index = df_ind.index

    return entries, exits


def run_strategy(name: str, signal_func, engine: BacktestEngine, codes: list, start: str, end: str, params: dict = None) -> BacktestResult:
    """단일 전략 백테스트 실행."""
    logger.info(f"=== {name} 백테스트 시작 ===")

    price_data = engine.load_price_data(codes, start, end)
    if not price_data:
        return BacktestResult(0,0,0,0,0,0,0,0,0,0,params or {})

    all_results = []
    all_trades = []
    last_equity = None

    for code, df in price_data.items():
        try:
            entries, exits = signal_func(df, params)

            df_ind = calculate_indicators(df)
            close = df_ind["close"]
            high = df_ind["high"]
            low = df_ind["low"]
            atr_series = df_ind["atr"]

            # Align signal length with indicator length
            entries = entries.reindex(df_ind.index, fill_value=False)
            exits = exits.reindex(df_ind.index, fill_value=False)

            sim_params = params or {}
            sim_params.setdefault("stop_atr_mult", 2.0)
            sim_params.setdefault("target_return", 0.10)
            sim_params.setdefault("max_hold_days", 15)
            sim_params.setdefault("trailing_atr_mult", 2.5)
            sim_params.setdefault("trailing_activate_pct", 0.05)
            sim_params.setdefault("max_stop_pct", 0.10)

            trades, equity = engine._simulate_portfolio(close, high, low, atr_series, entries, exits, sim_params)
            result = engine._calculate_metrics(trades, equity, sim_params)

            all_results.append(result)
            all_trades.extend(trades)
            if last_equity is None:
                last_equity = equity
        except Exception as e:
            logger.warning(f"{code}: {e}")

    if not all_results:
        return BacktestResult(0,0,0,0,0,0,0,0,0,0,params or {})

    # Average across stocks
    avg = BacktestResult(
        total_return=round(np.mean([r.total_return for r in all_results]), 2),
        annual_return=round(np.mean([r.annual_return for r in all_results]), 2),
        max_drawdown=round(np.min([r.max_drawdown for r in all_results]), 2),
        sharpe_ratio=round(np.mean([r.sharpe_ratio for r in all_results]), 2),
        sortino_ratio=round(np.mean([r.sortino_ratio for r in all_results]), 2),
        win_rate=round(np.mean([r.win_rate for r in all_results]), 2),
        profit_factor=round(np.mean([r.profit_factor for r in all_results if r.profit_factor != float("inf")]) if any(r.profit_factor != float("inf") for r in all_results) else 0.0, 2),
        avg_trade_return=round(np.mean([r.avg_trade_return for r in all_results]), 2),
        trade_count=sum(r.trade_count for r in all_results),
        avg_hold_days=round(np.mean([r.avg_hold_days for r in all_results]), 1),
        params=params or {},
    )

    engine._last_trades = all_trades
    engine._last_equity = last_equity

    return avg


def main():
    sys.stdout.reconfigure(encoding='utf-8')

    engine = BacktestEngine(initial_capital=10_000_000)
    reporter = BacktestReporter()

    strategies = [
        ("1. 모멘텀 브레이크아웃", strategy_1_momentum_breakout, {"high_period": 20, "low_period": 10}),
        ("2. 평균회귀 (볼린저밴드)", strategy_2_mean_reversion, {"rsi_oversold": 30}),
        ("3. 듀얼 모멘텀", strategy_3_dual_momentum, {"momentum_period": 20}),
        ("4. 골든크로스 변형", strategy_4_golden_cross, {"adx_threshold": 20}),
    ]

    results = {}

    for name, func, params in strategies:
        print(f"\n{'='*60}")
        print(f"  {name}")
        print(f"{'='*60}")

        result = run_strategy(name, func, engine, CODES, START_DATE, END_DATE, params)
        results[name] = result
        reporter.print_summary(result)

        # HTML 리포트 생성
        safe_name = name.replace(" ", "_").replace(".", "").replace("(", "").replace(")", "")
        report_path = reporter.generate_html(
            result,
            output_path=f"reports/strategy_{safe_name}.html",
            equity=engine._last_equity,
            trades=engine._last_trades,
        )
        print(f"  리포트: {report_path}")

    # === 비교 테이블 출력 ===
    print(f"\n\n{'='*80}")
    print(f"  전략 비교 종합표 (30종목, 5년, 동일 리스크 관리)")
    print(f"{'='*80}")

    header = f"{'전략':<25} {'수익률':>8} {'연환산':>8} {'MDD':>8} {'Sharpe':>8} {'승률':>8} {'손익비':>8} {'거래수':>8} {'보유일':>8}"
    print(header)
    print("-" * len(header))

    for name, r in results.items():
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
        print(f"{name:<25} {r.total_return:>7.2f}% {r.annual_return:>7.2f}% {r.max_drawdown:>7.2f}% {r.sharpe_ratio:>8.2f} {r.win_rate:>7.2f}% {pf_str:>8} {r.trade_count:>8d} {r.avg_hold_days:>7.1f}")

    # 기준선 (MACD-RSI)
    print("-" * len(header))
    print(f"{'[기준] MACD-RSI':<25} {'0.02':>8}% {'-0.27':>8}% {'-41.51':>8}% {'-0.00':>8} {'49.63':>8}% {'1.56':>8} {'334':>8} {'6.1':>8}")
    print()


if __name__ == "__main__":
    main()

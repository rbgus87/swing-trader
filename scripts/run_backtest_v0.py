"""Phase 2 Step 1a — TrendFollowing v0 백테스트 실행.

대표 종목 5개에 대해 단일 종목 백테스트 실행.
포트폴리오(동시 보유 제한)는 Step 1b에서.
"""
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, Exception):
    pass

import pandas as pd
from loguru import logger

from src.data_pipeline.db import get_connection
from src.strategy.trend_following_v0 import StrategyParams, scan_entry_signals
from src.backtest.swing_backtester import run_single_backtest, CostModel


TEST_TICKERS = [
    ('005930', '삼성전자'),
    ('000660', 'SK하이닉스'),
    ('035420', 'NAVER'),
    ('068270', '셀트리온'),
    ('005380', '현대차'),
]

EXTENDED_TICKERS = [
    ('035720', '카카오'),
    ('006400', '삼성SDI'),
    ('207940', '삼성바이오로직스'),
    ('003670', '포스코퓨처엠'),
    ('247540', '에코프로비엠'),
]


def load_candles(ticker: str) -> pd.DataFrame:
    """daily_candles에서 종목 일봉 로드."""
    with get_connection() as conn:
        cursor = conn.execute("""
            SELECT date, open, high, low, close, volume
            FROM daily_candles
            WHERE ticker = ?
            ORDER BY date
        """, (ticker,))
        rows = cursor.fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df['date'] = pd.to_datetime(df['date'])
    return df


def run_and_report(tickers: list, params: StrategyParams):
    """종목 리스트에 대해 백테스트 실행 + 결과 출력."""
    cost = CostModel()
    results = []

    for ticker, name in tickers:
        df = load_candles(ticker)
        if df.empty or len(df) < params.ma_long + 10:
            logger.warning(f"{ticker} {name}: 데이터 부족 ({len(df)}행), 스킵")
            continue

        signals = scan_entry_signals(df, ticker, params)
        result = run_single_backtest(df, ticker, signals, params, cost)
        results.append((name, result))

        logger.info(
            f"{ticker} {name}: "
            f"trades={result.total_trades}, "
            f"WR={result.win_rate:.1%}, "
            f"PF={result.profit_factor:.2f}, "
            f"total={result.total_return_pct:.1%}, "
            f"MDD={result.max_drawdown_pct:.1%}"
        )

    print("\n" + "="*100)
    print("TrendFollowing v0 Baseline — 단일 종목 백테스트 결과")
    print("="*100)
    print(f"전략: 60일 신고가 돌파 + 정배열 + 거래량 확인")
    print(f"비용: 왕복 {cost.total_cost_pct():.2%}")
    print(f"파라미터: breakout={params.breakout_period}d, "
          f"SL=ATR×{params.stop_loss_atr}, TP1=ATR×{params.take_profit_atr}, "
          f"trail=ATR×{params.trailing_atr}, max_hold={params.max_hold_days}d")
    print()

    header = (
        f"{'종목':<16} {'거래수':>5} {'승률':>6} {'PF':>6} "
        f"{'총수익':>8} {'평균':>7} {'보유일':>7} {'MDD':>7} "
        f"{'손절':>4} {'익절':>4} {'트레일':>5} {'시간':>4} {'추세':>4}"
    )
    print(header)
    print("-"*len(header))

    for name, r in results:
        ed = r.exit_reason_dist
        pf_str = f"{r.profit_factor:>6.2f}" if r.profit_factor != float('inf') else f"{'inf':>6}"
        print(
            f"{name:<16} "
            f"{r.total_trades:>5} "
            f"{r.win_rate:>5.1%} "
            f"{pf_str} "
            f"{r.total_return_pct:>7.1%} "
            f"{r.avg_return_pct:>6.2%} "
            f"{r.avg_hold_days:>6.1f}d "
            f"{r.max_drawdown_pct:>6.1%} "
            f"{ed.get('STOP_LOSS',0):>4} "
            f"{ed.get('TAKE_PROFIT_1',0):>4} "
            f"{ed.get('TRAILING',0):>5} "
            f"{ed.get('TIME_EXIT',0):>4} "
            f"{ed.get('TREND_EXIT',0):>4}"
        )

    overall_pf = 0
    avg_wr = 0
    if results:
        all_trades = [t for _, r in results for t in r.trades]
        total = len(all_trades)
        winners = sum(1 for t in all_trades if t.pnl_pct > 0)
        total_pnl = sum(t.pnl_pct for t in all_trades)
        gross_profit = sum(t.pnl_pct for t in all_trades if t.pnl_pct > 0)
        gross_loss = abs(sum(t.pnl_pct for t in all_trades if t.pnl_pct <= 0))
        overall_pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        avg_wr = winners / total if total else 0

        print("-"*len(header))
        pf_str = f"{overall_pf:>6.2f}" if overall_pf != float('inf') else f"{'inf':>6}"
        print(f"{'전체 합산':<16} "
              f"{total:>5} "
              f"{avg_wr:>5.1%} "
              f"{pf_str} "
              f"{total_pnl:>7.1%}")

    print()

    if results:
        print("="*100)
        if overall_pf >= 1.5 and avg_wr >= 0.35:
            print("✅ STRONG — baseline에서 이미 유의미한 엣지. 필터 추가로 개선 여지 탐색.")
        elif overall_pf >= 1.0:
            print("⚠ MARGINAL — 엣지 있으나 약함. Squeeze/모멘텀 필터 추가 권장.")
        else:
            print("❌ WEAK — baseline 돌파 전략으로는 부족. 전략 구조 재검토 필요.")
        print("="*100)

    return results


def main():
    params = StrategyParams()

    logger.info("="*50)
    logger.info("Phase 2 Step 1a — v0 Baseline Backtest")
    logger.info("="*50)

    logger.info("\n--- 대표 종목 5개 ---")
    run_and_report(TEST_TICKERS, params)

    logger.info("\n--- 확장 종목 5개 ---")
    run_and_report(EXTENDED_TICKERS, params)


def run_portfolio_mode():
    """포트폴리오 레벨 백테스트."""
    from src.backtest.portfolio_backtester import run_portfolio_backtest

    logger.info("\n" + "="*80)
    logger.info("Phase 2 Step 1b — Portfolio Backtest (v0.1)")
    logger.info("="*80)

    result = run_portfolio_backtest(
        initial_capital=5_000_000,
        max_positions=4,
    )

    print("\n" + "="*80)
    print("TrendFollowing v0.1 — Portfolio Backtest Result")
    print("="*80)
    print(f"Period:          {result.period}")
    print(f"Initial Capital: {result.initial_capital:,.0f}원")
    print(f"Final Capital:   {result.final_capital:,.0f}원")
    print(f"Total Return:    {result.total_return_pct:.1%}")
    print(f"CAGR:            {result.cagr_pct:.1%}")
    print(f"MDD:             {result.max_drawdown_pct:.1%}")
    print(f"Total Trades:    {result.total_trades}")
    print(f"Win Rate:        {result.win_rate:.1%}")
    pf_s = f"{result.profit_factor:.2f}" if result.profit_factor != float('inf') else 'inf'
    print(f"Profit Factor:   {pf_s}")
    print(f"Avg Hold Days:   {result.avg_hold_days:.1f}")
    print(f"Avg Positions:   {result.avg_positions:.2f}")
    max_c = getattr(result, 'max_concurrent', '?')
    u_refresh = getattr(result, 'universe_refresh_count', '?')
    print(f"Max Concurrent:  {max_c}")
    print(f"Universe Refresh: {u_refresh}")

    gate_open = getattr(result, 'gate_open_days', 0)
    gate_closed = getattr(result, 'gate_closed_days', 0)
    total_days = gate_open + gate_closed
    if total_days > 0:
        print(f"Gate OPEN days:   {gate_open} ({gate_open/total_days:.0%})")
        print(f"Gate CLOSED days: {gate_closed} ({gate_closed/total_days:.0%})")

    avg_uni = getattr(result, 'avg_universe_size', 0)
    if avg_uni > 0:
        print(f"Universe avg size:   {avg_uni:.0f}")

    print(f"\nExit Reason Distribution:")
    for reason, count in sorted(result.exit_reason_dist.items(), key=lambda x: -x[1]):
        pct = count / result.total_trades if result.total_trades else 0
        print(f"  {reason}: {count} ({pct:.1%})")

    print(f"\nAnnual Returns:")
    equity_by_year = {}
    for date_str, eq in result.equity_curve:
        year = date_str[:4]
        if year not in equity_by_year:
            equity_by_year[year] = {'first': eq, 'last': eq}
        equity_by_year[year]['last'] = eq

    prev_eq = result.initial_capital
    for year in sorted(equity_by_year.keys()):
        yr_return = (equity_by_year[year]['last'] / prev_eq) - 1
        print(f"  {year}: {yr_return:+.1%}")
        prev_eq = equity_by_year[year]['last']

    print("\n" + "="*80)
    if result.profit_factor >= 1.5 and result.cagr_pct >= 0.10:
        print("✅ STRONG — 포트폴리오 레벨에서 유의미한 엣지.")
    elif result.profit_factor >= 1.0 and result.cagr_pct >= 0.0:
        print("⚠ MARGINAL — 엣지 있으나 필터/파라미터 개선 필요.")
    else:
        print("❌ WEAK — 포트폴리오 레벨에서 수익 불가. 전략 재검토.")
    print("="*80)


if __name__ == "__main__":
    main()
    print("\n\n")
    run_portfolio_mode()

"""Phase 2 Step 2a — MeanReversion v0 포트폴리오 백테스트."""
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, Exception):
    pass

from loguru import logger

from src.backtest.portfolio_backtester import run_mr_portfolio_backtest


def main():
    logger.info("="*80)
    logger.info("Phase 2 Step 2a — MeanReversion v0 Portfolio Backtest")
    logger.info("="*80)

    result = run_mr_portfolio_backtest(
        initial_capital=5_000_000,
        max_positions=4,
    )

    print("\n" + "="*80)
    print("MeanReversion v0 — Portfolio Backtest Result")
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
    print(f"Max Concurrent:  {max_c}")
    avg_uni = getattr(result, 'avg_universe_size', 0)
    if avg_uni > 0:
        print(f"Universe avg:    {avg_uni:.0f}")

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
    if result.profit_factor >= 1.5 and result.cagr_pct >= 0.08:
        print("✅ STRONG — MR 단독 엣지 유의미.")
    elif result.profit_factor >= 1.0 and result.cagr_pct >= 0.0:
        print("⚠ MARGINAL — 엣지 있음. TF와 합산 보완 가능.")
    else:
        print("❌ WEAK — 단독으로 수익 불가.")
    print("="*80)


if __name__ == "__main__":
    main()

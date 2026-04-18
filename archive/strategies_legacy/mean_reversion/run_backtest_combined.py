"""Phase 2 Step 2b — MR v0.2 단독 + TF·MR 합산 포트폴리오 백테스트."""
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, Exception):
    pass

from loguru import logger

from src.backtest.portfolio_backtester import (
    run_mr_portfolio_backtest,
    run_combined_portfolio_backtest,
)


def _print_result(title, result, extra_fields=None):
    print("\n" + "="*80)
    print(title)
    print("="*80)
    print(f"Period:          {result.period}")
    print(f"Initial / Final: {result.initial_capital:,.0f} → {result.final_capital:,.0f}")
    print(f"Total Return:    {result.total_return_pct:+.1%}")
    print(f"CAGR:            {result.cagr_pct:+.2%}")
    print(f"MDD:             {result.max_drawdown_pct:.1%}")
    pf = result.profit_factor
    pf_s = f"{pf:.2f}" if pf != float('inf') else 'inf'
    print(f"PF:              {pf_s}")
    print(f"WR:              {result.win_rate:.1%}")
    print(f"Trades:          {result.total_trades}")
    print(f"Avg Hold:        {result.avg_hold_days:.1f}")
    print(f"Avg Positions:   {result.avg_positions:.2f}")
    if extra_fields:
        for k, v in extra_fields.items():
            print(f"{k}: {v}")
    print(f"\nExit Reasons:")
    for reason, cnt in sorted(result.exit_reason_dist.items(), key=lambda x: -x[1]):
        p = cnt / result.total_trades if result.total_trades else 0
        print(f"  {reason}: {cnt} ({p:.1%})")


def _annual(result):
    ey = {}
    for d, eq in result.equity_curve:
        y = d[:4]
        if y not in ey:
            ey[y] = {'first': eq, 'last': eq}
        ey[y]['last'] = eq
    rows = []
    prev = result.initial_capital
    for y in sorted(ey):
        yr = (ey[y]['last'] / prev) - 1
        rows.append((y, yr))
        prev = ey[y]['last']
    return rows


def main():
    logger.info("="*80)
    logger.info("Phase 2 Step 2b — MR v0.2 + Combined Portfolio")
    logger.info("="*80)

    # MR v0.2 단독
    logger.info("\n--- MR v0.2 단독 ---")
    mr = run_mr_portfolio_backtest(initial_capital=5_000_000, max_positions=4)
    _print_result("MeanReversion v0.2 — 단독", mr)

    # 합산 (TF 2 + MR 2)
    logger.info("\n--- Combined (TF 2 + MR 2) ---")
    comb = run_combined_portfolio_backtest(
        initial_capital=5_000_000,
        max_positions=4,
        tf_slots=2,
        mr_slots=2,
    )
    go = getattr(comb, 'gate_open_days', 0)
    gc = getattr(comb, 'gate_closed_days', 0)
    td = go + gc
    tf_cnt = getattr(comb, 'tf_trade_count', 0)
    mr_cnt = getattr(comb, 'mr_trade_count', 0)
    _print_result(
        "Combined — TF 2슬롯 + MR 2슬롯",
        comb,
        extra_fields={
            'TF trades': tf_cnt,
            'MR trades': mr_cnt,
            'Gate OPEN/CLOSED': f"{go}/{gc} ({go/td:.0%}/{gc/td:.0%})" if td else '?',
        },
    )

    # 연도별 3열 비교 (TF는 이전 확정치 하드코딩)
    TF_ANNUAL = {
        '2014': -0.030, '2015': 0.057, '2016': -0.122, '2017': -0.050,
        '2018': -0.031, '2019': 0.015, '2020': -0.072, '2021': -0.052,
        '2022': 0.000, '2023': 0.140, '2024': -0.170, '2025': 0.374,
        '2026': 0.232,
    }
    mr_annual = dict(_annual(mr))
    comb_annual = dict(_annual(comb))

    print("\n" + "="*80)
    print("연도별 수익률 비교 (TF v1 / MR v0.2 / Combined)")
    print("="*80)
    print(f"{'Year':<6} {'TF v1':>10} {'MR v0.2':>10} {'Combined':>10}")
    print("-"*40)
    for y in sorted(TF_ANNUAL.keys()):
        tf_y = TF_ANNUAL[y]
        mr_y = mr_annual.get(y, 0)
        co_y = comb_annual.get(y, 0)
        print(f"{y:<6} {tf_y:>+10.1%} {mr_y:>+10.1%} {co_y:>+10.1%}")


if __name__ == "__main__":
    main()

"""실험 17b: 5M에서 TP1 10% + TP2 변형."""
import sys
import time
from dataclasses import replace

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from loguru import logger

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v2 import StrategyParams


def fmt_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def cm_score(r):
    return r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0


def run():
    t0 = time.time()
    base = StrategyParams()
    v25 = replace(base, tp2_atr=4.0, tp2_sell_ratio=0.30)

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base)

    logger.info("precompute")
    pc = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        v25,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )

    variants = [
        (1, 0.20, 0.20, '20/20/60'),
        (2, 0.10, 0.20, '10/20/70'),
        (3, 0.10, 0.10, '10/10/80'),
        (4, 0.10, 0.30, '10/30/60'),
    ]

    rows = []
    for n, t1, t2, lbl in variants:
        params = replace(v25, tp1_sell_ratio=t1, tp2_sell_ratio=t2)
        t = time.time()
        r = run_portfolio_backtest(
            initial_capital=5_000_000, max_positions=4, params=params,
            preloaded_data=preloaded, precomputed=pc,
            risk=None, sizing_mode='equity',
        )
        net = r.final_capital - r.initial_capital
        logger.info(
            f"#{n} {lbl}: trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, MDD={r.max_drawdown_pct:.1%}, "
            f"net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        rows.append((n, lbl, r, net))

    total_time = time.time() - t0

    print("\n" + "=" * 95)
    print("📋 실험 17b: 5M·4종목 TP1 10% 그룹")
    print("=" * 95)
    print(f"Period: {rows[0][2].period} / 총 소요: {total_time:.1f}s")
    print()
    print(f"{'#':<3} {'TP비율':<10} {'건수':>5} {'WR':>6} {'PF':>5} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9}")
    print("-" * 80)
    for n, lbl, r, net in rows:
        print(
            f"{n:<3} {lbl:<10} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{fmt_pf(r.profit_factor):>5} {r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm_score(r):>8.2f}"
        )


if __name__ == "__main__":
    run()

"""실험 17: 5M·10M TP 비율 최종 확인."""
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


TOTAL_COST_PCT = 0.0031


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
        # (#, 자본, 종목수, TP1, TP2, label)
        (1, 5_000_000,  4, 0.30, 0.30, '5M/4', '30/30/40'),
        (2, 5_000_000,  4, 0.20, 0.30, '5M/4', '20/30/50'),
        (3, 5_000_000,  4, 0.20, 0.20, '5M/4', '20/20/60'),
        (4, 10_000_000, 6, 0.20, 0.30, '10M/6', '20/30/50'),
        (5, 10_000_000, 6, 0.20, 0.20, '10M/6', '20/20/60'),
    ]

    rows = []
    for n, cap, mp, t1, t2, cap_lbl, tp_lbl in variants:
        params = replace(v25, tp1_sell_ratio=t1, tp2_sell_ratio=t2)
        min_amt = 600_000 if cap == 10_000_000 else 300_000
        t = time.time()
        r = run_portfolio_backtest(
            initial_capital=cap, max_positions=mp, params=params,
            preloaded_data=preloaded, precomputed=pc,
            risk=None, sizing_mode='equity', min_position_amount=min_amt,
        )
        net = r.final_capital - r.initial_capital
        logger.info(
            f"#{n} {cap_lbl} {tp_lbl}: trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, MDD={r.max_drawdown_pct:.1%}, "
            f"net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        rows.append((n, cap_lbl, tp_lbl, r, net))

    total_time = time.time() - t0

    print("\n" + "=" * 100)
    print("📋 실험 17: 5M·10M TP 비율 최종")
    print("=" * 100)
    print(f"Period: {rows[0][3].period} / 총 소요: {total_time:.1f}s")

    print(f"\n{'#':<3} {'자본/종목':<10} {'TP비율':<10} {'건수':>5} {'WR':>6} {'PF':>5} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9}")
    print("-" * 95)
    for n, cap_lbl, tp_lbl, r, net in rows:
        print(
            f"{n:<3} {cap_lbl:<10} {tp_lbl:<10} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{fmt_pf(r.profit_factor):>5} {r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm_score(r):>8.2f}"
        )


if __name__ == "__main__":
    run()

"""v2.6 검증 — 5M/4 + 10M/6에서 실험 17c #1·#4 결과 재현."""
import sys
import time

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
from src.utils.config import config


def fmt_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def cm(r):
    return r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0


def run():
    t0 = time.time()
    # config에서 v2.6 파라미터 로드
    params = StrategyParams(
        tp1_sell_ratio=float(config.get("trend_following.tp1_sell_ratio", 0.10)),
        tp2_atr=float(config.get("trend_following.tp2_atr", 4.0)),
        tp2_sell_ratio=float(config.get("trend_following.tp2_sell_ratio", 0.10)),
    )
    logger.info(
        f"v2.6 params: tp1={params.tp1_sell_ratio}, "
        f"tp2_atr={params.tp2_atr}, tp2_ratio={params.tp2_sell_ratio}"
    )

    logger.info("데이터 로드")
    preloaded = load_backtest_data(params)

    logger.info("precompute")
    pc = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        params,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )

    def go(name, cap, mp, min_amt):
        t = time.time()
        r = run_portfolio_backtest(
            initial_capital=cap, max_positions=mp, params=params,
            preloaded_data=preloaded, precomputed=pc,
            risk=None, sizing_mode='equity', min_position_amount=min_amt,
        )
        net = r.final_capital - r.initial_capital
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        return r, net

    r5, n5 = go('v2.6 5M/4',  5_000_000,  4, 300_000)
    r10, n10 = go('v2.6 10M/6', 10_000_000, 6, 600_000)

    print("\n" + "=" * 90)
    print("📋 v2.6 검증 보고")
    print("=" * 90)
    print(f"Period: {r5.period} / 총 소요: {time.time()-t0:.1f}s")
    print()
    print(f"{'사양':<14} {'건수':>5} {'WR':>6} {'PF':>5} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9}")
    print("-" * 75)
    for name, r, net in [('5M/4', r5, n5), ('10M/6', r10, n10)]:
        print(
            f"{name:<14} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{fmt_pf(r.profit_factor):>5} {r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm(r):>8.2f}"
        )

    # 기대치 비교 (실험 17c #1·#4)
    print("\n■ 실험 17c 기대치 대비")
    print(f"  5M/4 기대: PF 2.41 / CAGR 21.2% / MDD 31.4% / WR 59.3%")
    print(f"  5M/4 실측: PF {r5.profit_factor:.2f} / CAGR {r5.cagr_pct:.1%} / MDD {r5.max_drawdown_pct:.1%} / WR {r5.win_rate:.1%}")
    print()
    print(f"  10M/6 기대: PF 1.95 / CAGR 17.7% / MDD 32.1% / WR 59.9%")
    print(f"  10M/6 실측: PF {r10.profit_factor:.2f} / CAGR {r10.cagr_pct:.1%} / MDD {r10.max_drawdown_pct:.1%} / WR {r10.win_rate:.1%}")

    pf_5m_ok = abs(r5.profit_factor - 2.41) < 0.05
    pf_10m_ok = abs(r10.profit_factor - 1.95) < 0.05

    print("\n■ 판정")
    if pf_5m_ok and pf_10m_ok:
        print("  ✅ v2.6 검증 통과 — 실험 17c 결과 재현")
    else:
        print(f"  ⚠ PF 차이: 5M Δ{r5.profit_factor-2.41:+.2f}, 10M Δ{r10.profit_factor-1.95:+.2f}")


if __name__ == "__main__":
    run()

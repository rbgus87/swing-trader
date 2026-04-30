"""실험 14: 10M 자본 최적화 — min_position_amount × max_positions 그리드.

5M: PF 1.93, MDD 33.9% (실험 13과 일치)
10M (v2.5 동일 파라미터): PF 1.44, MDD 48.6%

가설: min_position_amount=300K가 10M에서 상대적으로 작아 저품질 진입 217건
추가 통과. 자본 비례 스케일(600K~1M) + 종목 수(3/4/5) 조합 탐색.
"""
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


def pf_gross(trades):
    gp = gl = 0.0
    for t in trades:
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if g > 0:
            gp += g
        else:
            gl += abs(g)
    return gp / gl if gl > 0 else float('inf')


def run():
    t0 = time.time()
    base = StrategyParams()
    v25 = replace(base, tp2_atr=4.0, tp2_sell_ratio=0.30)

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base)

    logger.info("precompute")
    precomp = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        base,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )

    def go(name, capital, max_pos, min_amt, params=v25, mode='equity'):
        logger.info(f"--- {name}: cap={capital:,}, max_pos={max_pos}, min={min_amt:,} ---")
        t = time.time()
        r = run_portfolio_backtest(
            initial_capital=capital, max_positions=max_pos,
            params=params, preloaded_data=preloaded, precomputed=precomp,
            risk=None, sizing_mode=mode, min_position_amount=min_amt,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        return r, net, pfg

    variants = [
        # name, capital, max_pos, min_amt
        ('5M v2.5 (참고)',         5_000_000,  4,   300_000),
        ('10M 현행 (min 300K)',    10_000_000, 4,   300_000),
        ('10M min 600K (6%)',      10_000_000, 4,   600_000),
        ('10M min 800K (8%)',      10_000_000, 4,   800_000),
        ('10M 3종목 (min 600K)',   10_000_000, 3,   600_000),
        ('10M 5종목 (min 600K)',   10_000_000, 5,   600_000),
        ('10M min 1,000K (10%)',   10_000_000, 4, 1_000_000),
    ]

    results = []
    for name, cap, mp, mn in variants:
        r, net, pfg = go(name, cap, mp, mn)
        results.append((name, r, net, pfg, cap, mp, mn))

    total_time = time.time() - t0

    # ── 보고 ──
    print("\n" + "=" * 115)
    print("📋 실험 14: 10M 자본 최적화 완료 보고")
    print("=" * 115)
    print(f"Period: {results[0][1].period}")
    print(f"총 소요: {total_time:.1f}s")

    print("\n■ 핵심 지표")
    print(f"{'변형':<28} {'건수':>5} {'WR':>6} {'PF':>5} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9}")
    print("-" * 115)
    for name, r, net, pfg, cap, mp, mn in results:
        cm = r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0
        print(
            f"{name:<28} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{fmt_pf(r.profit_factor):>5} {fmt_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm:>8.2f}"
        )

    # 5M reference
    ref = results[0]
    ref_pf = ref[1].profit_factor
    ref_mdd = ref[1].max_drawdown_pct
    ref_cagr = ref[1].cagr_pct
    ref_wr = ref[1].win_rate

    # 10M variants only
    ten_m = results[1:]

    print("\n■ 5M v2.5 대비 (10M 변형들)")
    print(f"{'변형':<28} {'ΔPF':>8} {'ΔCAGR':>9} {'ΔMDD':>9} {'ΔWR':>9}")
    print("-" * 75)
    for name, r, _, _, _, _, _ in ten_m:
        d_pf = r.profit_factor - ref_pf
        d_cagr = (r.cagr_pct - ref_cagr) * 100
        d_mdd = (r.max_drawdown_pct - ref_mdd) * 100
        d_wr = (r.win_rate - ref_wr) * 100
        print(
            f"{name:<28} {d_pf:>+8.2f} {d_cagr:>+8.1f}%p "
            f"{d_mdd:>+8.1f}%p {d_wr:>+8.1f}%p"
        )

    # 최근접 변형 (5M PF에 가장 가까운 10M)
    closest = min(ten_m, key=lambda x: abs(x[1].profit_factor - ref_pf))
    print(f"\n■ 5M v2.5(PF {ref_pf:.2f})에 PF 기준 가장 근접: {closest[0]} (PF {closest[1].profit_factor:.2f})")

    # 판정 — CAGR/MDD 비율 최고
    def cm(r):
        return r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0
    best_cm = max(ten_m, key=lambda x: cm(x[1]))
    best_pnl = max(ten_m, key=lambda x: x[2])
    best_pf = max(ten_m, key=lambda x: x[1].profit_factor)

    print("\n■ 판정 (10M 변형 중)")
    print(f"  CAGR/MDD 최고:  {best_cm[0]} ({cm(best_cm[1]):.2f})")
    print(f"  PF 최고:        {best_pf[0]} (PF {best_pf[1].profit_factor:.2f})")
    print(f"  순손익 최고:    {best_pnl[0]} ({best_pnl[2]:+,.0f})")

    # 5M 수준 회복 여부
    print(f"\n■ 5M 수준 회복 여부 (PF≥{ref_pf-0.1:.2f}, MDD≤{(ref_mdd+0.03)*100:.0f}%)")
    recovered = [x for x in ten_m
                 if x[1].profit_factor >= ref_pf - 0.10
                 and x[1].max_drawdown_pct <= ref_mdd + 0.03]
    if recovered:
        for n, r, net, _, _, _, _ in recovered:
            print(f"  ✅ {n}: PF {r.profit_factor:.2f}, MDD {r.max_drawdown_pct:.1%}, net {net:+,.0f}")
    else:
        print("  ❌ 5M 수준에 도달한 10M 변형 없음")


if __name__ == "__main__":
    run()

"""v2.5 확정 검증 — 자본 1000만원 + equity 균등 + TP2.

실험 13의 equity+TP2(30+30) 결과 (자본 500만원 기준):
  PF 1.95 / CAGR 17.4% / MDD 33.9% / WR 60.8% / trades 687

자본 1000만원으로 변경 시 수치가 약간 달라질 수 있음 — 확인.
"""
import sys
import time
from collections import defaultdict
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


def tp_stats(trades):
    n1 = sum(1 for t in trades if t.exit_reason == 'TAKE_PROFIT_1')
    n2 = sum(1 for t in trades if t.exit_reason == 'TAKE_PROFIT_2')
    p1 = sum(t.pnl_amount for t in trades if t.exit_reason == 'TAKE_PROFIT_1')
    p2 = sum(t.pnl_amount for t in trades if t.exit_reason == 'TAKE_PROFIT_2')
    return n1, p1, n2, p2


def run():
    t0 = time.time()
    base = StrategyParams()  # v2.4 default — TP2 OFF

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base)

    logger.info("precompute")
    t1 = time.time()
    precomp = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        base,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )
    logger.info(f"  precompute {time.time()-t1:.1f}s")

    # v2.5 = base + TP2 4.0/30%
    v25_params = replace(base, tp2_atr=4.0, tp2_sell_ratio=0.30)

    def go(name, capital, sizing_mode, params, min_pos=300_000):
        t = time.time()
        r = run_portfolio_backtest(
            initial_capital=capital, max_positions=4, params=params,
            preloaded_data=preloaded, precomputed=precomp, risk=None,
            sizing_mode=sizing_mode, min_position_amount=min_pos,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] cap={capital:,}, mode={sizing_mode}, "
            f"trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        return r, net, pfg

    logger.info("=== v2.4 baseline (5M, cash×25%, TP2 off) ===")
    r24_5m, n24_5m, _ = go('v2.4 5M', 5_000_000, 'cash_pct', base)

    logger.info("=== v2.5 (10M, equity, TP2 on) ===")
    r25, n25, pfg25 = go('v2.5 10M', 10_000_000, 'equity', v25_params)

    logger.info("=== v2.5 비교: 10M cash×25% (TP2 off) ===")
    r25_cash, n25_cash, _ = go('v2.5 10M cash', 10_000_000, 'cash_pct', base)

    logger.info("=== 비교: v2.5 (5M, equity, TP2 on) ===")
    r25_5m, n25_5m, _ = go('v2.5 5M', 5_000_000, 'equity', v25_params)

    total_time = time.time() - t0

    print("\n" + "=" * 100)
    print("📋 v2.5 확정 검증 보고")
    print("=" * 100)
    print(f"Period: {r25.period}")
    print(f"총 소요: {total_time:.1f}s")

    print("\n■ 핵심 지표")
    print(f"{'변형':<40} {'cap(M)':>7} {'건수':>5} {'WR':>6} {'PF':>6} {'CAGR':>7} {'MDD':>7} {'순손익':>14}")
    print("-" * 105)
    for name, r, net, cap in [
        ('v2.4 baseline (5M cash, TP2 off)',  r24_5m,   n24_5m,   5),
        ('v2.5 (5M equity, TP2 on)',          r25_5m,   n25_5m,   5),
        ('v2.5 (10M equity, TP2 on)',         r25,      n25,      10),
        ('v2.5 (10M cash×25%, TP2 off)',      r25_cash, n25_cash, 10),
    ]:
        print(
            f"{name:<40} {cap:>7} {r.total_trades:>5} "
            f"{r.win_rate:>5.1%} {fmt_pf(r.profit_factor):>6} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f}"
        )

    print("\n■ TP 발동 (v2.5 10M)")
    n1, p1, n2, p2 = tp_stats(r25.trades)
    print(f"  TP1: {n1}건, +{p1:,.0f}원")
    print(f"  TP2: {n2}건, +{p2:,.0f}원")

    # 기대치 검증
    print("\n■ 기대치 대비")
    print("  실험 13 equity+TP2(30+30) at 5M: PF 1.95 / CAGR 17.4% / MDD 33.9% / WR 60.8%")
    print(f"  v2.5 검증     at 10M:           "
          f"PF {fmt_pf(r25.profit_factor)} / CAGR {r25.cagr_pct:.1%} / "
          f"MDD {r25.max_drawdown_pct:.1%} / WR {r25.win_rate:.1%}")
    print(f"  v2.5 검증     at  5M:           "
          f"PF {fmt_pf(r25_5m.profit_factor)} / CAGR {r25_5m.cagr_pct:.1%} / "
          f"MDD {r25_5m.max_drawdown_pct:.1%} / WR {r25_5m.win_rate:.1%}")

    # 기대 범위 (실험 13 결과 ±10%)
    pf_ok = 1.7 <= r25_5m.profit_factor <= 2.2
    cagr_ok = 0.13 <= r25_5m.cagr_pct <= 0.22
    wr_ok = 0.55 <= r25_5m.win_rate <= 0.65

    print("\n■ 판정")
    if pf_ok and cagr_ok and wr_ok:
        print("  ✅ v2.5 검증 통과 — 실험 13 결과와 일치")
    else:
        print(f"  ⚠ 일부 지표 범위 이탈 — PF_ok={pf_ok} CAGR_ok={cagr_ok} WR_ok={wr_ok}")


if __name__ == "__main__":
    run()

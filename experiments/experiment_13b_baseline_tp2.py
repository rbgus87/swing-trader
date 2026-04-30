"""실험 13b: baseline + TP2 — cash×25%에 TP2만 추가.

실험 13에서 TP2(+4ATR/30%)가 러너를 죽이지 않으면서 WR을 크게 올리는
좋은 구조라는 것이 확인됨. equity 균등의 MDD 문제는 TP로 해결 불가
→ baseline 사이징을 유지한 채 TP2만 얹어 품질 개선 가능한지 확인.
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from dataclasses import replace
from loguru import logger

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v2 import StrategyParams


TOTAL_COST_PCT = 0.0031
INITIAL_CAPITAL = 5_000_000
MAX_POSITIONS = 4


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
        if hd <= 5:   k = '1-5d'
        elif hd <= 10: k = '6-10d'
        elif hd <= 15: k = '11-15d'
        elif hd <= 25: k = '16-25d'
        else:          k = '26d+'
        b[k][0] += t.pnl_amount
        b[k][1] += 1
    return b


def tp_stats(trades):
    n1 = sum(1 for t in trades if t.exit_reason == 'TAKE_PROFIT_1')
    n2 = sum(1 for t in trades if t.exit_reason == 'TAKE_PROFIT_2')
    p1 = sum(t.pnl_amount for t in trades if t.exit_reason == 'TAKE_PROFIT_1')
    p2 = sum(t.pnl_amount for t in trades if t.exit_reason == 'TAKE_PROFIT_2')
    return n1, p1, n2, p2


def run():
    t0 = time.time()
    base_params = StrategyParams()

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base_params)

    logger.info("precompute (v2.4)")
    t1 = time.time()
    precomp = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        base_params,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )
    logger.info(f"  precompute {time.time()-t1:.1f}s")

    def go(name, override):
        logger.info(f"--- 백테스트: {name} ---")
        params = replace(base_params, **override)
        t = time.time()
        r = run_portfolio_backtest(
            initial_capital=INITIAL_CAPITAL, max_positions=MAX_POSITIONS,
            params=params, preloaded_data=preloaded, precomputed=precomp,
            risk=None, sizing_mode='cash_pct',
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        return r, net, pfg

    r1, n1, pfg1 = go('v2.4 baseline (cash×25%, TP2 없음)', {})
    r2, n2, pfg2 = go('baseline+TP2 (cash×25%, TP2 4.0/30%)',
                       {'tp2_atr': 4.0, 'tp2_sell_ratio': 0.30})

    total_time = time.time() - t0

    # ── 보고 ──
    print("\n" + "=" * 110)
    print("📋 실험 13b: baseline + TP2 완료 보고")
    print("=" * 110)
    print(f"Period: {r1.period}")
    print(f"총 소요: {total_time:.1f}s")

    print("\n■ 핵심 지표")
    print(f"{'변형':<40} {'건수':>5} {'WR':>5} {'PF':>5} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9} {'Payoff':>7}")
    print("-" * 115)
    for name, r, net, pfg in [('baseline', r1, n1, pfg1), ('baseline+TP2', r2, n2, pfg2)]:
        cm = r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0
        p = payoff(r.trades)
        ps = f"{p:.2f}" if p == p else 'n/a'
        print(
            f"{name:<40} {r.total_trades:>5} {r.win_rate:>4.1%} "
            f"{fmt_pf(r.profit_factor):>5} {fmt_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm:>8.2f} {ps:>7}"
        )

    print("\n■ TP 발동 통계")
    print(f"{'변형':<40} {'TP1 건':>7} {'TP1 손익':>14} {'TP2 건':>7} {'TP2 손익':>14}")
    print("-" * 90)
    for name, r in [('baseline', r1), ('baseline+TP2', r2)]:
        nn1, pp1, nn2, pp2 = tp_stats(r.trades)
        print(f"{name:<40} {nn1:>7} {pp1:>+14,.0f} {nn2:>7} {pp2:>+14,.0f}")

    print("\n■ 러너 보존 (16-25d / 26d+)")
    print(f"{'변형':<40} {'16-25d 건':>10} {'16-25d 손익':>15} {'26d+ 건':>9} {'26d+ 손익':>14}")
    print("-" * 95)
    for name, r in [('baseline', r1), ('baseline+TP2', r2)]:
        b = hold_buckets(r.trades)
        p16, n16 = b['16-25d']
        p26, n26 = b['26d+']
        print(f"{name:<40} {n16:>10} {p16:>+15,.0f} {n26:>9} {p26:>+14,.0f}")

    # 판정
    pf_diff = r2.profit_factor - r1.profit_factor
    cagr_diff = r2.cagr_pct - r1.cagr_pct
    mdd_diff = r2.max_drawdown_pct - r1.max_drawdown_pct
    wr_diff = r2.win_rate - r1.win_rate
    net_diff = n2 - n1

    print("\n■ 판정")
    print(f"  WR: {r1.win_rate:.1%} → {r2.win_rate:.1%} ({wr_diff*100:+.1f}%p)")
    print(f"  PF: {fmt_pf(r1.profit_factor)} → {fmt_pf(r2.profit_factor)} ({pf_diff:+.2f})")
    print(f"  CAGR: {r1.cagr_pct:.1%} → {r2.cagr_pct:.1%} ({cagr_diff*100:+.1f}%p)")
    print(f"  MDD: {r1.max_drawdown_pct:.1%} → {r2.max_drawdown_pct:.1%} ({mdd_diff*100:+.1f}%p)")
    print(f"  순손익: {n1:+,.0f} → {n2:+,.0f} ({net_diff:+,.0f})")

    cm1 = r1.cagr_pct / r1.max_drawdown_pct if r1.max_drawdown_pct > 0 else 0
    cm2 = r2.cagr_pct / r2.max_drawdown_pct if r2.max_drawdown_pct > 0 else 0
    print(f"  CAGR/MDD: {cm1:.2f} → {cm2:.2f} ({cm2-cm1:+.2f})")

    # 채택 기준: MDD 악화 ≤ +1%p, WR/PF 개선 → 채택
    if mdd_diff <= 0.01 and (pf_diff >= 0.02 or net_diff > 500_000) and wr_diff >= 0:
        print("  ✅ baseline+TP2 채택 (MDD 유지 + 품질 개선)")
    elif mdd_diff <= 0.01 and net_diff > 0:
        print("  ⚠ 소폭 개선 (판단 유보)")
    else:
        print("  ❌ 현행 baseline 유지")


if __name__ == "__main__":
    run()

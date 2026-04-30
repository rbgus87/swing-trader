"""실험 12: 포지션 사이징 — cash×25% vs equity 균등 배분.

현행 v2.4: alloc = cash × (1/max_positions)
- 매수 순서 1→2→3→4 진행하면 4번째는 1번째의 ~42%에 그침
- 복리 효과 없음 (수익이 cash 외에 묶이면 새 진입 alloc 늘지 않음)

대안: alloc = total_equity / max_positions
- total_equity = cash + 보유 종목 평가액(당일 종가)
- 매수 순서 무관 균등
- 수익에 따라 alloc 자동 확장 (복리)
- alloc은 cash로 상한 (없는 돈으로 못 삼)

run_portfolio_backtest(sizing_mode='cash_pct'|'equity_equal',
                       alloc_tracker=list) 사용.
"""
import sys
import time
from collections import defaultdict

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


def yearly_pf(trades):
    by_year = defaultdict(lambda: [0.0, 0.0])
    for t in trades:
        yr = t.exit_date[:4]
        if t.pnl_amount > 0:
            by_year[yr][0] += t.pnl_amount
        else:
            by_year[yr][1] += abs(t.pnl_amount)
    result = {}
    for yr, (gp, gl) in sorted(by_year.items()):
        result[yr] = gp / gl if gl > 0 else float('inf')
    return result


def yearly_count(trades):
    by_year = defaultdict(int)
    for t in trades:
        by_year[t.exit_date[:4]] += 1
    return dict(by_year)


def order_avg_alloc(tracker):
    """매수 순서별 평균 alloc."""
    bucket = defaultdict(list)
    for r in tracker:
        bucket[r['order']].append(r['alloc'])
    out = {}
    for o in sorted(bucket.keys()):
        vals = bucket[o]
        out[o] = (sum(vals) / len(vals), len(vals))
    return out


def run():
    t0 = time.time()
    params = StrategyParams()

    logger.info("데이터 로드")
    preloaded = load_backtest_data(params)

    logger.info("precompute (v2.4 시장별 분기)")
    t1 = time.time()
    precomp = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        params,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )
    logger.info(f"  precompute {time.time()-t1:.1f}s")

    def go(name, sizing_mode):
        logger.info(f"--- 백테스트: {name} ---")
        t = time.time()
        tracker = []
        r = run_portfolio_backtest(
            initial_capital=5_000_000, max_positions=4, params=params,
            preloaded_data=preloaded, precomputed=precomp, risk=None,
            sizing_mode=sizing_mode, alloc_tracker=tracker,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, PF(전)={fmt_pf(pfg)}, "
            f"CAGR={r.cagr_pct:.1%}, MDD={r.max_drawdown_pct:.1%}, "
            f"net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        return r, net, pfg, tracker

    r1, n1, pfg1, tk1 = go('v2.4 baseline (cash×25%)', 'cash_pct')
    r2, n2, pfg2, tk2 = go('equity 균등',              'equity_equal')

    total_time = time.time() - t0

    print("\n" + "=" * 100)
    print("📋 실험 12: 포지션 사이징 완료 보고")
    print("=" * 100)
    print(f"Period: {r1.period}")
    print(f"총 소요: {total_time:.1f}s")

    # ── 핵심 지표 ──
    print("\n■ 핵심 지표 비교")
    print(f"{'변형':<28} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'Payoff':>7}")
    print("-" * 100)
    for name, r, net, pfg in [
        ('v2.4 baseline (cash×25%)', r1, n1, pfg1),
        ('equity 균등',              r2, n2, pfg2),
    ]:
        p = payoff(r.trades)
        ps = f"{p:.2f}" if p == p else 'n/a'
        print(
            f"{name:<28} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{fmt_pf(r.profit_factor):>6} {fmt_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {ps:>7}"
        )

    # ── 매수 순서별 평균 alloc ──
    print("\n■ 포지션 크기 비교 (평균 alloc, 원)")
    a1 = order_avg_alloc(tk1)
    a2 = order_avg_alloc(tk2)
    orders = sorted(set(a1.keys()) | set(a2.keys()))
    print(f"{'매수 순서':<10} {'baseline':>14} {'(건)':>7}    {'equity 균등':>14} {'(건)':>7}    {'비율(eq/base)':>14}")
    print("-" * 80)
    for o in orders:
        b = a1.get(o, (0, 0))
        e = a2.get(o, (0, 0))
        ratio = e[0] / b[0] if b[0] else float('nan')
        rs = f"{ratio:.2f}x" if ratio == ratio else 'n/a'
        print(
            f"{o:<10} {b[0]:>14,.0f} {b[1]:>7}    "
            f"{e[0]:>14,.0f} {e[1]:>7}    {rs:>14}"
        )

    # 1번째 vs 4번째 ratio
    if 1 in a1 and 4 in a1:
        ratio_b = a1[4][0] / a1[1][0]
    else:
        ratio_b = float('nan')
    if 1 in a2 and 4 in a2:
        ratio_e = a2[4][0] / a2[1][0]
    else:
        ratio_e = float('nan')
    print(f"\n  4번째/1번째: baseline {ratio_b:.2f}, equity {ratio_e:.2f}")

    # ── 러너 보존 ──
    print("\n■ 러너 보존 (16-25d / 26d+)")
    print(f"{'변형':<28} {'16-25d 건':>10} {'16-25d 손익':>15} {'26d+ 건':>9} {'26d+ 손익':>14}")
    print("-" * 85)
    for name, r in [('baseline (cash×25%)', r1), ('equity 균등', r2)]:
        b = hold_buckets(r.trades)
        p16, n16 = b['16-25d']
        p26, n26 = b['26d+']
        print(f"{name:<28} {n16:>10} {p16:>+15,.0f} {n26:>9} {p26:>+14,.0f}")

    # ── 연도별 PF (큰 차이 위주) ──
    yp1, yp2 = yearly_pf(r1.trades), yearly_pf(r2.trades)
    yc1, yc2 = yearly_count(r1.trades), yearly_count(r2.trades)
    print("\n■ 연도별 PF (전체)")
    years = sorted(set(yp1.keys()) | set(yp2.keys()))
    print(f"{'연도':<6} {'baseline':>10} {'건':>4}   {'equity':>10} {'건':>4}   {'차이':>8}")
    print("-" * 55)
    for yr in years:
        p1 = yp1.get(yr, float('nan'))
        p2 = yp2.get(yr, float('nan'))
        c1 = yc1.get(yr, 0)
        c2 = yc2.get(yr, 0)
        if p1 == p1 and p2 == p2 and p1 != float('inf') and p2 != float('inf'):
            diff = p2 - p1
            ds = f"{diff:+.2f}"
        else:
            ds = 'n/a'
        p1s = fmt_pf(p1) if p1 == p1 else 'n/a'
        p2s = fmt_pf(p2) if p2 == p2 else 'n/a'
        print(f"{yr:<6} {p1s:>10} {c1:>4}   {p2s:>10} {c2:>4}   {ds:>8}")

    print("\n■ 연도별 큰 차이 (|Δ| ≥ 0.30)")
    bigs = []
    for yr in years:
        p1 = yp1.get(yr, float('nan'))
        p2 = yp2.get(yr, float('nan'))
        if p1 == p1 and p2 == p2 and p1 != float('inf') and p2 != float('inf'):
            d = p2 - p1
            if abs(d) >= 0.30:
                bigs.append((yr, p1, p2, d, yc1.get(yr, 0), yc2.get(yr, 0)))
    if bigs:
        print(f"{'연도':<6} {'baseline':>10} {'equity':>10} {'차이':>8} {'건수(b/e)':>12}")
        for yr, p1, p2, d, c1, c2 in bigs:
            print(f"{yr:<6} {fmt_pf(p1):>10} {fmt_pf(p2):>10} {d:>+8.2f}   {c1:>3}/{c2:<3}")
    else:
        print("  (해당 연도 없음)")

    # ── 판정 ──
    pf_diff = r2.profit_factor - r1.profit_factor
    net_diff = n2 - n1
    mdd_diff = r2.max_drawdown_pct - r1.max_drawdown_pct
    cagr_diff = r2.cagr_pct - r1.cagr_pct

    print("\n■ 판정")
    print(f"  PF: {fmt_pf(r1.profit_factor)} → {fmt_pf(r2.profit_factor)} ({pf_diff:+.2f})")
    print(f"  CAGR: {r1.cagr_pct:.1%} → {r2.cagr_pct:.1%} ({cagr_diff*100:+.1f}%p)")
    print(f"  MDD: {r1.max_drawdown_pct:.1%} → {r2.max_drawdown_pct:.1%} ({mdd_diff*100:+.1f}%p)")
    print(f"  순손익: {n1:+,.0f} → {n2:+,.0f} ({net_diff:+,.0f})")

    # 채택 기준: MDD 악화 ≤ +3%p, PF +0.05+ or 순손익 +1M+ → 채택
    if pf_diff >= 0.05 and net_diff > 1_000_000 and mdd_diff <= 0.03:
        print("  ✅ equity 균등 채택")
    elif pf_diff >= 0.02 and net_diff > 0 and mdd_diff <= 0.03:
        print("  ⚠ 소폭 개선 (판단 유보)")
    else:
        print("  ❌ 현행 유지 (cash×25%)")


if __name__ == "__main__":
    run()

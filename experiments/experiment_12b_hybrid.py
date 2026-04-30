"""실험 12b: 하이브리드 사이징 — equity 균등 + alloc 상한.

실험 12 결과: equity 균등은 CAGR 1.5배·MDD도 1.5배 (위험조정 수익률 동일).
하이브리드는 equity 복리를 살리되 alloc 상한으로 후반 폭주를 제어.

상한: initial_capital × cap_pct (30/35/40% 비교).
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


def order_alloc_stats(tracker, order):
    """매수 순서 N의 alloc 평균/최대/건수."""
    vals = [r['alloc'] for r in tracker if r['order'] == order]
    if not vals:
        return (0, 0, 0)
    return (sum(vals) / len(vals), max(vals), len(vals))


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

    def go(name, sizing_mode, cap):
        logger.info(f"--- 백테스트: {name} ---")
        t = time.time()
        tracker = []
        r = run_portfolio_backtest(
            initial_capital=INITIAL_CAPITAL, max_positions=MAX_POSITIONS,
            params=params, preloaded_data=preloaded, precomputed=precomp,
            risk=None, sizing_mode=sizing_mode, alloc_tracker=tracker,
            equity_alloc_cap=cap,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        return r, net, pfg, tracker

    variants = [
        ('baseline (cash×25%)',       'cash_pct',     None),
        ('equity 균등',                'equity_equal', None),
        ('hybrid-30% (상한 1,500,000)','equity_equal', INITIAL_CAPITAL * 0.30),
        ('hybrid-35% (상한 1,750,000)','equity_equal', INITIAL_CAPITAL * 0.35),
        ('hybrid-40% (상한 2,000,000)','equity_equal', INITIAL_CAPITAL * 0.40),
    ]

    results = []
    for name, mode, cap in variants:
        r, net, pfg, tk = go(name, mode, cap)
        results.append((name, r, net, pfg, tk))

    total_time = time.time() - t0

    # ── 보고 ──
    print("\n" + "=" * 110)
    print("📋 실험 12b: 하이브리드 사이징 완료 보고")
    print("=" * 110)
    print(f"Period: {results[0][1].period}")
    print(f"총 소요: {total_time:.1f}s")

    # 핵심 지표
    print("\n■ 핵심 지표")
    print(f"{'변형':<32} {'건수':>5} {'WR':>5} {'PF':>5} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9} {'Payoff':>7}")
    print("-" * 110)
    for name, r, net, pfg, _ in results:
        cm = r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else float('inf')
        p = payoff(r.trades)
        ps = f"{p:.2f}" if p == p else 'n/a'
        print(
            f"{name:<32} {r.total_trades:>5} {r.win_rate:>4.1%} "
            f"{fmt_pf(r.profit_factor):>5} {fmt_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm:>8.2f} {ps:>7}"
        )

    # 4번째 매수 alloc
    print("\n■ 4번째 매수 평균/최대 alloc")
    print(f"{'변형':<32} {'평균':>14} {'최대':>14} {'건수':>6}")
    print("-" * 75)
    for name, _, _, _, tk in results:
        avg, mx, cnt = order_alloc_stats(tk, 4)
        print(f"{name:<32} {avg:>14,.0f} {mx:>14,.0f} {cnt:>6}")

    # 모든 매수 순서별 평균
    print("\n■ 매수 순서별 평균 alloc (참고)")
    header = f"{'변형':<32}"
    for o in (1, 2, 3, 4):
        header += f"{'1번' if o==1 else '2번' if o==2 else '3번' if o==3 else '4번':>13}"
    print(header)
    print("-" * 90)
    for name, _, _, _, tk in results:
        line = f"{name:<32}"
        for o in (1, 2, 3, 4):
            avg, _, _ = order_alloc_stats(tk, o)
            line += f"{avg:>13,.0f}"
        print(line)

    # 판정 — CAGR/MDD 기준
    print("\n■ 판정")
    best_idx = 0
    best_score = -1.0
    for i, (name, r, net, _, _) in enumerate(results):
        if r.max_drawdown_pct <= 0:
            continue
        score = r.cagr_pct / r.max_drawdown_pct
        if score > best_score:
            best_score = score
            best_idx = i
    bn, br, bnet, _, _ = results[best_idx]
    print(f"  CAGR/MDD 최고: {bn}")
    print(f"    CAGR={br.cagr_pct:.1%}, MDD={br.max_drawdown_pct:.1%}, "
          f"비율={best_score:.2f}, 순손익={bnet:+,.0f}")

    # 절대 수익 최고
    best_pnl_idx = max(range(len(results)), key=lambda i: results[i][2])
    bpn = results[best_pnl_idx][0]
    print(f"  순손익 최고: {bpn}")

    # MDD 최저 (수익 양수 조건)
    candidates = [(i, r) for i, (_, r, net, _, _) in enumerate(results) if net > 0]
    if candidates:
        best_mdd_idx = min(candidates, key=lambda x: x[1].max_drawdown_pct)[0]
        bmn = results[best_mdd_idx][0]
        bmr = results[best_mdd_idx][1]
        print(f"  MDD 최저 (수익+): {bmn}  (MDD={bmr.max_drawdown_pct:.1%})")


if __name__ == "__main__":
    run()

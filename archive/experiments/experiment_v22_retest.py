"""실험 10: v2.2 위에서 TP1 + ATR 사이징 재실험.

v2.1의 TP1/리스크 실험을 v2.2(상태 기반 추세추종) 기반으로 재검증.
v2.2 러너 분포(16-25d 159건 +6.8M)가 v2.1(26d+ 8건 +5.1M)보다 분산 →
리스크 관리 여력 있을 수 있음.

6 변형:
  1) v2.2 baseline (TP1 2.0 / 50%)
  2) TP1 제거 (take_profit_atr=0)
  3) TP1 3.0 / 50%
  4) TP1 2.0 / 30%
  5) v2.2 + ATR 사이징
  6) Best TP1 + ATR 사이징
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
    RiskParams,
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v2 import StrategyParams


TOTAL_COST_PCT = 0.0031


def compute_payoff(trades):
    w = [t.pnl_pct for t in trades if t.pnl_amount > 0]
    l = [t.pnl_pct for t in trades if t.pnl_amount <= 0]
    if not w or not l:
        return float('nan')
    return (sum(w) / len(w)) / abs(sum(l) / len(l))


def pf_gross(trades):
    gp = gl = 0.0
    for t in trades:
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if g > 0: gp += g
        else: gl += abs(g)
    return gp / gl if gl > 0 else float('inf')


def hold_buckets(trades):
    b = {'1-5d': [0, 0], '6-10d': [0, 0], '11-15d': [0, 0], '16-25d': [0, 0], '26d+': [0, 0]}
    for t in trades:
        hd = t.hold_days
        if hd <= 5: k = '1-5d'
        elif hd <= 10: k = '6-10d'
        elif hd <= 15: k = '11-15d'
        elif hd <= 25: k = '16-25d'
        else: k = '26d+'
        b[k][0] += t.pnl_amount
        b[k][1] += 1
    return b


def exit_dist(trades):
    d = defaultdict(int)
    for t in trades:
        d[t.exit_reason] += 1
    return d


def tp1_stats(trades):
    tp1 = [t for t in trades if t.exit_reason == 'TAKE_PROFIT_1']
    rest = [t for t in trades if t.exit_reason != 'TAKE_PROFIT_1']
    tp1_pnl = sum(t.pnl_amount for t in tp1)
    rest_pnl = sum(t.pnl_amount for t in rest)
    return len(tp1), tp1_pnl, rest_pnl


def format_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def run_all():
    t0 = time.time()
    base_params = StrategyParams()

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base_params)

    logger.info("v2.2 precompute (kospi_ret_map 포함)")
    precomp = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        base_params, kospi_ret_map=preloaded['kospi_ret_map'],
    )
    t_prep = time.time() - t0
    logger.info(f"준비 완료 ({t_prep:.1f}s)")

    # TP1 파라미터 세트
    p_baseline = replace(base_params, take_profit_atr=2.0, tp1_sell_ratio=0.5)
    p_no_tp1 = replace(base_params, take_profit_atr=0.0, tp1_sell_ratio=0.0)
    p_tp1_30_50 = replace(base_params, take_profit_atr=3.0, tp1_sell_ratio=0.5)
    p_tp1_20_30 = replace(base_params, take_profit_atr=2.0, tp1_sell_ratio=0.3)

    # ATR 사이징 RiskParams
    atr_risk = RiskParams(
        enable_sizing=False, enable_atr_sizing=True,
        enable_daily_loss=False, enable_ticker_cooldown=False,
        atr_sizing_risk_pct=0.02, atr_sizing_max_pct=0.30,
    )

    def run_one(name, params, risk):
        logger.info(f"\n--- {name} ---")
        t1 = time.time()
        r = run_portfolio_backtest(
            initial_capital=5_000_000, max_positions=4, params=params,
            preloaded_data=preloaded, precomputed=precomp, risk=risk,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={format_pf(r.profit_factor)}, PF(전)={format_pf(pfg)}, "
            f"CAGR={r.cagr_pct:.1%}, MDD={r.max_drawdown_pct:.1%}, "
            f"net={net:+,.0f} ({time.time()-t1:.1f}s)"
        )
        return (name, r, net, pfg)

    # TP1 변형 4개
    results = []
    results.append(run_one('v2.2 baseline', p_baseline, None))
    results.append(run_one('TP1 제거', p_no_tp1, None))
    results.append(run_one('TP1 3.0/50%', p_tp1_30_50, None))
    results.append(run_one('TP1 2.0/30%', p_tp1_20_30, None))

    # Best TP1 선택 (baseline 포함 중 PF 최고)
    tp1_variants = results[:4]
    best_tp1 = max(tp1_variants, key=lambda x: (x[1].profit_factor, x[2]))
    best_tp1_params_map = {
        'v2.2 baseline': p_baseline,
        'TP1 제거': p_no_tp1,
        'TP1 3.0/50%': p_tp1_30_50,
        'TP1 2.0/30%': p_tp1_20_30,
    }
    best_tp1_params = best_tp1_params_map[best_tp1[0]]
    logger.info(f"\n>>> Best TP1: {best_tp1[0]} (PF {format_pf(best_tp1[1].profit_factor)})")

    # ATR 사이징 변형 2개
    results.append(run_one('ATR 사이징 (baseline TP1)', p_baseline, atr_risk))
    results.append(run_one(f'Best TP1 + ATR 사이징', best_tp1_params, atr_risk))

    total_time = time.time() - t0

    # ── 출력 ──
    baseline_name, baseline_r, baseline_net, baseline_pfg = results[0]

    print("\n" + "=" * 100)
    print("실험 10: v2.2 TP1 + 리스크 재실험 결과")
    print("=" * 100)
    print(f"Period: {baseline_r.period}")
    print(f"총 소요: {total_time:.1f}초 (prep {t_prep:.1f}s + sim {total_time-t_prep:.1f}s)")

    print("\n## 핵심 지표 비교")
    print(f"{'변형':<26} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
    print("-" * 105)
    for name, r, net, pfg in results:
        payoff = compute_payoff(r.trades)
        pay_s = f"{payoff:.2f}" if payoff == payoff else 'n/a'
        print(
            f"{name:<26} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{format_pf(r.profit_factor):>6} {format_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+12,.0f} {pay_s:>7}"
        )

    # 러너 보존
    print("\n## 러너 보존 (16-25d / 26d+)")
    print(f"{'변형':<26} {'16-25d 건':>10} {'16-25d 손익':>15} {'26d+ 건':>9} {'26d+ 손익':>14}")
    print("-" * 80)
    for name, r, *_ in results:
        b = hold_buckets(r.trades)
        p16, n16 = b['16-25d']
        p26, n26 = b['26d+']
        print(f"{name:<26} {n16:>10} {p16:>+15,.0f} {n26:>9} {p26:>+14,.0f}")

    # TP1 기여도 (TP1 있는 변형만)
    print("\n## TP1 기여도")
    print(f"{'변형':<26} {'TP1터치':>8} {'TP1손익':>14} {'나머지손익':>14}")
    print("-" * 70)
    for name, r, *_ in results:
        n_tp1, tp1_pnl, rest_pnl = tp1_stats(r.trades)
        print(f"{name:<26} {n_tp1:>8} {tp1_pnl:>+14,.0f} {rest_pnl:>+14,.0f}")

    # Exit reason
    print("\n## Exit Reason 분포")
    reasons = ['STOP_LOSS', 'TAKE_PROFIT_1', 'TRAILING', 'TREND_EXIT', 'TIME_EXIT', 'FINAL_CLOSE']
    print(f"{'변형':<26} " + " ".join(f"{r:>13}" for r in reasons))
    print("-" * (26 + 14 * len(reasons)))
    for name, r, *_ in results:
        d = exit_dist(r.trades)
        total = r.total_trades
        cells = []
        for reason in reasons:
            n = d.get(reason, 0)
            pct = n / total if total else 0
            cells.append(f"{n:>4}({pct:>5.1%})")
        print(f"{name:<26} " + " ".join(f"{c:>13}" for c in cells))

    # 보유 기간별 (Best vs baseline)
    non_baseline = results[1:]
    best_name, best_r, best_net, best_pfg = max(non_baseline, key=lambda x: (x[1].profit_factor, x[2]))

    print(f"\n## 보유 기간별 상세 (Best: {best_name} vs v2.2 baseline)")
    bb = hold_buckets(baseline_r.trades)
    xb = hold_buckets(best_r.trades)
    print(f"{'구간':<10} {'Baseline PnL(건)':>22} {'Best PnL(건)':>22} {'변화':>15}")
    print("-" * 75)
    for key in ['1-5d', '6-10d', '11-15d', '16-25d', '26d+']:
        bp, bn = bb[key]
        xp, xn = xb[key]
        print(f"{key:<10} {bp:>+14,.0f} ({bn:>3}) {xp:>+14,.0f} ({xn:>3}) {xp - bp:>+14,.0f}")

    print(f"\n## Best vs Baseline")
    print(f"{'항목':<10} {'v2.2 baseline':<20} {'Best':<30} {'변화':<15}")
    print("-" * 80)
    print(f"{'PF':<10} {format_pf(baseline_r.profit_factor):<20} {format_pf(best_r.profit_factor) + ' (' + best_name + ')':<30} "
          f"{best_r.profit_factor - baseline_r.profit_factor:+.2f}")
    print(f"{'CAGR':<10} {baseline_r.cagr_pct:<20.1%} {best_r.cagr_pct:<30.1%} "
          f"{(best_r.cagr_pct - baseline_r.cagr_pct)*100:+.1f}%p")
    print(f"{'MDD':<10} {baseline_r.max_drawdown_pct:<20.1%} {best_r.max_drawdown_pct:<30.1%} "
          f"{(best_r.max_drawdown_pct - baseline_r.max_drawdown_pct)*100:+.1f}%p")
    print(f"{'순손익':<10} {baseline_net:<+20,.0f} {best_net:<+30,.0f} "
          f"{best_net - baseline_net:+,.0f}")

    print(f"\n## 판정")
    pf_diff = best_r.profit_factor - baseline_r.profit_factor
    net_diff = best_net - baseline_net
    mdd_diff = best_r.max_drawdown_pct - baseline_r.max_drawdown_pct
    print(f"  Best: {best_name}")
    print(f"  PF: {format_pf(baseline_r.profit_factor)} → {format_pf(best_r.profit_factor)} ({pf_diff:+.2f})")
    print(f"  CAGR: {baseline_r.cagr_pct:.1%} → {best_r.cagr_pct:.1%}")
    print(f"  MDD: {baseline_r.max_drawdown_pct:.1%} → {best_r.max_drawdown_pct:.1%} ({mdd_diff*100:+.1f}%p)")
    print(f"  순손익: {baseline_net:+,.0f} → {best_net:+,.0f} ({net_diff:+,.0f})")

    if pf_diff >= 0.03 and net_diff > 0:
        print(f"  ✅ 개선 채택")
    elif pf_diff > 0:
        print(f"  ⚠ 소폭 개선 (판단 유보)")
    else:
        print(f"  ❌ 현행 유지")


if __name__ == "__main__":
    run_all()

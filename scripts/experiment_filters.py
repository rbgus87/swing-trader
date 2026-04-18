"""실험 5: v2 저비용 필터 3종 비교.

- RSI 과열 차단
- ATR/가격 밴드 (2.5~8%)
- ATR 역비례 사이징

precompute 1회 후 6개 변형 실행.
"""
import sys
import time
from collections import defaultdict
from copy import copy

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
from src.backtest.swing_backtester import CostModel
from src.strategy.trend_following_v0 import StrategyParams


RSI_MAX = 70.0
ATR_RATIO_MIN = 0.025
ATR_RATIO_MAX = 0.08
TOTAL_COST_PCT = 0.0031


def filter_precomp(precomp, *, rsi_max=None, atr_ratio_min=None, atr_ratio_max=None):
    """precomp['candidates']를 필터링하여 새 precomp 반환 + 차단 통계."""
    rsi_blocked = 0
    atr_blocked = 0
    new_cands = {}
    for date, cands in precomp['candidates'].items():
        filtered = []
        for c in cands:
            if rsi_max is not None and c.get('rsi14', 50.0) > rsi_max:
                rsi_blocked += 1
                continue
            if atr_ratio_min is not None and c.get('atr_ratio', 0.0) < atr_ratio_min:
                atr_blocked += 1
                continue
            if atr_ratio_max is not None and c.get('atr_ratio', 0.0) > atr_ratio_max:
                atr_blocked += 1
                continue
            filtered.append(c)
        new_cands[date] = filtered
    new_precomp = {**precomp, 'candidates': new_cands}
    return new_precomp, rsi_blocked, atr_blocked


def compute_payoff(trades):
    winners = [t.pnl_pct for t in trades if t.pnl_amount > 0]
    losers = [t.pnl_pct for t in trades if t.pnl_amount <= 0]
    if not winners or not losers:
        return float('nan')
    return (sum(winners) / len(winners)) / abs(sum(losers) / len(losers))


def pf_gross(trades):
    """비용 전 PF — pnl_amount에 비용 복원."""
    gp = gl = 0.0
    for t in trades:
        gross = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if gross > 0:
            gp += gross
        else:
            gl += abs(gross)
    return gp / gl if gl > 0 else float('inf')


def hold_bucket_stats(trades):
    buckets = {'1-5d': [], '6-10d': [], '11-15d': [], '16-25d': [], '26d+': []}
    for t in trades:
        hd = t.hold_days
        if hd <= 5:
            key = '1-5d'
        elif hd <= 10:
            key = '6-10d'
        elif hd <= 15:
            key = '11-15d'
        elif hd <= 25:
            key = '16-25d'
        else:
            key = '26d+'
        buckets[key].append(t.pnl_amount)
    return {k: (sum(v), len(v)) for k, v in buckets.items()}


def format_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def run_all():
    params = StrategyParams()

    t0 = time.time()
    logger.info("데이터 로드 + precompute (1회)")
    preloaded = load_backtest_data(params)
    precomp_base = precompute_daily_signals(
        preloaded['trading_dates'],
        preloaded['ticker_data'],
        preloaded['ticker_date_idx'],
        preloaded['initial_universe'],
        params,
    )
    t_prep = time.time() - t0
    logger.info(f"준비 완료 ({t_prep:.1f}초)")

    # 변형별 precomp 준비
    precomp_rsi, n_rsi_rsi, _ = filter_precomp(precomp_base, rsi_max=RSI_MAX)
    precomp_atr, _, n_atr_atr = filter_precomp(
        precomp_base, atr_ratio_min=ATR_RATIO_MIN, atr_ratio_max=ATR_RATIO_MAX
    )
    precomp_both, n_both_rsi, n_both_atr = filter_precomp(
        precomp_base,
        rsi_max=RSI_MAX, atr_ratio_min=ATR_RATIO_MIN, atr_ratio_max=ATR_RATIO_MAX,
    )

    atr_risk = RiskParams(
        enable_sizing=False, enable_atr_sizing=True,
        enable_daily_loss=False, enable_ticker_cooldown=False,
    )

    variants = [
        # (name, precomp, risk, rsi_blocked, atr_blocked)
        ('v2 baseline',         precomp_base,  None,     0,          0),
        ('v2 + RSI',            precomp_rsi,   None,     n_rsi_rsi,  0),
        ('v2 + ATR밴드',         precomp_atr,   None,     0,          n_atr_atr),
        ('v2 + RSI+ATR밴드',     precomp_both,  None,     n_both_rsi, n_both_atr),
        ('v2 + ATR사이징',        precomp_base,  atr_risk, 0,          0),
        ('v2 + 전체',            precomp_both,  atr_risk, n_both_rsi, n_both_atr),
    ]

    results = []
    for name, pcomp, risk, n_rsi, n_atr in variants:
        logger.info(f"\n--- {name} ---")
        t1 = time.time()
        r = run_portfolio_backtest(
            initial_capital=5_000_000,
            max_positions=4,
            params=params,
            preloaded_data=preloaded,
            precomputed=pcomp,
            risk=risk,
        )
        dt = time.time() - t1
        net = r.final_capital - r.initial_capital
        pf_g = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={format_pf(r.profit_factor)}, PF(전)={format_pf(pf_g)}, "
            f"CAGR={r.cagr_pct:.1%}, MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({dt:.1f}s)"
        )
        results.append((name, r, net, n_rsi, n_atr, pf_g))

    total_time = time.time() - t0

    # 재현 체크
    baseline_name, baseline_r, baseline_net, *_ = results[0]
    print("\n" + "=" * 100)
    print("실험 5: v2 저비용 필터 결과")
    print("=" * 100)
    print(f"Period: {baseline_r.period}")
    print(f"총 소요: {total_time:.1f}초 (prep {t_prep:.1f}s + sim {total_time-t_prep:.1f}s)")
    print(f"\n## baseline 재현 (기대: PF 1.12, trades 782, net +2,742,920)")
    print(f"  실측: PF {format_pf(baseline_r.profit_factor)}, trades {baseline_r.total_trades}, net {baseline_net:+,.0f}")

    print("\n## 핵심 지표 비교")
    print(f"{'변형':<22} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
    print("-" * 100)
    for name, r, net, _, _, pf_g in results:
        payoff = compute_payoff(r.trades)
        payoff_s = f"{payoff:.2f}" if payoff == payoff else 'n/a'
        print(
            f"{name:<22} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{format_pf(r.profit_factor):>6} {format_pf(pf_g):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+12,.0f} {payoff_s:>7}"
        )

    print("\n## 필터 효과")
    print(f"{'변형':<22} {'RSI차단':>10} {'ATR밴드차단':>14} {'제거합':>10}")
    print("-" * 70)
    for name, r, _, n_rsi, n_atr, _ in results:
        print(f"{name:<22} {n_rsi:>10} {n_atr:>14} {n_rsi+n_atr:>10}")

    print("\n## 러너 보존 (16-25d / 26d+)")
    print(f"{'변형':<22} {'26d+건':>7} {'26d+손익':>14} {'16-25d건':>9} {'16-25d손익':>15}")
    print("-" * 80)
    all_buckets = {}
    for name, r, *_ in results:
        b = hold_bucket_stats(r.trades)
        all_buckets[name] = b
        pnl26, n26 = b['26d+']
        pnl16, n16 = b['16-25d']
        print(f"{name:<22} {n26:>7} {pnl26:>+14,.0f} {n16:>9} {pnl16:>+15,.0f}")

    # ── Best 선정: baseline 제외, PF 최고 (동률이면 net 높은 쪽) ──
    sizing_results = [(name, r, net, pf_g) for name, r, net, _, _, pf_g in results[1:]]
    best = max(sizing_results, key=lambda x: (x[1].profit_factor, x[2]))
    best_name, best_r, best_net, best_pf_g = best

    print(f"\n## 보유 기간별 순손익 (Best: {best_name})")
    print(f"{'구간':<10} {'Baseline PnL(건)':>22} {'Best PnL(건)':>22} {'변화':>15}")
    print("-" * 75)
    b_buckets = all_buckets[baseline_name]
    x_buckets = all_buckets[best_name]
    for key in ['1-5d', '6-10d', '11-15d', '16-25d', '26d+']:
        bp, bn = b_buckets[key]
        xp, xn = x_buckets[key]
        delta = xp - bp
        print(f"{key:<10} {bp:>+14,.0f} ({bn:>3}) {xp:>+14,.0f} ({xn:>3}) {delta:>+14,.0f}")

    print("\n## Exit Reason 분포 (Best)")
    best_trades = best_r.trades
    dist = defaultdict(int)
    for t in best_trades:
        dist[t.exit_reason] += 1
    for reason, n in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {reason:<18} {n:>4} ({n/len(best_trades):.1%})")

    print(f"\n## Best vs Baseline 요약 — Best: {best_name}")
    print(f"{'항목':<10} {'v2 baseline':<20} {'Best':<20} {'변화':<15}")
    print("-" * 70)
    print(f"{'PF':<10} {format_pf(baseline_r.profit_factor):<20} {format_pf(best_r.profit_factor):<20} "
          f"{best_r.profit_factor - baseline_r.profit_factor:+.2f}")
    print(f"{'PF(전)':<10} {format_pf(pf_gross(baseline_r.trades)):<20} {format_pf(best_pf_g):<20} "
          f"{best_pf_g - pf_gross(baseline_r.trades):+.2f}")
    print(f"{'CAGR':<10} {baseline_r.cagr_pct:<20.1%} {best_r.cagr_pct:<20.1%} "
          f"{(best_r.cagr_pct - baseline_r.cagr_pct)*100:+.1f}%p")
    print(f"{'MDD':<10} {baseline_r.max_drawdown_pct:<20.1%} {best_r.max_drawdown_pct:<20.1%} "
          f"{(best_r.max_drawdown_pct - baseline_r.max_drawdown_pct)*100:+.1f}%p")
    print(f"{'순손익':<10} {baseline_net:<+20,.0f} {best_net:<+20,.0f} {best_net - baseline_net:+,.0f}")
    print(f"{'건수':<10} {baseline_r.total_trades:<20} {best_r.total_trades:<20} {best_r.total_trades - baseline_r.total_trades:+}")

    print("\n## 판정")
    pf_up = best_r.profit_factor > baseline_r.profit_factor
    net_up = best_net > baseline_net
    print(f"  Best: {best_name}")
    print(f"  PF: {format_pf(baseline_r.profit_factor)} → {format_pf(best_r.profit_factor)}")
    print(f"  순손익: {baseline_net:+,} → {best_net:+,}")
    if pf_up and net_up:
        print(f"  ✅ 개선 — 필터 채택 권장")
    elif pf_up:
        print(f"  ⚠ PF만 개선 (net 하락) — 판단 유보")
    else:
        print(f"  ❌ 현행 유지 — 필터 효과 없음")


if __name__ == "__main__":
    run_all()

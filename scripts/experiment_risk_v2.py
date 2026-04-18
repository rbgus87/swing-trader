"""Phase 4-1b: 포지션 사이징 기반 리스크 관리 비교 백테스트.

v2 baseline (25% 고정) vs 4가지 사이징 변형.
precompute 1회 후 5회 시뮬.
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
    RiskParams,
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v0 import StrategyParams


def make_variants():
    base = dict(
        dd_normal_threshold=-0.15,
        dd_caution_threshold=-0.25,
        dd_recovery_threshold=-0.10,
        alloc_normal=0.25,
        alloc_caution=0.15,
        alloc_crisis=0.10,
        ticker_sl_cooldown=5,
        daily_loss_limit=-0.03,
    )
    return [
        ('v2 baseline', None),
        ('v2 + 사이징', RiskParams(
            **base,
            enable_sizing=True, enable_daily_loss=False, enable_ticker_cooldown=False,
        )),
        ('v2 + 사이징+재진입', RiskParams(
            **base,
            enable_sizing=True, enable_daily_loss=False, enable_ticker_cooldown=True,
        )),
        ('v2 + 사이징+일일', RiskParams(
            **base,
            enable_sizing=True, enable_daily_loss=True, enable_ticker_cooldown=False,
        )),
        ('v2 + 전체', RiskParams(
            **base,
            enable_sizing=True, enable_daily_loss=True, enable_ticker_cooldown=True,
        )),
    ]


def compute_payoff(trades):
    winners = [t.pnl_pct for t in trades if t.pnl_amount > 0]
    losers = [t.pnl_pct for t in trades if t.pnl_amount <= 0]
    if not winners or not losers:
        return float('nan')
    avg_win = sum(winners) / len(winners)
    avg_loss = abs(sum(losers) / len(losers))
    return avg_win / avg_loss if avg_loss > 0 else float('inf')


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


def yearly_pf(trades):
    by_year = defaultdict(lambda: {'gp': 0.0, 'gl': 0.0})
    for t in trades:
        yr = t.exit_date[:4]
        if t.pnl_amount > 0:
            by_year[yr]['gp'] += t.pnl_amount
        else:
            by_year[yr]['gl'] += abs(t.pnl_amount)
    out = {}
    for yr in sorted(by_year):
        gp = by_year[yr]['gp']
        gl = by_year[yr]['gl']
        out[yr] = gp / gl if gl > 0 else float('inf')
    return out


def format_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def run_all():
    params = StrategyParams()
    variants = make_variants()

    t0 = time.time()
    logger.info("=" * 60)
    logger.info("데이터 로드 + precompute (1회)")
    logger.info("=" * 60)

    preloaded = load_backtest_data(params)
    precomp = precompute_daily_signals(
        preloaded['trading_dates'],
        preloaded['ticker_data'],
        preloaded['ticker_date_idx'],
        preloaded['initial_universe'],
        params,
    )

    t_prep = time.time() - t0
    logger.info(f"준비 완료 ({t_prep:.1f}초)")

    results = []
    for name, risk in variants:
        logger.info(f"\n--- {name} ---")
        t1 = time.time()
        r = run_portfolio_backtest(
            initial_capital=5_000_000,
            max_positions=4,
            params=params,
            preloaded_data=preloaded,
            precomputed=precomp,
            risk=risk,
        )
        dt = time.time() - t1
        net_pnl = r.final_capital - r.initial_capital
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={format_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net_pnl:+,.0f}원 ({dt:.1f}s)"
        )
        results.append((name, r, net_pnl))

    total_time = time.time() - t0

    baseline_name, baseline_r, baseline_net = results[0]

    print("\n" + "=" * 100)
    print("Phase 4-1b 포지션 사이징 리스크 결과")
    print("=" * 100)
    print(f"Period: {baseline_r.period}")
    print(f"총 소요: {total_time:.1f}초 (prep {t_prep:.1f}s + sim {total_time-t_prep:.1f}s)")

    # baseline 재현 체크 (이번 세션에서 v2 확정 시 측정: PF 1.12 / 782건 / net +2,742,920)
    print(f"\n## baseline 재현 검증 (기대: PF 1.12, trades 782, net +2,742,920)")
    print(f"  실측: PF {format_pf(baseline_r.profit_factor)}, trades {baseline_r.total_trades}, "
          f"net {baseline_net:+,.0f}")
    baseline_ok = (
        abs(baseline_r.profit_factor - 1.12) < 0.01
        and baseline_r.total_trades == 782
        and abs(baseline_net - 2_742_920) < 1000
    )
    print(f"  → {'✓ 재현' if baseline_ok else '⚠ 불일치'}")

    print("\n## 핵심 지표 비교")
    print(f"{'변형':<22} {'건수':>5} {'승률':>6} {'PF':>6} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
    print("-" * 100)
    for name, r, net in results:
        payoff = compute_payoff(r.trades)
        payoff_s = f"{payoff:.2f}" if payoff == payoff else 'n/a'
        print(
            f"{name:<22} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{format_pf(r.profit_factor):>6} {r.cagr_pct:>6.1%} "
            f"{r.max_drawdown_pct:>6.1%} {net:>+12,.0f} {payoff_s:>7}"
        )

    print("\n## 사이징 레벨 발동 (거래일수)")
    print(f"{'변형':<22} {'정상':>7} {'경계':>7} {'위기':>7} {'재진입차단':>10} {'일일한도':>9}")
    print("-" * 80)
    for name, r, _ in results:
        print(
            f"{name:<22} "
            f"{getattr(r, 'dd_normal_days', 0):>7} "
            f"{getattr(r, 'dd_caution_days', 0):>7} "
            f"{getattr(r, 'dd_crisis_days', 0):>7} "
            f"{getattr(r, 'ticker_cooldown_block_count', 0):>10} "
            f"{getattr(r, 'daily_loss_trigger_count', 0):>9}"
        )

    print("\n## 러너 보존 검증 (16-25d / 26d+)")
    print(f"{'변형':<22} {'26d+ 건':>8} {'26d+ 손익':>14} {'16-25d 건':>10} {'16-25d 손익':>15}")
    print("-" * 80)
    all_buckets = {}
    for name, r, _ in results:
        b = hold_bucket_stats(r.trades)
        all_buckets[name] = b
        pnl_26, n_26 = b['26d+']
        pnl_16, n_16 = b['16-25d']
        print(
            f"{name:<22} {n_26:>8} {pnl_26:>+14,.0f} {n_16:>10} {pnl_16:>+15,.0f}"
        )

    # ── Best 선정: 사이징 변형 중 PF ≥ 1.0 & MDD 최저. 없으면 PF 최고 ──
    sizing_results = [(n, r, net) for n, r, net in results[1:]]
    qualified = [(n, r, net) for n, r, net in sizing_results if r.profit_factor >= 1.0]
    if qualified:
        best = min(qualified, key=lambda x: x[1].max_drawdown_pct)
    else:
        best = max(sizing_results, key=lambda x: x[1].profit_factor)

    print(f"\n## 보유 기간별 순손익 상세 (Best: {best[0]})")
    print(f"{'구간':<10} {'v2 PnL(건)':>20} {'Best PnL(건)':>22} {'변화':>15}")
    print("-" * 75)
    b_buckets = all_buckets[baseline_name]
    x_buckets = all_buckets[best[0]]
    for key in ['1-5d', '6-10d', '11-15d', '16-25d', '26d+']:
        b_pnl, b_n = b_buckets[key]
        x_pnl, x_n = x_buckets[key]
        delta = x_pnl - b_pnl
        print(
            f"{key:<10} {b_pnl:>+12,.0f} ({b_n:>3}) {x_pnl:>+14,.0f} ({x_n:>3}) {delta:>+14,.0f}"
        )

    # 연도별 PF
    b_yr = yearly_pf(baseline_r.trades)
    x_yr = yearly_pf(best[1].trades)
    years = sorted(set(b_yr) | set(x_yr))
    print(f"\n## 연도별 PF (Best: {best[0]})")
    print(f"{'연도':<8} {'v2':>8} {'Best':>8}")
    print("-" * 28)
    for yr in years:
        b = b_yr.get(yr, float('nan'))
        x = x_yr.get(yr, float('nan'))
        b_s = format_pf(b) if b == b else '-'
        x_s = format_pf(x) if x == x else '-'
        print(f"{yr:<8} {b_s:>8} {x_s:>8}")

    print(f"\n## Best vs Baseline 요약 — Best: {best[0]}")
    print(f"{'항목':<10} {'v2 baseline':<20} {'Best':<20} {'변화':<15}")
    print("-" * 70)
    print(f"{'PF':<10} {format_pf(baseline_r.profit_factor):<20} {format_pf(best[1].profit_factor):<20} "
          f"{best[1].profit_factor - baseline_r.profit_factor:+.2f}")
    print(f"{'CAGR':<10} {baseline_r.cagr_pct:<20.1%} {best[1].cagr_pct:<20.1%} "
          f"{(best[1].cagr_pct - baseline_r.cagr_pct)*100:+.1f}%p")
    print(f"{'MDD':<10} {baseline_r.max_drawdown_pct:<20.1%} {best[1].max_drawdown_pct:<20.1%} "
          f"{(best[1].max_drawdown_pct - baseline_r.max_drawdown_pct)*100:+.1f}%p")
    print(f"{'순손익':<10} {baseline_net:<+20,.0f} {best[2]:<+20,.0f} {best[2] - baseline_net:+,.0f}")

    b_26, _ = b_buckets['26d+']
    x_26_pnl, x_26_n = x_buckets['26d+']
    b_26_n = b_buckets['26d+'][1]
    print(f"\n## 판정")
    ok_mdd = best[1].max_drawdown_pct <= baseline_r.max_drawdown_pct  # baseline 대비 개선
    ok_pf = best[1].profit_factor >= 1.0
    runner_preserved = x_26_n >= b_26_n * 0.8  # 80% 이상 보존
    if ok_mdd and ok_pf and runner_preserved:
        verdict = "✅ MDD 축소 + PF 유지 + 러너 보존"
    elif ok_mdd and ok_pf:
        verdict = f"⚠ MDD/PF 달성, 러너 손실 ({x_26_n}건 vs baseline {b_26_n}건)"
    elif ok_pf:
        verdict = f"⚠ PF 유지하나 MDD 미개선 (baseline {baseline_r.max_drawdown_pct:.1%} → {best[1].max_drawdown_pct:.1%})"
    else:
        verdict = "❌ PF 1.0 미달"
    print(f"  Best: {best[0]}")
    print(f"  MDD 개선: {baseline_r.max_drawdown_pct:.1%} → {best[1].max_drawdown_pct:.1%}")
    print(f"  PF 유지: {format_pf(baseline_r.profit_factor)} → {format_pf(best[1].profit_factor)}")
    print(f"  러너 보존: 26d+ {x_26_n}건 (baseline {b_26_n}건)")
    print(f"  {verdict}")


if __name__ == "__main__":
    run_all()

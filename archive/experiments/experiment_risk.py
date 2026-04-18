"""Phase 4-1: 리스크 관리 규칙 on/off 비교 백테스트.

v2 baseline (리스크 없음) 대비 4가지 변형을 비교:
  1) 전체 리스크
  2) MDD 한도만
  3) 연속 SL 쿨다운만
  4) MDD + 연속 SL

precompute 1회 후 5회 시뮬 반복.
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
    """실험 변형 5개 리턴."""
    base_kwargs = dict(
        mdd_pause_threshold=-0.20,
        mdd_stop_threshold=-0.25,
        mdd_resume_threshold=-0.10,
        mdd_stop_halt_days=20,
        consecutive_sl_limit=3,
        consecutive_sl_cooldown=5,
        daily_loss_limit=-0.03,
        ticker_sl_cooldown=10,
    )
    return [
        ('v2 (리스크 없음)', None),
        ('v2 + 전체', RiskParams(
            **base_kwargs,
            enable_mdd=True, enable_consecutive_sl=True,
            enable_daily_loss=True, enable_ticker_cooldown=True,
        )),
        ('v2 + MDD만', RiskParams(
            **base_kwargs,
            enable_mdd=True, enable_consecutive_sl=False,
            enable_daily_loss=False, enable_ticker_cooldown=False,
        )),
        ('v2 + 연속SL만', RiskParams(
            **base_kwargs,
            enable_mdd=False, enable_consecutive_sl=True,
            enable_daily_loss=False, enable_ticker_cooldown=False,
        )),
        ('v2 + MDD+연속SL', RiskParams(
            **base_kwargs,
            enable_mdd=True, enable_consecutive_sl=True,
            enable_daily_loss=False, enable_ticker_cooldown=False,
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
    """보유 기간별 순손익 + 거래수."""
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
    """연도별 PF."""
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
    logger.info("데이터 로드 + 지표 계산 + precompute (1회)")
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
        logger.info(f"\n--- 시뮬: {name} ---")
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

    # ── 출력 ──
    print("\n" + "=" * 90)
    print("Phase 4-1 리스크 관리 백테스트 결과")
    print("=" * 90)
    print(f"Period: {results[0][1].period}")
    print(f"총 소요: {total_time:.1f}초 (prep {t_prep:.1f}s + sim {total_time-t_prep:.1f}s)")

    print("\n## 핵심 지표 비교")
    print(f"{'변형':<22} {'건수':>6} {'승률':>6} {'PF':>6} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
    print("-" * 90)
    for name, r, net in results:
        payoff = compute_payoff(r.trades)
        payoff_s = f"{payoff:.2f}" if payoff == payoff else 'n/a'
        print(
            f"{name:<22} {r.total_trades:>6} {r.win_rate:>5.1%} "
            f"{format_pf(r.profit_factor):>6} {r.cagr_pct:>6.1%} "
            f"{r.max_drawdown_pct:>6.1%} {net:>+12,.0f} {payoff_s:>7}"
        )

    print("\n## 리스크 규칙 발동 통계")
    print(f"{'변형':<22} {'MDD pause':>10} {'MDD stop':>9} {'SL cooldown':>12} {'일일한도':>9} {'재진입차단':>10}")
    print("-" * 90)
    for name, r, _ in results:
        print(
            f"{name:<22} "
            f"{getattr(r, 'mdd_pause_trigger_count', 0):>10} "
            f"{getattr(r, 'mdd_stop_trigger_count', 0):>9} "
            f"{getattr(r, 'sl_cooldown_trigger_count', 0):>12} "
            f"{getattr(r, 'daily_loss_trigger_count', 0):>9} "
            f"{getattr(r, 'ticker_cooldown_block_count', 0):>10}"
        )

    # ── Best 선정: PF >= 1.0 중 MDD 최저, 없으면 Sharpe 대용 (CAGR/MDD) 최대 ──
    candidates = [(n, r, net) for n, r, net in results if n != 'v2 (리스크 없음)']
    qualified = [(n, r, net) for n, r, net in candidates if r.profit_factor >= 1.0]
    if qualified:
        best = min(qualified, key=lambda x: x[1].max_drawdown_pct)
    else:
        best = max(candidates, key=lambda x: (x[1].cagr_pct / x[1].max_drawdown_pct if x[1].max_drawdown_pct > 0 else 0))

    baseline = results[0]

    # 보유 기간별 순손익 비교
    baseline_buckets = hold_bucket_stats(baseline[1].trades)
    best_buckets = hold_bucket_stats(best[1].trades)

    print(f"\n## 보유 기간별 순손익 (Best: {best[0]})")
    print(f"{'구간':<10} {'v2 PnL(건)':>20} {'Best PnL(건)':>22} {'변화':>15}")
    print("-" * 75)
    for key in ['1-5d', '6-10d', '11-15d', '16-25d', '26d+']:
        b_pnl, b_n = baseline_buckets[key]
        x_pnl, x_n = best_buckets[key]
        delta = x_pnl - b_pnl
        print(
            f"{key:<10} {b_pnl:>+12,.0f} ({b_n:>3}) {x_pnl:>+14,.0f} ({x_n:>3}) {delta:>+14,.0f}"
        )

    # 연도별 PF
    baseline_yr = yearly_pf(baseline[1].trades)
    best_yr = yearly_pf(best[1].trades)
    all_years = sorted(set(baseline_yr) | set(best_yr))

    print(f"\n## 연도별 PF (Best: {best[0]})")
    print(f"{'연도':<8} {'v2':>8} {'Best':>8}")
    print("-" * 28)
    for yr in all_years:
        b = baseline_yr.get(yr, float('nan'))
        x = best_yr.get(yr, float('nan'))
        b_s = format_pf(b) if b == b else '-'
        x_s = format_pf(x) if x == x else '-'
        print(f"{yr:<8} {b_s:>8} {x_s:>8}")

    print(f"\n## Best vs Baseline 요약 — Best: {best[0]}")
    print(f"{'항목':<10} {'v2 (리스크 없음)':<20} {'Best':<20} {'변화':<15}")
    print("-" * 70)
    print(f"{'PF':<10} {format_pf(baseline[1].profit_factor):<20} {format_pf(best[1].profit_factor):<20} "
          f"{best[1].profit_factor - baseline[1].profit_factor:+.2f}")
    print(f"{'CAGR':<10} {baseline[1].cagr_pct:<20.1%} {best[1].cagr_pct:<20.1%} "
          f"{(best[1].cagr_pct - baseline[1].cagr_pct)*100:+.1f}%p")
    print(f"{'MDD':<10} {baseline[1].max_drawdown_pct:<20.1%} {best[1].max_drawdown_pct:<20.1%} "
          f"{(best[1].max_drawdown_pct - baseline[1].max_drawdown_pct)*100:+.1f}%p")
    print(f"{'순손익':<10} {baseline[2]:<+20,.0f} {best[2]:<+20,.0f} "
          f"{best[2] - baseline[2]:+,.0f}")

    print("\n## 판정")
    ok_mdd = best[1].max_drawdown_pct <= 0.25
    ok_pf = best[1].profit_factor >= 1.0
    if ok_mdd and ok_pf:
        verdict = "✅ MDD≤25% + PF≥1.0 달성"
    elif ok_pf:
        verdict = f"⚠ PF 유지 but MDD={best[1].max_drawdown_pct:.1%} (목표 25% 미달)"
    elif ok_mdd:
        verdict = f"⚠ MDD 달성 but PF={best[1].profit_factor:.2f} (<1.0)"
    else:
        verdict = "❌ PF/MDD 목표 모두 미달"
    print(f"  Best: {best[0]}")
    print(f"  MDD 개선: {baseline[1].max_drawdown_pct:.1%} → {best[1].max_drawdown_pct:.1%}")
    print(f"  PF 유지: {format_pf(baseline[1].profit_factor)} → {format_pf(best[1].profit_factor)}")
    print(f"  {verdict}")


if __name__ == "__main__":
    run_all()

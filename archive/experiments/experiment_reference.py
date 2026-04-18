"""실험 6: 레퍼런스 눌림목 전략 독립 백테스트.

v2 돌파 vs 눌림목(레퍼런스 청산) vs 눌림목(v2 청산).
precompute는 전략별로 별도 수행 (진입 로직이 다름).
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
    precompute_pullback_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v0 import StrategyParams


TOTAL_COST_PCT = 0.0031


def compute_payoff(trades):
    winners = [t.pnl_pct for t in trades if t.pnl_amount > 0]
    losers = [t.pnl_pct for t in trades if t.pnl_amount <= 0]
    if not winners or not losers:
        return float('nan')
    return (sum(winners) / len(winners)) / abs(sum(losers) / len(losers))


def pf_gross(trades):
    gp = gl = 0.0
    for t in trades:
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if g > 0:
            gp += g
        else:
            gl += abs(g)
    return gp / gl if gl > 0 else float('inf')


def hold_buckets(trades):
    b = {'1-5d': [0, 0], '6-10d': [0, 0], '11-15d': [0, 0], '16-25d': [0, 0], '26d+': [0, 0]}
    for t in trades:
        hd = t.hold_days
        if hd <= 5:
            k = '1-5d'
        elif hd <= 10:
            k = '6-10d'
        elif hd <= 15:
            k = '11-15d'
        elif hd <= 25:
            k = '16-25d'
        else:
            k = '26d+'
        b[k][0] += t.pnl_amount
        b[k][1] += 1
    return b


def yearly_pf(trades):
    by = defaultdict(lambda: {'gp': 0.0, 'gl': 0.0})
    for t in trades:
        yr = t.exit_date[:4]
        if t.pnl_amount > 0:
            by[yr]['gp'] += t.pnl_amount
        else:
            by[yr]['gl'] += abs(t.pnl_amount)
    return {y: (by[y]['gp'] / by[y]['gl'] if by[y]['gl'] > 0 else float('inf'))
            for y in sorted(by)}


def exit_dist(trades):
    d = defaultdict(int)
    for t in trades:
        d[t.exit_reason] += 1
    return d


def format_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def run_all():
    t0 = time.time()

    # ── 공용 데이터 로드 ──
    params_base = StrategyParams()
    logger.info("공용 데이터 로드 + 지표 계산 (1회)")
    preloaded = load_backtest_data(params_base)

    # v2 돌파 precompute
    logger.info("v2 돌파 precompute")
    precomp_v2 = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'], params_base,
    )
    # 눌림목 precompute
    logger.info("눌림목 precompute")
    precomp_pullback = precompute_pullback_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'], params_base,
    )
    t_prep = time.time() - t0
    logger.info(f"준비 완료 ({t_prep:.1f}초)")

    # ── 파라미터 세트 ──
    params_v2 = StrategyParams()  # SL2.0/Trail4.0/Hold20/TP1 0.5
    params_ref = replace(
        params_base,
        stop_loss_atr=1.5,
        take_profit_atr=2.0,
        trailing_atr=3.0,
        max_hold_days=10,
        tp1_sell_ratio=0.3,
    )

    atr_risk = RiskParams(
        enable_sizing=False, enable_atr_sizing=True,
        enable_daily_loss=False, enable_ticker_cooldown=False,
        atr_sizing_risk_pct=0.02, atr_sizing_max_pct=0.30,
    )

    variants = [
        ('v2 돌파',              params_v2,  precomp_v2,       None),
        ('레퍼런스 눌림목',         params_ref, precomp_pullback, atr_risk),
        ('눌림목+v2청산',         params_v2,  precomp_pullback, atr_risk),
    ]

    results = []
    for name, p, pcomp, risk in variants:
        logger.info(f"\n--- {name} ---")
        t1 = time.time()
        r = run_portfolio_backtest(
            initial_capital=5_000_000,
            max_positions=4,
            params=p,
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
        results.append((name, r, net, pf_g))

    total_time = time.time() - t0

    # ── 출력 ──
    baseline_name, baseline_r, baseline_net, baseline_pfg = results[0]
    print("\n" + "=" * 100)
    print("실험 6: 레퍼런스 눌림목 vs v2 돌파")
    print("=" * 100)
    print(f"Period: {baseline_r.period}")
    print(f"총 소요: {total_time:.1f}초 (prep {t_prep:.1f}s + sim {total_time-t_prep:.1f}s)")

    print("\n## 핵심 지표 비교")
    print(f"{'전략':<22} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
    print("-" * 100)
    for name, r, net, pf_g in results:
        payoff = compute_payoff(r.trades)
        payoff_s = f"{payoff:.2f}" if payoff == payoff else 'n/a'
        print(
            f"{name:<22} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{format_pf(r.profit_factor):>6} {format_pf(pf_g):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+12,.0f} {payoff_s:>7}"
        )

    print("\n## 보유 기간별 순손익 (건수)")
    print(f"{'전략':<22} {'1-5d':>15} {'6-10d':>15} {'11-15d':>15} {'16-25d':>15} {'26d+':>15}")
    print("-" * 100)
    for name, r, *_ in results:
        b = hold_buckets(r.trades)
        cells = []
        for k in ['1-5d', '6-10d', '11-15d', '16-25d', '26d+']:
            pnl, n = b[k]
            cells.append(f"{pnl:>+9,.0f}({n:>3})")
        print(f"{name:<22} " + " ".join(f"{c:>15}" for c in cells))

    print("\n## Exit Reason 분포")
    reasons = ['STOP_LOSS', 'TAKE_PROFIT_1', 'TRAILING', 'TREND_EXIT', 'TIME_EXIT', 'FINAL_CLOSE']
    print(f"{'전략':<22} " + " ".join(f"{r:>13}" for r in reasons))
    print("-" * 120)
    for name, r, *_ in results:
        d = exit_dist(r.trades)
        total = r.total_trades
        cells = []
        for reason in reasons:
            n = d.get(reason, 0)
            pct = n / total if total else 0
            cells.append(f"{n:>4}({pct:>5.1%})")
        print(f"{name:<22} " + " ".join(f"{c:>13}" for c in cells))

    # 연도별 PF
    all_years = set()
    yr_maps = []
    for name, r, *_ in results:
        ym = yearly_pf(r.trades)
        yr_maps.append((name, ym))
        all_years |= set(ym)
    print("\n## 연도별 PF")
    print(f"{'연도':<6} " + "".join(f"{n:>22}" for n, _ in yr_maps))
    print("-" * (6 + 22 * len(yr_maps)))
    for yr in sorted(all_years):
        cells = []
        for _, ym in yr_maps:
            v = ym.get(yr, float('nan'))
            cells.append(format_pf(v) if v == v else '-')
        print(f"{yr:<6} " + "".join(f"{c:>22}" for c in cells))

    # 종목 중복도: (ticker) set 비교
    v2_tickers = set(t.ticker for t in results[0][1].trades)
    pb_tickers = set(t.ticker for t in results[1][1].trades)
    overlap = v2_tickers & pb_tickers
    union = v2_tickers | pb_tickers
    overlap_pct = len(overlap) / min(len(v2_tickers), len(pb_tickers)) if v2_tickers and pb_tickers else 0

    # (ticker, entry_date) 쌍 중복도
    v2_pairs = set((t.ticker, t.entry_date) for t in results[0][1].trades)
    pb_pairs = set((t.ticker, t.entry_date) for t in results[1][1].trades)
    pair_overlap = v2_pairs & pb_pairs
    pair_pct = len(pair_overlap) / min(len(v2_pairs), len(pb_pairs)) if v2_pairs and pb_pairs else 0

    print(f"\n## 종목 중복도 (v2 돌파 vs 레퍼런스 눌림목)")
    print(f"  v2 진입 티커 수: {len(v2_tickers)}")
    print(f"  눌림목 진입 티커 수: {len(pb_tickers)}")
    print(f"  티커 교집합: {len(overlap)} / 합집합 {len(union)}")
    print(f"  티커 중복률 (min 기준): {overlap_pct:.1%}")
    print(f"  (티커, 진입일) 쌍 중복: {len(pair_overlap)} / min({len(v2_pairs)},{len(pb_pairs)}) = {pair_pct:.1%}")

    ref = results[1]
    print(f"\n## 판정")
    print(f"  레퍼런스 PF: {format_pf(ref[1].profit_factor)} (v2 돌파 {format_pf(baseline_r.profit_factor)})")
    print(f"  레퍼런스 CAGR: {ref[1].cagr_pct:.1%} (v2 {baseline_r.cagr_pct:.1%})")
    print(f"  레퍼런스 MDD: {ref[1].max_drawdown_pct:.1%} (v2 {baseline_r.max_drawdown_pct:.1%})")
    print(f"  레퍼런스 순손익: {ref[2]:+,.0f} (v2 {baseline_net:+,.0f})")

    pf_better = ref[1].profit_factor >= baseline_r.profit_factor + 0.05
    pf_similar = abs(ref[1].profit_factor - baseline_r.profit_factor) < 0.05
    if pf_better:
        verdict_v2 = "✅ 우위"
    elif pf_similar:
        verdict_v2 = "⚠ 비슷"
    else:
        verdict_v2 = "❌ 열세"
    print(f"  v2 대비: {verdict_v2}")

    if pair_pct < 0.10:
        comp_verdict = "높음 (독립 알파 가능성)"
    elif pair_pct < 0.30:
        comp_verdict = "중간"
    else:
        comp_verdict = "낮음 (대체 관계)"
    print(f"  보완 가능성: 쌍 중복률 {pair_pct:.1%} → {comp_verdict}")


if __name__ == "__main__":
    run_all()

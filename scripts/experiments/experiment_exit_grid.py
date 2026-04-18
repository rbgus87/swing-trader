"""실험 2: SL × Trail × Hold 그리드 — 청산 구조 동시 최적화.

실험 1: SL 단독 조정은 zero-sum (STOP↓=TRAIL↑).
가설: SL+Trail+Hold 동시 확대 시 수익 러너 확보 → PF 개선.

그리드: SL [1.5,2.0,2.5] × Trail [3.0,4.0,5.0,6.0] × Hold [15,20,30] = 36 조합.
TP1 = 2.0 고정 (실험 3 별도).

캐싱: load_backtest_data() 1회 → 36회 시뮬만 반복.
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, Exception):
    pass

import numpy as np
from collections import defaultdict
from loguru import logger

from src.backtest.portfolio_backtester import (
    run_portfolio_backtest,
    load_backtest_data,
)
from src.backtest.swing_backtester import CostModel
from src.strategy.trend_following_v0 import StrategyParams


SL_VALUES = [1.5, 2.0, 2.5]
TRAIL_VALUES = [3.0, 4.0, 5.0, 6.0]
HOLD_VALUES = [15, 20, 30]

BASELINE = (1.5, 3.0, 15)


def extract(result, sl, tr, hd):
    trades = result.trades
    cost_pct = CostModel().total_cost_pct()
    winners = [t for t in trades if t.pnl_amount > 0]
    losers = [t for t in trades if t.pnl_amount <= 0]
    avg_w = np.mean([t.pnl_pct for t in winners]) if winners else 0
    avg_l = np.mean([t.pnl_pct for t in losers]) if losers else 0
    payoff = abs(avg_w / avg_l) if avg_l != 0 else 0

    pnl_bc = [(t.pnl_pct + cost_pct) for t in trades]
    gp = sum(p for p in pnl_bc if p > 0)
    gl = abs(sum(p for p in pnl_bc if p <= 0))
    pf_bc = gp / gl if gl > 0 else float('inf')

    reasons = defaultdict(int)
    for t in trades:
        reasons[t.exit_reason] += 1

    hold_net = {
        '1-2d': (sum(t.pnl_amount for t in trades if t.hold_days <= 2),
                 sum(1 for t in trades if t.hold_days <= 2)),
        '3-5d': (sum(t.pnl_amount for t in trades if 3 <= t.hold_days <= 5),
                 sum(1 for t in trades if 3 <= t.hold_days <= 5)),
        '6-10d': (sum(t.pnl_amount for t in trades if 6 <= t.hold_days <= 10),
                  sum(1 for t in trades if 6 <= t.hold_days <= 10)),
        '11-15d': (sum(t.pnl_amount for t in trades if 11 <= t.hold_days <= 15),
                   sum(1 for t in trades if 11 <= t.hold_days <= 15)),
        '16-25d': (sum(t.pnl_amount for t in trades if 16 <= t.hold_days <= 25),
                   sum(1 for t in trades if 16 <= t.hold_days <= 25)),
        '26d+': (sum(t.pnl_amount for t in trades if t.hold_days > 25),
                 sum(1 for t in trades if t.hold_days > 25)),
    }

    return {
        'sl': sl, 'tr': tr, 'hd': hd,
        'trades': result.total_trades,
        'wr': result.win_rate,
        'pf': result.profit_factor,
        'pf_bc': pf_bc,
        'cagr': result.cagr_pct,
        'mdd': result.max_drawdown_pct,
        'net': sum(t.pnl_amount for t in trades),
        'final': result.final_capital,
        'payoff': payoff,
        'reasons': dict(reasons),
        'hold_net': hold_net,
    }


def fmt_pf(x, w=5):
    if x == float('inf'):
        return f"{'inf':>{w}}"
    return f"{x:>{w}.2f}"


def print_report(results, t_load, t_sims):
    total_sim = sum(t_sims)
    avg_sim = total_sim / len(t_sims)

    print("\n" + "=" * 110)
    print("  실험 2: SL × Trail × Hold 그리드 — 청산 구조 동시 최적화")
    print("=" * 110)

    print("\n■ 성능")
    print(f"  데이터 로딩:  {t_load:.1f}초 (1회)")
    print(f"  시뮬레이션:  {len(t_sims)}회 × 평균 {avg_sim:.1f}초 = {total_sim:.1f}초")
    print(f"  총 소요:     {t_load + total_sim:.1f}초")

    # PF 상위 10
    sorted_results = sorted(results, key=lambda r: r['pf'], reverse=True)
    print("\n■ 전체 그리드 결과 (PF 상위 10)")
    print(f"  {'순위':>3} {'SL':>4} {'Trail':>5} {'Hold':>4} {'건수':>5} {'승률':>6} "
          f"{'PF':>5} {'PF(전)':>6} {'CAGR':>7} {'MDD':>7} {'순손익':>12} {'Payoff':>6}")
    print("  " + "-" * 95)
    for i, r in enumerate(sorted_results[:10], 1):
        print(f"  {i:>3} {r['sl']:>4.1f} {r['tr']:>5.1f} {r['hd']:>4} "
              f"{r['trades']:>5} {r['wr']:>5.1%} {fmt_pf(r['pf'])} "
              f"{fmt_pf(r['pf_bc'], 6)} {r['cagr']:>+6.1%} {r['mdd']:>6.1%} "
              f"{r['net']:>+12,.0f} {r['payoff']:>6.2f}")

    # 히트맵 (Hold별)
    print("\n■ 전체 그리드 히트맵 (PF)")
    for hd in HOLD_VALUES:
        print(f"\n  [Hold={hd}]")
        header = f"  {'SL\\Trail':<10}"
        for tr in TRAIL_VALUES:
            header += f" {tr:>7.1f}"
        print(header)
        print("  " + "-" * (10 + 8 * len(TRAIL_VALUES)))
        for sl in SL_VALUES:
            line = f"  SL {sl:<7.1f}"
            for tr in TRAIL_VALUES:
                r = next((x for x in results if x['sl'] == sl and x['tr'] == tr and x['hd'] == hd), None)
                if r:
                    line += f" {fmt_pf(r['pf'], 7)}"
                else:
                    line += f" {'N/A':>7}"
            print(line)

    # Baseline vs Best 상세
    baseline = next(r for r in results if (r['sl'], r['tr'], r['hd']) == BASELINE)
    best = sorted_results[0]

    print("\n■ Best vs Baseline 상세 비교")
    print(f"  {'항목':<15} {'Baseline (1.5/3.0/15)':>22} {'Best ('+str(best['sl'])+'/'+str(best['tr'])+'/'+str(best['hd'])+')':>22} {'변화':>10}")
    print("  " + "-" * 75)
    for label, key, fmt in [
        ('Trades', 'trades', '{:>22}'),
        ('WR', 'wr', '{:>21.1%}'),
        ('PF', 'pf', '{:>22.2f}'),
        ('PF(비용전)', 'pf_bc', '{:>22.2f}'),
        ('CAGR', 'cagr', '{:>+21.1%}'),
        ('MDD', 'mdd', '{:>21.1%}'),
        ('순손익', 'net', '{:>+22,.0f}'),
        ('Payoff', 'payoff', '{:>22.2f}'),
    ]:
        b_val = fmt.format(baseline[key])
        bs_val = fmt.format(best[key])
        diff = best[key] - baseline[key]
        if isinstance(diff, float):
            if 'pct' in key or key in ('wr', 'cagr', 'mdd'):
                diff_s = f"{diff:>+9.1%}"
            elif key == 'net':
                diff_s = f"{diff:>+10,.0f}"
            else:
                diff_s = f"{diff:>+10.2f}"
        else:
            diff_s = f"{diff:>+10}"
        print(f"  {label:<15} {b_val} {bs_val} {diff_s}")

    # Exit Reason
    print(f"\n  Exit Reason 비교:")
    print(f"  {'Reason':<15} {'Baseline':>10} {'Best':>10}")
    for r_name in ['STOP_LOSS', 'TAKE_PROFIT_1', 'TRAILING', 'TREND_EXIT', 'TIME_EXIT', 'FINAL_CLOSE']:
        b = baseline['reasons'].get(r_name, 0)
        bs = best['reasons'].get(r_name, 0)
        print(f"  {r_name:<15} {b:>10} {bs:>10}")

    # 보유 기간별
    print("\n■ 보유 기간별 비교 (Best vs Baseline)")
    print(f"  {'구간':<10} {'Baseline PnL(건)':>25} {'Best PnL(건)':>25}")
    print("  " + "-" * 65)
    for bucket in ['1-2d', '3-5d', '6-10d', '11-15d', '16-25d', '26d+']:
        b_net, b_cnt = baseline['hold_net'][bucket]
        bs_net, bs_cnt = best['hold_net'][bucket]
        print(f"  {bucket:<10} {b_net:>+18,.0f}({b_cnt:>3}) {bs_net:>+18,.0f}({bs_cnt:>3})")

    # 판정
    pf_improvement = best['pf'] - baseline['pf']
    print("\n" + "=" * 110)
    print(f"  Best 조합:   SL {best['sl']} / Trail {best['tr']} / Hold {best['hd']}")
    print(f"  PF 개선:     {baseline['pf']:.2f} → {best['pf']:.2f}  ({pf_improvement:+.2f})")

    if best['pf'] >= 1.2:
        verdict = "✅ PF≥1.2 달성"
    elif best['pf'] > baseline['pf']:
        verdict = "⚠ 개선되었으나 PF<1.2"
    else:
        verdict = "❌ 개선 실패"
    print(f"  판정:        {verdict}")
    print("=" * 110)


def main():
    logger.info("=" * 60)
    logger.info("실험 2: SL × Trail × Hold 그리드 (36 조합)")
    logger.info("=" * 60)

    t0 = time.time()
    cache = load_backtest_data()
    t_load = time.time() - t0
    logger.info(f"Data loaded in {t_load:.1f}s")

    results = []
    t_sims = []

    combos = [(sl, tr, hd) for sl in SL_VALUES for tr in TRAIL_VALUES for hd in HOLD_VALUES]
    for i, (sl, tr, hd) in enumerate(combos, 1):
        t0 = time.time()
        params = StrategyParams(
            stop_loss_atr=sl,
            trailing_atr=tr,
            max_hold_days=hd,
        )
        result = run_portfolio_backtest(
            initial_capital=5_000_000,
            max_positions=4,
            params=params,
            preloaded_data=cache,
        )
        elapsed = time.time() - t0
        t_sims.append(elapsed)
        m = extract(result, sl, tr, hd)
        results.append(m)
        logger.info(
            f"[{i:>2}/{len(combos)}] SL={sl}/Tr={tr}/Hd={hd} | "
            f"trades={m['trades']} WR={m['wr']:.1%} "
            f"PF={m['pf']:.2f} CAGR={m['cagr']:+.1%} MDD={m['mdd']:.1%} "
            f"| {elapsed:.1f}s"
        )

    print_report(results, t_load, t_sims)


if __name__ == '__main__':
    main()

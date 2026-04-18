"""실험 3: TP1 구조 재설계 — 실험 2 Best(2.0/4.0/20) 위에서.

고정: SL 2.0, Trail 4.0, Hold 20.
변형: TP1 배수(2.0/3.0/4.0/off) + 분할 비율(50%/30%).

목표: PF 1.12 → 1.2+.
러너 보존률 변화가 핵심.
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
    precompute_daily_signals,
)
from src.backtest.swing_backtester import CostModel
from src.strategy.trend_following_v0 import StrategyParams


VARIANTS = [
    ('TP1 2.0 / 50% (현행)',  2.0, 0.5),
    ('TP1 3.0 / 50%',         3.0, 0.5),
    ('TP1 4.0 / 50%',         4.0, 0.5),
    ('TP1 2.0 / 30%',         2.0, 0.3),
    ('TP1 없음',              0.0, 0.0),
]

BASE_SL = 2.0
BASE_TRAIL = 4.0
BASE_HOLD = 20


def extract(result, label):
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
        '1-5d': [sum(t.pnl_amount for t in trades if t.hold_days <= 5),
                 sum(1 for t in trades if t.hold_days <= 5)],
        '6-10d': [sum(t.pnl_amount for t in trades if 6 <= t.hold_days <= 10),
                  sum(1 for t in trades if 6 <= t.hold_days <= 10)],
        '11-15d': [sum(t.pnl_amount for t in trades if 11 <= t.hold_days <= 15),
                   sum(1 for t in trades if 11 <= t.hold_days <= 15)],
        '16-25d': [sum(t.pnl_amount for t in trades if 16 <= t.hold_days <= 25),
                   sum(1 for t in trades if 16 <= t.hold_days <= 25)],
        '26d+': [sum(t.pnl_amount for t in trades if t.hold_days > 25),
                 sum(1 for t in trades if t.hold_days > 25)],
    }

    tp1_trades = [t for t in trades if t.exit_reason == 'TAKE_PROFIT_1']
    tp1_pnl = sum(t.pnl_amount for t in tp1_trades)
    other_pnl = sum(t.pnl_amount for t in trades) - tp1_pnl

    return {
        'label': label,
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
        'tp1_count': len(tp1_trades),
        'tp1_pnl': tp1_pnl,
        'other_pnl': other_pnl,
    }


def fmt_pf(x, w=5):
    if x == float('inf'):
        return f"{'inf':>{w}}"
    return f"{x:>{w}.2f}"


def main():
    logger.info("=" * 60)
    logger.info("실험 3: TP1 구조 재설계 (5개 변형)")
    logger.info("=" * 60)

    # 1회 로딩 + 사전계산
    base_params = StrategyParams(
        stop_loss_atr=BASE_SL, trailing_atr=BASE_TRAIL, max_hold_days=BASE_HOLD,
    )

    t0 = time.time()
    cache = load_backtest_data(base_params)
    t_load = time.time() - t0
    logger.info(f"Data loaded in {t_load:.1f}s")

    t0 = time.time()
    pre = precompute_daily_signals(
        cache['trading_dates'], cache['ticker_data'], cache['ticker_date_idx'],
        cache['initial_universe'], base_params,
    )
    t_pre = time.time() - t0
    logger.info(f"Signals precomputed in {t_pre:.1f}s")

    results = []
    t_sims = []
    for label, tp1_atr, ratio in VARIANTS:
        params = StrategyParams(
            stop_loss_atr=BASE_SL,
            trailing_atr=BASE_TRAIL,
            max_hold_days=BASE_HOLD,
            take_profit_atr=tp1_atr,
            tp1_sell_ratio=ratio,
        )
        t0 = time.time()
        result = run_portfolio_backtest(
            initial_capital=5_000_000,
            max_positions=4,
            params=params,
            preloaded_data=cache,
            precomputed=pre,
        )
        elapsed = time.time() - t0
        t_sims.append(elapsed)
        m = extract(result, label)
        results.append(m)
        logger.info(
            f"[{label}] trades={m['trades']} WR={m['wr']:.1%} "
            f"PF={m['pf']:.2f} CAGR={m['cagr']:+.1%} MDD={m['mdd']:.1%} "
            f"| {elapsed:.1f}s"
        )

    # ─── 리포트 ───
    total_sim = sum(t_sims)
    print("\n" + "=" * 110)
    print("  실험 3: TP1 구조 재설계 (SL 2.0 / Trail 4.0 / Hold 20 고정)")
    print("=" * 110)

    print("\n■ 성능")
    print(f"  데이터+신호 로딩:  {t_load + t_pre:.1f}초 (Load {t_load:.1f} + Precompute {t_pre:.1f})")
    print(f"  시뮬레이션:        {len(t_sims)}회 × 평균 {total_sim/len(t_sims):.1f}초 = {total_sim:.1f}초")
    print(f"  총 소요:           {t_load + t_pre + total_sim:.1f}초")

    print("\n■ 핵심 지표 비교")
    print(f"  {'변형':<22} {'건수':>5} {'승률':>6} {'PF':>5} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'Payoff':>7}")
    print("  " + "-" * 95)
    for m in results:
        print(f"  {m['label']:<22} {m['trades']:>5} {m['wr']:>5.1%} "
              f"{fmt_pf(m['pf'])} {fmt_pf(m['pf_bc'], 7)} "
              f"{m['cagr']:>+6.1%} {m['mdd']:>6.1%} "
              f"{m['net']:>+14,.0f} {m['payoff']:>7.2f}")

    print("\n■ 보유 기간별 순손익 (건수)")
    print(f"  {'변형':<22} {'1-5d':>18} {'6-10d':>18} {'11-15d':>18} {'16-25d':>18} {'26d+':>18}")
    print("  " + "-" * 118)
    for m in results:
        h = m['hold_net']
        print(f"  {m['label']:<22} "
              f"{h['1-5d'][0]:>+12,.0f}({h['1-5d'][1]:>3}) "
              f"{h['6-10d'][0]:>+12,.0f}({h['6-10d'][1]:>3}) "
              f"{h['11-15d'][0]:>+12,.0f}({h['11-15d'][1]:>3}) "
              f"{h['16-25d'][0]:>+12,.0f}({h['16-25d'][1]:>3}) "
              f"{h['26d+'][0]:>+12,.0f}({h['26d+'][1]:>3})")

    print("\n■ Exit Reason 분포")
    print(f"  {'변형':<22} {'SL':>5} {'TP1':>5} {'TRAIL':>6} {'TREND':>6} {'TIME':>5} {'FINAL':>6}")
    print("  " + "-" * 65)
    for m in results:
        r = m['reasons']
        print(f"  {m['label']:<22} "
              f"{r.get('STOP_LOSS', 0):>5} {r.get('TAKE_PROFIT_1', 0):>5} "
              f"{r.get('TRAILING', 0):>6} {r.get('TREND_EXIT', 0):>6} "
              f"{r.get('TIME_EXIT', 0):>5} {r.get('FINAL_CLOSE', 0):>6}")

    print("\n■ TP1 기여도")
    print(f"  {'변형':<22} {'TP1 터치':>10} {'TP1 손익':>14} {'나머지 손익':>14}")
    print("  " + "-" * 65)
    for m in results:
        print(f"  {m['label']:<22} {m['tp1_count']:>10} "
              f"{m['tp1_pnl']:>+14,.0f} {m['other_pnl']:>+14,.0f}")

    # Best 선택 (실험3 내에서 PF 최대)
    best = max(results, key=lambda m: m['pf'])
    exp2_best = results[0]  # TP1 2.0 / 50% (현행) = 실험2 Best

    print("\n■ Best vs 실험2 Best 비교")
    print(f"  {'항목':<15} {'실험2 Best':>18} {'실험3 Best':>18} {'변화':>12}")
    print("  " + "-" * 70)
    for label, key, fmt_s in [
        ('Trades', 'trades', '{:>18}'),
        ('WR', 'wr', '{:>17.1%}'),
        ('PF', 'pf', '{:>18.2f}'),
        ('PF(비용전)', 'pf_bc', '{:>18.2f}'),
        ('CAGR', 'cagr', '{:>+17.1%}'),
        ('MDD', 'mdd', '{:>17.1%}'),
        ('순손익', 'net', '{:>+18,.0f}'),
        ('Payoff', 'payoff', '{:>18.2f}'),
    ]:
        a = fmt_s.format(exp2_best[key])
        b = fmt_s.format(best[key])
        diff = best[key] - exp2_best[key]
        if key in ('wr', 'cagr', 'mdd'):
            d_s = f"{diff:>+11.1%}"
        elif key == 'net':
            d_s = f"{diff:>+12,.0f}"
        elif key == 'trades':
            d_s = f"{diff:>+12}"
        else:
            d_s = f"{diff:>+12.2f}"
        print(f"  {label:<15} {a} {b} {d_s}")

    print("\n" + "=" * 110)
    print(f"  Best: {best['label']}")
    print(f"  PF: {exp2_best['pf']:.2f} (실험2) → {best['pf']:.2f} (실험3)  [{best['pf']-exp2_best['pf']:+.2f}]")
    if best['pf'] >= 1.2:
        v = "✅ PF≥1.2 달성"
    elif best['pf'] > exp2_best['pf']:
        v = "⚠ 개선되었으나 PF<1.2"
    else:
        v = "❌ 개선 실패 (현행 유지)"
    print(f"  판정: {v}")
    print("=" * 110)


if __name__ == '__main__':
    main()

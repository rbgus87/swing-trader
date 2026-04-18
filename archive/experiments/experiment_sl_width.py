"""실험 1: SL ATR 배수 비교 — 조기 손절 문제 검증.

Loss Decomposition 진단:
- 1-2일 보유 PF 0.30, 3-5일 PF 0.44 (조기 손절 재앙)
- STOP_LOSS 458건 WR 0% (−13.6M)
- 11-15일 보유 PF 2.47 (여기서만 수익)

가설: SL 폭 확대 → 조기 손절 감소 → WR/PF 개선.
단, 개별 손실 폭 증가로 역전되는 최적점 존재.

4가지 변형: ATR×1.5 / 2.0 / 2.5 / 3.0
"""
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, Exception):
    pass

import numpy as np
from collections import defaultdict
from loguru import logger

from src.backtest.portfolio_backtester import run_portfolio_backtest
from src.backtest.swing_backtester import CostModel
from src.strategy.trend_following_v0 import StrategyParams


SL_VARIANTS = [1.5, 2.0, 2.5, 3.0]


def extract_metrics(result, sl_mult):
    """PortfolioResult에서 지표 추출."""
    trades = result.trades
    cost = CostModel()
    cost_pct = cost.total_cost_pct()

    winners = [t for t in trades if t.pnl_amount > 0]
    losers = [t for t in trades if t.pnl_amount <= 0]

    avg_win_pct = np.mean([t.pnl_pct for t in winners]) if winners else 0
    avg_loss_pct = np.mean([t.pnl_pct for t in losers]) if losers else 0
    payoff = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct != 0 else 0

    # 비용 전 PF
    pnl_bc = [(t.pnl_pct + cost_pct) for t in trades]
    gp_bc = sum(p for p in pnl_bc if p > 0)
    gl_bc = abs(sum(p for p in pnl_bc if p <= 0))
    pf_bc = gp_bc / gl_bc if gl_bc > 0 else float('inf')

    # Exit Reason 분포
    reason_groups = defaultdict(list)
    for t in trades:
        reason_groups[t.exit_reason].append(t)

    sl_trades = reason_groups.get('STOP_LOSS', [])
    sl_avg_pct = np.mean([t.pnl_pct for t in sl_trades]) if sl_trades else 0

    tp1_trades = reason_groups.get('TAKE_PROFIT_1', [])
    total = result.total_trades

    # 보유 기간별
    hold_buckets = {
        '1-2d': [t for t in trades if t.hold_days <= 2],
        '3-5d': [t for t in trades if 3 <= t.hold_days <= 5],
        '6-10d': [t for t in trades if 6 <= t.hold_days <= 10],
        '11-15d': [t for t in trades if 11 <= t.hold_days <= 15],
    }
    hold_net = {k: (sum(t.pnl_amount for t in v), len(v)) for k, v in hold_buckets.items()}

    return {
        'sl_mult': sl_mult,
        'trades': total,
        'wr': result.win_rate,
        'pf': result.profit_factor,
        'pf_before_cost': pf_bc,
        'cagr': result.cagr_pct,
        'mdd': result.max_drawdown_pct,
        'net_pnl': sum(t.pnl_amount for t in trades),
        'final_capital': result.final_capital,
        'payoff': payoff,
        'sl_count': len(sl_trades),
        'sl_avg_pct': sl_avg_pct,
        'tp1_count': len(tp1_trades),
        'tp1_ratio': len(tp1_trades) / total if total else 0,
        'hold_net': hold_net,
        'reasons': {r: len(v) for r, v in reason_groups.items()},
    }


def fmt_pf(x):
    if x == float('inf'):
        return '  inf'
    return f"{x:>5.2f}"


def print_report(results):
    print("\n" + "=" * 100)
    print("  실험 1: SL ATR 배수 비교 (trailing_atr=3.0, take_profit_atr=2.0 고정)")
    print("=" * 100)

    print("\n■ 핵심 지표 비교")
    print(f"{'변형':<10} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(비용전)':>10} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'Payoff':>7}")
    print("-" * 85)
    for m in results:
        print(f"ATR×{m['sl_mult']:<6} {m['trades']:>5} "
              f"{m['wr']:>5.1%} {fmt_pf(m['pf'])} {fmt_pf(m['pf_before_cost']):>10} "
              f"{m['cagr']:>+6.1%} {m['mdd']:>6.1%} "
              f"{m['net_pnl']:>+14,.0f} {m['payoff']:>7.2f}")

    print("\n■ 손절 상세")
    print(f"{'변형':<10} {'SL건수':>7} {'SL평균%':>8} {'TP1건수':>8} {'TP1비율':>8}")
    print("-" * 50)
    for m in results:
        print(f"ATR×{m['sl_mult']:<6} {m['sl_count']:>7} "
              f"{m['sl_avg_pct']:>+7.2%} {m['tp1_count']:>8} {m['tp1_ratio']:>7.1%}")

    print("\n■ 보유 기간별 순손익 (건수)")
    print(f"{'변형':<10} {'1-2d':>18} {'3-5d':>18} {'6-10d':>18} {'11-15d':>18}")
    print("-" * 90)
    for m in results:
        h = m['hold_net']
        print(f"ATR×{m['sl_mult']:<6} "
              f"{h['1-2d'][0]:>+12,.0f}({h['1-2d'][1]:>3}) "
              f"{h['3-5d'][0]:>+12,.0f}({h['3-5d'][1]:>3}) "
              f"{h['6-10d'][0]:>+12,.0f}({h['6-10d'][1]:>3}) "
              f"{h['11-15d'][0]:>+12,.0f}({h['11-15d'][1]:>3})")

    print("\n■ Exit Reason 분포")
    print(f"{'변형':<10} {'STOP_LOSS':>10} {'TAKE_PROFIT_1':>14} {'TRAILING':>10} {'TREND_EXIT':>11} {'TIME_EXIT':>10} {'FINAL':>7}")
    print("-" * 85)
    for m in results:
        r = m['reasons']
        print(f"ATR×{m['sl_mult']:<6} "
              f"{r.get('STOP_LOSS', 0):>10} {r.get('TAKE_PROFIT_1', 0):>14} "
              f"{r.get('TRAILING', 0):>10} {r.get('TREND_EXIT', 0):>11} "
              f"{r.get('TIME_EXIT', 0):>10} {r.get('FINAL_CLOSE', 0):>7}")

    # Best 판정
    best = max(results, key=lambda m: m['pf'])
    baseline = next(m for m in results if m['sl_mult'] == 1.5)

    print("\n" + "=" * 100)
    print(f"  Best: ATR×{best['sl_mult']}  (PF {best['pf']:.2f}, CAGR {best['cagr']:+.1%}, MDD {best['mdd']:.1%})")
    print(f"  개선폭: PF {baseline['pf']:.2f} (baseline ATR×1.5) → {best['pf']:.2f}  [{best['pf']-baseline['pf']:+.2f}]")

    if best['pf'] >= 1.2:
        verdict = "✅ STRONG 개선 (PF 1.2+ 달성)"
    elif best['pf'] >= 1.0:
        verdict = "⚠ MARGINAL 개선 (엣지 있지만 약함)"
    elif best['pf'] > baseline['pf']:
        verdict = "⚠ 부분 개선 (baseline 대비 상승)"
    else:
        verdict = "❌ 개선 실패"
    print(f"  판정: {verdict}")
    print("=" * 100)


def main():
    results = []
    for sl_mult in SL_VARIANTS:
        logger.info("=" * 60)
        logger.info(f"Running SL=ATR×{sl_mult}")
        logger.info("=" * 60)

        params = StrategyParams(stop_loss_atr=sl_mult)
        result = run_portfolio_backtest(
            initial_capital=5_000_000,
            max_positions=4,
            params=params,
        )
        m = extract_metrics(result, sl_mult)
        results.append(m)
        logger.info(
            f"SL=ATR×{sl_mult} | trades={m['trades']} WR={m['wr']:.1%} "
            f"PF={m['pf']:.2f} CAGR={m['cagr']:+.1%} MDD={m['mdd']:.1%}"
        )

    print_report(results)


if __name__ == '__main__':
    main()

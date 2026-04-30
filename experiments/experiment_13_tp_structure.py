"""실험 13: equity 균등 + TP 구조 변경 (MDD 통제).

equity 균등은 CAGR 18.9% / MDD 36.5% (CAGR/MDD 0.52, baseline 동률).
복리 효과를 살리되 MDD를 30% 이하로 낮추기 위해 TP1 비율 상향 또는
TP2(2단계 익절) 추가로 되돌림 노출 감소를 시도.

모든 변형(2~6): equity 균등 사이징 (alloc = equity / 4).

| # | 변형 | TP1 | TP2 | 잔여 (러닝) |
| 1 | baseline (참고) | 2.0/30% | -        | 70% (cash×25%) |
| 2 | equity+현행TP   | 2.0/30% | -        | 70% |
| 3 | equity+TP1 40%  | 2.0/40% | -        | 60% |
| 4 | equity+TP1 50%  | 2.0/50% | -        | 50% |
| 5 | equity+TP2(30+30)| 2.0/30%| 4.0/30%  | 40% |
| 6 | equity+TP2 변형 | 2.0/20% | 3.0/30%  | 50% |
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


def tp_stats(trades):
    """TP1/TP2 발동 수와 확보 금액(매도 대금)."""
    tp1_n = sum(1 for t in trades if t.exit_reason == 'TAKE_PROFIT_1')
    tp2_n = sum(1 for t in trades if t.exit_reason == 'TAKE_PROFIT_2')
    tp1_pnl = sum(t.pnl_amount for t in trades if t.exit_reason == 'TAKE_PROFIT_1')
    tp2_pnl = sum(t.pnl_amount for t in trades if t.exit_reason == 'TAKE_PROFIT_2')
    return tp1_n, tp1_pnl, tp2_n, tp2_pnl


def run():
    t0 = time.time()
    base_params = StrategyParams()  # v2.4 기본

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base_params)

    # ── 변형별 params 정의 ──
    variants = [
        # (이름, sizing_mode, params_override)
        ('baseline (cash×25%)',     'cash_pct',     {}),
        ('equity+현행TP (30%)',      'equity_equal', {}),
        ('equity+TP1 40%',           'equity_equal', {'tp1_sell_ratio': 0.40}),
        ('equity+TP1 50%',           'equity_equal', {'tp1_sell_ratio': 0.50}),
        ('equity+TP2 (30+30)',       'equity_equal', {'tp2_atr': 4.0, 'tp2_sell_ratio': 0.30}),
        ('equity+TP2 변형 (20+30)',   'equity_equal', {'tp1_sell_ratio': 0.20,
                                                      'tp2_atr': 3.0,
                                                      'tp2_sell_ratio': 0.30}),
    ]

    # precompute는 진입 조건만 사용 (청산 파라미터 무관) → 1회만
    logger.info("precompute (v2.4)")
    t1 = time.time()
    precomp = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        base_params,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )
    logger.info(f"  precompute {time.time()-t1:.1f}s")

    def go(name, sizing_mode, override):
        logger.info(f"--- 백테스트: {name} (override={override}) ---")
        params = replace(base_params, **override)
        t = time.time()
        r = run_portfolio_backtest(
            initial_capital=INITIAL_CAPITAL, max_positions=MAX_POSITIONS,
            params=params, preloaded_data=preloaded, precomputed=precomp,
            risk=None, sizing_mode=sizing_mode,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t:.1f}s)"
        )
        return r, net, pfg

    results = []
    for name, mode, override in variants:
        r, net, pfg = go(name, mode, override)
        results.append((name, r, net, pfg))

    total_time = time.time() - t0

    # ── 보고 ──
    print("\n" + "=" * 110)
    print("📋 실험 13: equity + TP 구조 변경 완료 보고")
    print("=" * 110)
    print(f"Period: {results[0][1].period}")
    print(f"총 소요: {total_time:.1f}s")

    # 핵심 지표
    print("\n■ 핵심 지표")
    print(f"{'변형':<32} {'건수':>5} {'WR':>5} {'PF':>5} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9} {'Payoff':>7}")
    print("-" * 110)
    for name, r, net, pfg in results:
        cm = r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else float('inf')
        p = payoff(r.trades)
        ps = f"{p:.2f}" if p == p else 'n/a'
        print(
            f"{name:<32} {r.total_trades:>5} {r.win_rate:>4.1%} "
            f"{fmt_pf(r.profit_factor):>5} {fmt_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm:>8.2f} {ps:>7}"
        )

    # TP 발동 통계
    print("\n■ TP 발동 통계")
    print(f"{'변형':<32} {'TP1 건':>7} {'TP1 손익':>14} {'TP2 건':>7} {'TP2 손익':>14}")
    print("-" * 85)
    for name, r, _, _ in results:
        n1, p1, n2, p2 = tp_stats(r.trades)
        print(f"{name:<32} {n1:>7} {p1:>+14,.0f} {n2:>7} {p2:>+14,.0f}")

    # 러너 보존
    print("\n■ 러너 보존 (16-25d / 26d+)")
    print(f"{'변형':<32} {'16-25d 건':>10} {'16-25d 손익':>15} {'26d+ 건':>9} {'26d+ 손익':>14}")
    print("-" * 90)
    for name, r, _, _ in results:
        b = hold_buckets(r.trades)
        p16, n16 = b['16-25d']
        p26, n26 = b['26d+']
        print(f"{name:<32} {n16:>10} {p16:>+15,.0f} {n26:>9} {p26:>+14,.0f}")

    # 판정
    print("\n■ 판정")
    # CAGR/MDD 최고
    def cm(r):
        return r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0
    best_idx = max(range(len(results)), key=lambda i: cm(results[i][1]))
    bn, br, bnet, _ = results[best_idx]
    print(f"  CAGR/MDD 최고: {bn} ({cm(br):.2f})")
    print(f"    CAGR={br.cagr_pct:.1%}, MDD={br.max_drawdown_pct:.1%}, 순손익={bnet:+,.0f}")

    # MDD ≤ 30% 변형들
    under30 = [(name, r, net) for name, r, net, _ in results if r.max_drawdown_pct <= 0.30]
    if under30:
        print(f"\n  MDD ≤ 30% 달성:")
        for n, r, net in under30:
            print(f"    {n}: MDD={r.max_drawdown_pct:.1%}, CAGR={r.cagr_pct:.1%}, 순손익={net:+,.0f}")
    else:
        print(f"\n  MDD ≤ 30% 달성: 없음")

    # 순손익 최고
    best_pnl_idx = max(range(len(results)), key=lambda i: results[i][2])
    print(f"\n  순손익 최고: {results[best_pnl_idx][0]} ({results[best_pnl_idx][2]:+,.0f})")


if __name__ == "__main__":
    run()

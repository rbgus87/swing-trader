"""실험 16: 10M 최종 — TP 비율 그리드 + 6종목 분산.

전부 10M, equity 균등, RS 5%p (현행), min 600K.

A그룹: 5종목 / TP 비율 5변형
B그룹: 6종목 / 현행 TP + A 그룹 Best TP 적용

precompute는 진입 조건(MA/MACD/RS/ADX) 무관 — 1회 재사용.
"""
import sys
import time
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
INITIAL_CAPITAL = 10_000_000
MIN_POS_AMT = 600_000


def fmt_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def pf_gross(trades):
    gp = gl = 0.0
    for t in trades:
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if g > 0: gp += g
        else: gl += abs(g)
    return gp / gl if gl > 0 else float('inf')


def cm_score(r):
    return r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0


def go(name, max_pos, params, preloaded, precomp):
    t = time.time()
    r = run_portfolio_backtest(
        initial_capital=INITIAL_CAPITAL, max_positions=max_pos, params=params,
        preloaded_data=preloaded, precomputed=precomp,
        risk=None, sizing_mode='equity', min_position_amount=MIN_POS_AMT,
    )
    net = r.final_capital - r.initial_capital
    pfg = pf_gross(r.trades)
    logger.info(
        f"[{name}] mp={max_pos} TP1={params.tp1_sell_ratio:.0%} TP2={params.tp2_sell_ratio:.0%} → "
        f"trades={r.total_trades}, WR={r.win_rate:.1%}, PF={fmt_pf(r.profit_factor)}, "
        f"CAGR={r.cagr_pct:.1%}, MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t:.1f}s)"
    )
    return r, net, pfg


def print_table(title, rows):
    print(f"\n■ {title}")
    print(f"{'변형':<24} {'건수':>5} {'WR':>6} {'PF':>5} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9}")
    print("-" * 100)
    for name, r, net, pfg in rows:
        print(
            f"{name:<24} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{fmt_pf(r.profit_factor):>5} {fmt_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm_score(r):>8.2f}"
        )


def run():
    t0 = time.time()
    base = StrategyParams()
    v25 = replace(base, tp2_atr=4.0, tp2_sell_ratio=0.30)

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base)

    logger.info("precompute (default — 진입 조건 동일)")
    pc = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        v25,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )

    # ── 5M 참조 ──
    logger.info("=== 5M v2.5 참조 ===")
    t = time.time()
    r5 = run_portfolio_backtest(
        initial_capital=5_000_000, max_positions=4, params=v25,
        preloaded_data=preloaded, precomputed=pc,
        risk=None, sizing_mode='equity',
    )
    n5 = r5.final_capital - r5.initial_capital
    logger.info(
        f"[5M v2.5] trades={r5.total_trades}, WR={r5.win_rate:.1%}, "
        f"PF={fmt_pf(r5.profit_factor)}, CAGR={r5.cagr_pct:.1%}, "
        f"MDD={r5.max_drawdown_pct:.1%} ({time.time()-t:.1f}s)"
    )

    # ── A그룹: 5종목, TP 비율 5변형 ──
    logger.info("=== A그룹: 5종목, TP 비율 ===")
    A_variants = [
        ('현행 30/30/40', 0.30, 0.30),
        ('40/30/30',      0.40, 0.30),
        ('30/40/30',      0.30, 0.40),
        ('40/40/20',      0.40, 0.40),
        ('20/30/50',      0.20, 0.30),
    ]
    A_rows = []
    A_meta = []  # (name, params, r, net, pfg)
    for name, t1, t2 in A_variants:
        params = replace(v25, tp1_sell_ratio=t1, tp2_sell_ratio=t2)
        r, net, pfg = go(name, 5, params, preloaded, pc)
        A_rows.append((name, r, net, pfg))
        A_meta.append((name, params, r, net, pfg))

    # A best (CAGR/MDD)
    A_best = max(A_meta, key=lambda x: cm_score(x[2]))
    A_best_name, A_best_params, A_best_r, _, _ = A_best
    logger.info(
        f"=== A best: {A_best_name} (CAGR/MDD={cm_score(A_best_r):.2f}, "
        f"PF={fmt_pf(A_best_r.profit_factor)}) ==="
    )

    # ── B그룹: 6종목 ──
    logger.info("=== B그룹: 6종목 ===")
    B_rows = []

    # B1: 6종목 + 현행 TP (30/30/40)
    params_default = replace(v25)
    r, net, pfg = go('6종목 현행TP', 6, params_default, preloaded, pc)
    B_rows.append(('6종목 현행TP (30/30/40)', r, net, pfg))

    # B2: 6종목 + Best TP
    if A_best_name != '현행 30/30/40':
        r, net, pfg = go(f'6종목 Best TP ({A_best_name})', 6, A_best_params, preloaded, pc)
        B_rows.append((f'6종목 Best TP ({A_best_name})', r, net, pfg))
    else:
        logger.info("A best가 현행과 동일 → B2 스킵 (B1과 동치)")

    # ── 보고 ──
    total_time = time.time() - t0
    print("\n" + "=" * 110)
    print("📋 실험 16: 10M TP + 종목수 완료 보고")
    print("=" * 110)
    print(f"Period: {r5.period}")
    print(f"공통: 10M / equity 균등 / TP1+TP2 / RS 5%p / min 600K")
    print(f"5M 참조: PF {fmt_pf(r5.profit_factor)}, CAGR {r5.cagr_pct:.1%}, MDD {r5.max_drawdown_pct:.1%}, CAGR/MDD {cm_score(r5):.2f}")
    print(f"총 소요: {total_time:.1f}s")

    print_table("A그룹: 5종목, TP 비율", A_rows)
    print_table("B그룹: 6종목", B_rows)

    # ── 전체 Best ──
    all_rows = list(A_rows) + list(B_rows)
    all_best_name, all_best_r, all_best_net, _ = max(all_rows, key=lambda x: cm_score(x[1]))

    print(f"\n■ 5M v2.5 대비 전체 Best: {all_best_name}")
    print(f"{'항목':<15} {'5M v2.5':>15} {'10M Best':>15} {'차이':>15}")
    print("-" * 65)
    items = [
        ('PF', f"{r5.profit_factor:.2f}", f"{all_best_r.profit_factor:.2f}",
         f"{all_best_r.profit_factor - r5.profit_factor:+.2f}"),
        ('CAGR', f"{r5.cagr_pct:.1%}", f"{all_best_r.cagr_pct:.1%}",
         f"{(all_best_r.cagr_pct - r5.cagr_pct)*100:+.1f}%p"),
        ('MDD', f"{r5.max_drawdown_pct:.1%}", f"{all_best_r.max_drawdown_pct:.1%}",
         f"{(all_best_r.max_drawdown_pct - r5.max_drawdown_pct)*100:+.1f}%p"),
        ('WR', f"{r5.win_rate:.1%}", f"{all_best_r.win_rate:.1%}",
         f"{(all_best_r.win_rate - r5.win_rate)*100:+.1f}%p"),
        ('순손익', f"{n5:+,.0f}", f"{all_best_net:+,.0f}",
         f"{all_best_net - n5:+,.0f}"),
        ('CAGR/MDD', f"{cm_score(r5):.2f}", f"{cm_score(all_best_r):.2f}",
         f"{cm_score(all_best_r) - cm_score(r5):+.2f}"),
    ]
    for k, v1, v2, vd in items:
        print(f"{k:<15} {v1:>15} {v2:>15} {vd:>15}")

    print("\n■ 판정")
    print(f"  10M Best 사양: {all_best_name}")
    # max_pos·TP 추출
    if all_best_name.startswith('6종목'):
        mp = 6
        if 'Best TP' in all_best_name:
            tp1 = A_best_params.tp1_sell_ratio
            tp2 = A_best_params.tp2_sell_ratio
        else:
            tp1, tp2 = 0.30, 0.30
    else:
        mp = 5
        # A_meta에서 매칭
        for n, p, r, net, pfg in A_meta:
            if n == all_best_name:
                tp1, tp2 = p.tp1_sell_ratio, p.tp2_sell_ratio
                break
        else:
            tp1, tp2 = 0.30, 0.30
    print(f"    종목수: {mp}")
    print(f"    TP1 비율: {tp1:.0%}")
    print(f"    TP2 비율: {tp2:.0%}")
    print(f"    잔여 (트레일링): {1-tp1-tp2:.0%}")
    print(f"    PF {all_best_r.profit_factor:.2f} / CAGR {all_best_r.cagr_pct:.1%} / "
          f"MDD {all_best_r.max_drawdown_pct:.1%} / 순손익 {all_best_net:+,.0f}")


if __name__ == "__main__":
    run()

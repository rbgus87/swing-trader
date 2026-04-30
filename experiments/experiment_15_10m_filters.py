"""실험 15: 10M 자본 5종목 환경에서 진입 필터 강화로 PF 회복.

전부 10M, 5종목, equity 균등, TP1+TP2.

A그룹: breadth gate (0.40 / 0.50 / 0.55) — precompute 동일, runtime gate만 변경
B그룹: ADX threshold (25 / 30) — precompute 재실행 필요 (진입 조건 변경)
C그룹: RS threshold (7%p / 10%p) — precompute 재실행 필요
D그룹: A+B / A+C / A+B+C 자동 조합

목표: PF ≥ 1.80, MDD ≤ 35%, CAGR ≥ 14%
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
MAX_POSITIONS = 5  # 10M 5종목 baseline
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


def precompute_with(preloaded, params):
    """precompute_daily_signals 래퍼 — 시장별 분기 기본."""
    return precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        params,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )


def run_variant(name, preloaded, precomp, params, gate=None):
    t = time.time()
    r = run_portfolio_backtest(
        initial_capital=INITIAL_CAPITAL, max_positions=MAX_POSITIONS,
        params=params, preloaded_data=preloaded, precomputed=precomp,
        risk=None, sizing_mode='equity', min_position_amount=MIN_POS_AMT,
        breadth_gate_threshold=gate,
    )
    net = r.final_capital - r.initial_capital
    pfg = pf_gross(r.trades)
    logger.info(
        f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
        f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
        f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t:.1f}s)"
    )
    return r, net, pfg


def print_table(title, rows):
    """rows: list of (name, r, net, pfg)"""
    print(f"\n■ {title}")
    print(f"{'변형':<28} {'건수':>5} {'WR':>6} {'PF':>5} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'CAGR/MDD':>9}")
    print("-" * 105)
    for name, r, net, pfg in rows:
        print(
            f"{name:<28} {r.total_trades:>5} {r.win_rate:>5.1%} "
            f"{fmt_pf(r.profit_factor):>5} {fmt_pf(pfg):>7} "
            f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
            f"{net:>+13,.0f} {cm_score(r):>8.2f}"
        )


def best_of(rows, key=cm_score):
    """best by CAGR/MDD ratio."""
    return max(rows, key=lambda x: key(x[1]))


def run():
    t0 = time.time()
    base = StrategyParams()
    v25 = replace(base, tp2_atr=4.0, tp2_sell_ratio=0.30)

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base)

    # ── 5M 참조 ──
    logger.info("=== 5M v2.5 참조 (4종목 baseline) ===")
    pc_default = precompute_with(preloaded, v25)
    t = time.time()
    r5 = run_portfolio_backtest(
        initial_capital=5_000_000, max_positions=4, params=v25,
        preloaded_data=preloaded, precomputed=pc_default,
        risk=None, sizing_mode='equity',
    )
    n5 = r5.final_capital - r5.initial_capital
    logger.info(
        f"[5M v2.5] trades={r5.total_trades}, WR={r5.win_rate:.1%}, "
        f"PF={fmt_pf(r5.profit_factor)}, CAGR={r5.cagr_pct:.1%}, "
        f"MDD={r5.max_drawdown_pct:.1%} ({time.time()-t:.1f}s)"
    )

    # ── A그룹: breadth gate ──
    logger.info("=== A그룹: breadth gate ===")
    A_rows = []
    for gate in (0.40, 0.50, 0.55):
        r, net, pfg = run_variant(
            f'10M 5종목 gate {gate:.2f}', preloaded, pc_default, v25, gate=gate
        )
        A_rows.append((f'gate {gate:.2f}', r, net, pfg))

    # ── B그룹: ADX ──
    logger.info("=== B그룹: ADX threshold ===")
    B_rows = []
    for adx in (25, 30):
        params_adx = replace(v25, adx_threshold=float(adx))
        logger.info(f"  precompute (ADX={adx})")
        pc = precompute_with(preloaded, params_adx)
        r, net, pfg = run_variant(
            f'10M 5종목 ADX {adx}', preloaded, pc, params_adx, gate=None
        )
        B_rows.append((f'ADX {adx}', r, net, pfg, pc, params_adx))

    # ── C그룹: RS threshold ──
    logger.info("=== C그룹: RS threshold ===")
    C_rows = []
    for rs in (0.07, 0.10):
        params_rs = replace(v25, relative_strength_threshold=rs)
        logger.info(f"  precompute (RS={rs:.2f})")
        pc = precompute_with(preloaded, params_rs)
        r, net, pfg = run_variant(
            f'10M 5종목 RS {int(rs*100)}%p', preloaded, pc, params_rs, gate=None
        )
        C_rows.append((f'RS {int(rs*100)}%p', r, net, pfg, pc, params_rs))

    # ── 그룹 best 식별 ──
    A_best_name, A_best_r, _, _ = best_of(A_rows)
    A_best_gate = float(A_best_name.split()[1])

    B_best_name, B_best_r, _, _, B_best_pc, B_best_params = best_of(
        [(n, r, net, pfg) for (n, r, net, pfg, _, _) in B_rows]
    )
    # 다시 매핑 (pc, params 회수)
    B_best_meta = next(x for x in B_rows if x[0] == B_best_name)
    B_best_pc, B_best_params = B_best_meta[4], B_best_meta[5]

    C_best_name, C_best_r, _, _ = best_of(
        [(n, r, net, pfg) for (n, r, net, pfg, _, _) in C_rows]
    )
    C_best_meta = next(x for x in C_rows if x[0] == C_best_name)
    C_best_pc, C_best_params = C_best_meta[4], C_best_meta[5]

    logger.info(
        f"=== 그룹 best: A={A_best_name} (cm={cm_score(A_best_r):.2f}), "
        f"B={B_best_name} (cm={cm_score(B_best_r):.2f}), "
        f"C={C_best_name} (cm={cm_score(C_best_r):.2f}) ==="
    )

    # ── D그룹: 복합 ──
    # 조건 — A~C 어느 한 그룹이라도 baseline(첫 행)보다 cm_score 높지 않으면 D 스킵
    A_baseline_cm = cm_score(A_rows[0][1])
    A_improved = cm_score(A_best_r) > A_baseline_cm

    D_rows = []
    if A_improved or cm_score(B_best_r) > cm_score(A_rows[0][1]) or cm_score(C_best_r) > cm_score(A_rows[0][1]):
        logger.info("=== D그룹: 복합 (A best + B / C / B+C) ===")

        # D1: A best + B best (B best precomp 재사용 가능, gate만 변경)
        r, net, pfg = run_variant(
            f'D1: gate{A_best_gate:.2f} + ADX{int(B_best_params.adx_threshold)}',
            preloaded, B_best_pc, B_best_params, gate=A_best_gate
        )
        D_rows.append((
            f'A({A_best_gate:.2f})+B(ADX{int(B_best_params.adx_threshold)})',
            r, net, pfg
        ))

        # D2: A best + C best
        r, net, pfg = run_variant(
            f'D2: gate{A_best_gate:.2f} + RS{int(C_best_params.relative_strength_threshold*100)}%p',
            preloaded, C_best_pc, C_best_params, gate=A_best_gate
        )
        D_rows.append((
            f'A({A_best_gate:.2f})+C(RS{int(C_best_params.relative_strength_threshold*100)}%p)',
            r, net, pfg
        ))

        # D3: A + B + C — 새 precomp 필요
        params_abc = replace(
            v25,
            adx_threshold=B_best_params.adx_threshold,
            relative_strength_threshold=C_best_params.relative_strength_threshold,
        )
        logger.info(f"  precompute (ADX={int(params_abc.adx_threshold)}, RS={params_abc.relative_strength_threshold:.2f})")
        pc_abc = precompute_with(preloaded, params_abc)
        r, net, pfg = run_variant(
            f'D3: gate{A_best_gate:.2f} + ADX{int(params_abc.adx_threshold)} + RS{int(params_abc.relative_strength_threshold*100)}%p',
            preloaded, pc_abc, params_abc, gate=A_best_gate
        )
        D_rows.append((
            f'A+B+C(ADX{int(params_abc.adx_threshold)},RS{int(params_abc.relative_strength_threshold*100)}%p)',
            r, net, pfg
        ))
    else:
        logger.warning("개별 그룹에서 baseline 대비 개선 없음 → D그룹 스킵")

    # ── 보고 ──
    total_time = time.time() - t0
    print("\n" + "=" * 110)
    print("📋 실험 15: 10M 5종목 진입 필터 강화 완료 보고")
    print("=" * 110)
    print(f"Period: {r5.period}")
    print(f"기본: 10M / 5종목 / equity 균등 / TP1+TP2 / min 600K")
    print(f"5M 참조: PF {fmt_pf(r5.profit_factor)}, CAGR {r5.cagr_pct:.1%}, MDD {r5.max_drawdown_pct:.1%}, CAGR/MDD {cm_score(r5):.2f}")
    print(f"총 소요: {total_time:.1f}s")

    print_table("A그룹: breadth gate", A_rows)
    print_table("B그룹: ADX threshold", [(n, r, net, pfg) for (n, r, net, pfg, _, _) in B_rows])
    print_table("C그룹: RS threshold", [(n, r, net, pfg) for (n, r, net, pfg, _, _) in C_rows])

    if D_rows:
        print_table("D그룹: 복합", D_rows)

    # ── Best of all ──
    all_rows = list(A_rows) + \
        [(n, r, net, pfg) for (n, r, net, pfg, _, _) in B_rows] + \
        [(n, r, net, pfg) for (n, r, net, pfg, _, _) in C_rows] + \
        list(D_rows)
    all_best_name, all_best_r, all_best_net, _ = best_of(all_rows)

    print(f"\n■ 5M v2.5 대비 Best 변형: {all_best_name}")
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

    # ── 판정 ──
    print("\n■ 판정")
    pf_ok = all_best_r.profit_factor >= 1.80
    mdd_ok = all_best_r.max_drawdown_pct <= 0.35
    cagr_ok = all_best_r.cagr_pct >= 0.14
    target_ok = pf_ok and mdd_ok and cagr_ok

    print(f"  목표 (PF≥1.80, MDD≤35%, CAGR≥14%):")
    print(f"    PF   {all_best_r.profit_factor:.2f} {'✓' if pf_ok else '✗'}")
    print(f"    MDD  {all_best_r.max_drawdown_pct:.1%} {'✓' if mdd_ok else '✗'}")
    print(f"    CAGR {all_best_r.cagr_pct:.1%} {'✓' if cagr_ok else '✗'}")

    if target_ok:
        print(f"\n  ✅ 10M 전용 사양 확정 가능: {all_best_name}")
    else:
        print(f"\n  ❌ 목표 미달 — 필터 강화만으로 PF 회복 불가, 다른 차원 검토 필요")
        print(f"     Best (CAGR/MDD 기준): {all_best_name}")


if __name__ == "__main__":
    run()

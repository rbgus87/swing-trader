"""청산 파라미터 재최적화 — SL/TP1/TP2/Trail 전체 검증.

Phase A~C + ETF + 스코어러 통합 후 청산 파라미터를 재검증한다.
SL=Trail=3.0 문제(trail이 SL 역할 수행) 해결 및 TP1/TP2/Trail 계층 구조 확립.

현행 v2.7 청산 파라미터:
  SL: ATR×3.0 / TP1: ATR×1.5 (10%) / TP2: ATR×4.0 (10%) / Trail: ATR×3.0 / Hold: 20일

설계:
  Round 1: SL × Trail 그리드 (TP1=1.5/TP2=4.0 고정, trail ≥ sl 조건)
  Round 2: TP1 × TP2 그리드 (Round1 최적 SL/Trail 고정, tp1 < tp2 조건)
  Round 3: 보유 기간 (Round1+2 최적 고정)

핵심 검증: trailing_stop 청산의 평균 PnL ≥ 0 (Trail이 이익 보호하는지 확인)

실행:
    python experiments/experiment_exit_reoptimize.py
결과:
    experiments/results_exit_reoptimize.txt
"""
from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.cost_model import CostModel
from src.utils.slippage_model import SlippageParams
from src.strategy.ranking import RankingWeights
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.scaling import ScalingParams


# ─────────────────────────────────────────────────────────────────────────────
# v2.7 설정
# ─────────────────────────────────────────────────────────────────────────────
with open(ROOT / "config.yaml", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)
_tf  = _cfg["trend_following"]
_trd = _cfg["trading"]
_rsk = _cfg["risk"]

CAPITAL  = 10_000_000
MAX_POS  = int(_trd["max_positions"])
MIN_AMT  = int(_rsk["min_position_amount"])
BREADTH  = 0.40

BASE_PARAMS = StrategyParams(
    adx_threshold=float(_tf["adx_threshold"]),
    relative_strength_threshold=float(_tf["relative_strength_threshold"]),
    stop_loss_atr=float(_tf["stop_loss_atr"]),
    take_profit_atr=float(_tf["take_profit_atr"]),
    trailing_atr=float(_tf["trailing_atr"]),
    max_hold_days=int(_tf["max_hold_days"]),
    tp1_sell_ratio=float(_tf["tp1_sell_ratio"]),
    tp2_atr=float(_tf["tp2_atr"]),
    tp2_sell_ratio=float(_tf["tp2_sell_ratio"]),
    ma60_position_min=float(_tf["ma60_position_min"]),
    ma60_position_max=float(_tf["ma60_position_max"]),
    atr_price_min=float(_tf["atr_price_min"]),
    atr_price_max=float(_tf["atr_price_max"]),
    min_trading_value=float(_tf["min_trading_value"]),
)
_rw = _tf.get("ranking_weights", {})
V27_WEIGHTS = RankingWeights(
    rs=float(_rw.get("rs", 0.50)),
    momentum_atr=float(_rw.get("momentum_atr", 0.20)),
    adx=float(_rw.get("adx", 0.15)),
    liquidity=float(_rw.get("liquidity", 0.10)),
    ma_alignment=float(_rw.get("ma_alignment", 0.05)),
)
COST  = CostModel()
_sm   = _tf.get("slippage_model", {})
SLIP  = SlippageParams(
    enabled=bool(_sm.get("enabled", True)),
    base_slippage=float(_sm.get("base_slippage", 0.0003)),
    impact_coefficient=float(_sm.get("impact_coefficient", 0.1)),
    max_slippage=float(_sm.get("max_slippage", 0.02)),
)
SECTOR  = SectorConstraint(enabled=False)
DYNHOLD = DynamicHoldParams(enabled=False)
SCALING = ScalingParams(enabled=False)


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _params_with_exit(
    base: StrategyParams,
    sl: float,
    tp1: float,
    tp2_atr: float,
    trail: float,
    hold: int = 20,
) -> StrategyParams:
    """베이스 파라미터에서 청산 파라미터만 교체. 진입 로직은 불변."""
    return dataclasses.replace(
        base,
        stop_loss_atr=sl,
        take_profit_atr=tp1,
        tp2_atr=tp2_atr,
        trailing_atr=trail,
        max_hold_days=hold,
    )


def _run_bt(preloaded: dict, precomp: dict, params: StrategyParams):
    return run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=params,
        cost=COST,
        min_position_amount=MIN_AMT,
        preloaded_data=preloaded,
        precomputed=precomp,
        sizing_mode="equity",
        breadth_gate_threshold=BREADTH,
        regime_gate_enabled=True,
        sector_constraint=SECTOR,
        dynamic_hold=DYNHOLD,
        scaling=SCALING,
        slippage_params=SLIP,
    )


def _exit_stats(trades: list) -> dict[str, dict]:
    """청산 사유별 건수 + 평균 PnL.

    Returns:
        {reason: {count, avg_pnl, total_pnl}}
    """
    stats: dict[str, dict] = {}
    for t in trades:
        r = t.exit_reason
        if r not in stats:
            stats[r] = {"count": 0, "total_pnl": 0.0}
        stats[r]["count"] += 1
        stats[r]["total_pnl"] += t.pnl_amount
    for s in stats.values():
        s["avg_pnl"] = s["total_pnl"] / s["count"] if s["count"] > 0 else 0.0
    return stats


def evaluate(r) -> float:
    """복합 평가 점수 (높을수록 좋음).

    거래 수 < 500이면 -999 (과소 거래 패널티).
    """
    if r.total_trades < 500:
        return -999.0
    cagr_mdd = r.cagr_pct / abs(r.max_drawdown_pct) if r.max_drawdown_pct != 0 else 0.0
    return r.profit_factor * 0.5 + cagr_mdd * 0.5


def _fmt_result(r) -> str:
    """결과 한 줄 요약."""
    util = r.avg_positions / MAX_POS * 100
    return (
        f"건수 {r.total_trades:4d} | WR {r.win_rate*100:.1f}% | "
        f"PF {r.profit_factor:.2f} | CAGR {r.cagr_pct*100:+.1f}% | "
        f"MDD -{r.max_drawdown_pct*100:.1f}% | 활용 {util:.0f}%"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    out_path = ROOT / "experiments" / "results_exit_reoptimize.txt"
    SEP  = "═" * 70
    SEP2 = "─" * 70

    lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    t_total = time.time()

    # ── 1. 데이터 로드 ─────────────────────────────────────────────────────
    print("[1/3] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    print(f"  완료: {time.time() - t0:.1f}s  ({len(preloaded['ticker_data'])} 종목)")

    # ── 2. 신호 사전 계산 (진입 후보 — 청산 실험 전 1회만) ────────────────
    print("[2/3] v2.7 진입 신호 사전 계산...")
    t0 = time.time()
    precomp_base = precompute_daily_signals(
        preloaded["trading_dates"],
        preloaded["ticker_data"],
        preloaded["ticker_date_idx"],
        set(preloaded["initial_universe"]),
        params=BASE_PARAMS,
        kospi_ret_map=preloaded.get("kospi_ret_map", {}),
        kosdaq_ret_map=preloaded.get("kosdaq_ret_map", {}),
        ticker_market=preloaded.get("ticker_market", {}),
        weights=V27_WEIGHTS,
    )
    total_cands = sum(len(v) for v in precomp_base["candidates"].values())
    print(f"  완료: {time.time() - t0:.1f}s  (총 후보 {total_cands:,}건)")

    # ── 3. 기준선 (현행 v2.7) ─────────────────────────────────────────────
    print("[3/3] 파라미터 그리드 탐색...")
    print("  [기준선] 현행 v2.7 (SL=3.0, TP1=1.5, TP2=4.0, Trail=3.0, Hold=20)...")
    t0 = time.time()
    baseline_r = _run_bt(preloaded, precomp_base, BASE_PARAMS)
    baseline_stats = _exit_stats(baseline_r.trades)
    print(f"    {_fmt_result(baseline_r)}  ({time.time()-t0:.1f}s)")

    # ═══════════════════════════════════════════════════════════════════════
    # Round 1: SL × Trail 그리드 (TP1=1.5, TP2=4.0 고정)
    # ═══════════════════════════════════════════════════════════════════════
    sl_values    = [1.5, 2.0, 2.5, 3.0]
    trail_values = [2.5, 3.0, 3.5, 4.0, 4.5]

    r1: dict[tuple, object] = {}  # (sl, trail) → result

    combos_r1 = [
        (sl, tr) for sl in sl_values for tr in trail_values if tr >= sl
    ]
    print(f"\n  [Round 1] SL×Trail 그리드 ({len(combos_r1)}조합, TP1=1.5/TP2=4.0 고정)...")
    for i, (sl, tr) in enumerate(combos_r1):
        t0 = time.time()
        params = _params_with_exit(BASE_PARAMS, sl=sl, tp1=1.5, tp2_atr=4.0, trail=tr)
        r = _run_bt(preloaded, precomp_base, params)
        r1[(sl, tr)] = r
        score = evaluate(r)
        mark = " ←현행" if (sl == 3.0 and tr == 3.0) else ""
        print(f"    SL{sl:.1f}/Tr{tr:.1f}: PF {r.profit_factor:.2f} | "
              f"CAGR {r.cagr_pct*100:+.1f}% | MDD -{r.max_drawdown_pct*100:.1f}% | "
              f"건수 {r.total_trades} | 점수 {score:.3f}{mark}  ({time.time()-t0:.1f}s)")

    best_r1_key = max(r1.keys(), key=lambda k: evaluate(r1[k]))
    best_sl, best_trail = best_r1_key
    best_r1_r = r1[best_r1_key]
    print(f"\n  → Round 1 최적: SL={best_sl:.1f}, Trail={best_trail:.1f} "
          f"(PF {best_r1_r.profit_factor:.2f})")

    # ═══════════════════════════════════════════════════════════════════════
    # Round 2: TP1 × TP2 그리드 (Round 1 최적 SL/Trail 고정)
    # ═══════════════════════════════════════════════════════════════════════
    tp1_values = [1.0, 1.5, 2.0, 2.5]
    tp2_values = [3.0, 4.0, 5.0, 6.0]

    r2: dict[tuple, object] = {}  # (tp1, tp2) → result

    combos_r2 = [
        (tp1, tp2) for tp1 in tp1_values for tp2 in tp2_values if tp1 < tp2
    ]
    print(f"\n  [Round 2] TP1×TP2 그리드 ({len(combos_r2)}조합, "
          f"SL={best_sl:.1f}/Trail={best_trail:.1f} 고정)...")
    for tp1, tp2 in combos_r2:
        t0 = time.time()
        params = _params_with_exit(BASE_PARAMS, sl=best_sl, tp1=tp1, tp2_atr=tp2, trail=best_trail)
        r = _run_bt(preloaded, precomp_base, params)
        r2[(tp1, tp2)] = r
        score = evaluate(r)
        mark = " ←현행" if (tp1 == 1.5 and tp2 == 4.0) else ""
        print(f"    TP1={tp1:.1f}/TP2={tp2:.1f}: PF {r.profit_factor:.2f} | "
              f"CAGR {r.cagr_pct*100:+.1f}% | MDD -{r.max_drawdown_pct*100:.1f}% | "
              f"건수 {r.total_trades} | 점수 {score:.3f}{mark}  ({time.time()-t0:.1f}s)")

    best_r2_key = max(r2.keys(), key=lambda k: evaluate(r2[k]))
    best_tp1, best_tp2 = best_r2_key
    best_r2_r = r2[best_r2_key]
    print(f"\n  → Round 2 최적: TP1={best_tp1:.1f}, TP2={best_tp2:.1f} "
          f"(PF {best_r2_r.profit_factor:.2f})")

    # ═══════════════════════════════════════════════════════════════════════
    # Round 3: 보유 기간 (Round 1+2 최적 고정)
    # ═══════════════════════════════════════════════════════════════════════
    hold_values = [10, 15, 20, 25]
    r3: dict[int, object] = {}

    print(f"\n  [Round 3] Hold 기간 ({len(hold_values)}조합, "
          f"SL={best_sl:.1f}/TP1={best_tp1:.1f}/TP2={best_tp2:.1f}/Tr={best_trail:.1f} 고정)...")
    for hold in hold_values:
        t0 = time.time()
        params = _params_with_exit(BASE_PARAMS, sl=best_sl, tp1=best_tp1,
                                   tp2_atr=best_tp2, trail=best_trail, hold=hold)
        r = _run_bt(preloaded, precomp_base, params)
        r3[hold] = r
        score = evaluate(r)
        mark = " ←현행" if hold == 20 else ""
        print(f"    Hold={hold:2d}일: PF {r.profit_factor:.2f} | "
              f"CAGR {r.cagr_pct*100:+.1f}% | MDD -{r.max_drawdown_pct*100:.1f}% | "
              f"건수 {r.total_trades} | 점수 {score:.3f}{mark}  ({time.time()-t0:.1f}s)")

    best_hold = max(r3.keys(), key=lambda k: evaluate(r3[k]))
    best_r3_r = r3[best_hold]
    print(f"\n  → Round 3 최적: Hold={best_hold}일 (PF {best_r3_r.profit_factor:.2f})")

    # ═══════════════════════════════════════════════════════════════════════
    # 최종 최적 조합 — 청산 사유별 통계
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n  [최종] 최적 조합 백테스트 재실행 (청산 사유별 통계용)...")
    t0 = time.time()
    final_params = _params_with_exit(
        BASE_PARAMS, sl=best_sl, tp1=best_tp1, tp2_atr=best_tp2,
        trail=best_trail, hold=best_hold,
    )
    final_r    = _run_bt(preloaded, precomp_base, final_params)
    final_stats = _exit_stats(final_r.trades)
    print(f"    {_fmt_result(final_r)}  ({time.time()-t0:.1f}s)")

    elapsed_total = time.time() - t_total

    # ═══════════════════════════════════════════════════════════════════════
    # 보고서 작성
    # ═══════════════════════════════════════════════════════════════════════
    log()
    log(SEP)
    log("📋 청산 파라미터 재최적화 (v2.7, 5종목, 2014~2026)")
    log(SEP)
    log()

    # ── Round 1 그리드 표 ──────────────────────────────────────────────────
    log("■ Round 1: SL × Trail 그리드 (TP1=1.5, TP2=4.0 고정 / PF 표시)")
    log()
    hdr = f"  SL\\Trail " + "".join(f"   {tr:.1f}" for tr in trail_values)
    log(hdr)
    log("  " + SEP2[:len(hdr) - 2])
    for sl in sl_values:
        row_parts = []
        for tr in trail_values:
            if tr < sl:
                row_parts.append("    -  ")
            else:
                r = r1[(sl, tr)]
                score = evaluate(r)
                star = "⭐" if (sl, tr) == best_r1_key else "  "
                row_parts.append(f" {r.profit_factor:4.2f}{star}")
        log(f"  {sl:.1f}      " + "".join(row_parts))
    log()

    log(f"  → 최적: SL={best_sl:.1f}, Trail={best_trail:.1f}")
    r1b = r1[best_r1_key]
    log(f"    {_fmt_result(r1b)}")
    log()

    # ── Round 2 그리드 표 ──────────────────────────────────────────────────
    log(f"■ Round 2: TP1 × TP2 그리드 (SL={best_sl:.1f}/Trail={best_trail:.1f} 고정 / PF 표시)")
    log()
    hdr = f"  TP1\\TP2 " + "".join(f"   {tp2:.1f}" for tp2 in tp2_values)
    log(hdr)
    log("  " + SEP2[:len(hdr) - 2])
    for tp1 in tp1_values:
        row_parts = []
        for tp2 in tp2_values:
            if tp1 >= tp2:
                row_parts.append("    -  ")
            else:
                r = r2[(tp1, tp2)]
                star = "⭐" if (tp1, tp2) == best_r2_key else "  "
                row_parts.append(f" {r.profit_factor:4.2f}{star}")
        log(f"  {tp1:.1f}     " + "".join(row_parts))
    log()

    log(f"  → 최적: TP1={best_tp1:.1f}, TP2={best_tp2:.1f}")
    log(f"    {_fmt_result(r2[best_r2_key])}")
    log()

    # ── Round 3 보유 기간 ──────────────────────────────────────────────────
    log(f"■ Round 3: 보유 기간 (SL={best_sl:.1f}/TP1={best_tp1:.1f}/"
        f"TP2={best_tp2:.1f}/Trail={best_trail:.1f} 고정 / PF 표시)")
    log()
    log("  Hold   건수     WR      PF    CAGR     MDD    점수")
    log("  " + SEP2[:55])
    for hold in hold_values:
        r = r3[hold]
        sc = evaluate(r)
        star = "⭐" if hold == best_hold else "  "
        mark = " ←현행" if hold == 20 else ""
        log(f"  {hold:2d}일 {star}  {r.total_trades:4d}  {r.win_rate*100:5.1f}%  "
            f"{r.profit_factor:4.2f}  {r.cagr_pct*100:+5.1f}%  "
            f"-{r.max_drawdown_pct*100:4.1f}%  {sc:.3f}{mark}")
    log()

    # ── 최종 최적 조합 ─────────────────────────────────────────────────────
    log("■ 최종 최적 조합")
    log(SEP2)
    log(f"  SL={best_sl:.1f} / TP1={best_tp1:.1f} / TP2={best_tp2:.1f} / "
        f"Trail={best_trail:.1f} / Hold={best_hold}일")
    log(f"  {_fmt_result(final_r)}")
    log()

    # ── 현행 대비 ──────────────────────────────────────────────────────────
    log("■ 현행 대비")
    log(SEP2)
    bl_cagr_mdd = (baseline_r.cagr_pct / abs(baseline_r.max_drawdown_pct)
                   if baseline_r.max_drawdown_pct != 0 else 0.0)
    fn_cagr_mdd = (final_r.cagr_pct / abs(final_r.max_drawdown_pct)
                   if final_r.max_drawdown_pct != 0 else 0.0)
    log(f"  현행 (SL3/TP1.5/TP4/Tr3): {_fmt_result(baseline_r)}")
    log(f"  최적               : {_fmt_result(final_r)}")
    log(f"  CAGR/MDD 개선       : {bl_cagr_mdd:.3f} → {fn_cagr_mdd:.3f} "
        f"({'개선' if fn_cagr_mdd > bl_cagr_mdd else '유지/악화'})")
    log()

    # ── 청산 사유별 통계 ───────────────────────────────────────────────────
    REASON_LABELS = {
        "stop_loss":        "stop_loss    ",
        "trailing_stop":    "trailing_stop",
        "partial_target":   "tp1 (partial)",
        "partial_target_2": "tp2 (partial)",
        "trend_exit":       "trend_exit   ",
        "max_hold":         "time_exit    ",
        "early_time_exit":  "early_time   ",
    }

    def _fmt_stats_block(title: str, stats: dict) -> None:
        log(f"■ 청산 사유별 통계 ({title})")
        log(SEP2)
        all_reasons = set(stats.keys()) | set(REASON_LABELS.keys())
        for reason in sorted(all_reasons):
            if reason not in stats:
                continue
            s = stats[reason]
            label = REASON_LABELS.get(reason, reason.ljust(13))
            sign  = "+" if s["avg_pnl"] >= 0 else ""
            flag  = ""
            if reason == "trailing_stop":
                flag = "  ← 양수=정상(이익보호)" if s["avg_pnl"] >= 0 else "  ← ⚠ 음수(SL 역할)"
            log(f"  {label}: {s['count']:4d}건  평균 PnL {sign}{s['avg_pnl']:+,.0f}원{flag}")
        log()

    _fmt_stats_block("현행 v2.7", baseline_stats)
    _fmt_stats_block(
        f"최적 SL{best_sl:.1f}/TP1{best_tp1:.1f}/TP2{best_tp2:.1f}/Tr{best_trail:.1f}",
        final_stats,
    )

    # ── 진단: trailing_stop PnL 요약 ──────────────────────────────────────
    log("■ 핵심 검증: trailing_stop 평균 PnL")
    log(SEP2)
    bl_trail = baseline_stats.get("trailing_stop", {})
    fn_trail = final_stats.get("trailing_stop", {})
    bl_sign = "✅ 양수 (이익 보호)" if bl_trail.get("avg_pnl", -1) >= 0 else "❌ 음수 (SL 역할)"
    fn_sign = "✅ 양수 (이익 보호)" if fn_trail.get("avg_pnl", -1) >= 0 else "❌ 음수 (SL 역할)"
    log(f"  현행 trailing_stop: {bl_trail.get('avg_pnl', 0):+,.0f}원  {bl_sign}")
    log(f"  최적 trailing_stop: {fn_trail.get('avg_pnl', 0):+,.0f}원  {fn_sign}")
    log()

    # ── Summary ────────────────────────────────────────────────────────────
    log(SEP)
    log(f"총 소요 시간: {elapsed_total:.1f}초")
    log()
    log("결론:")
    if final_r.profit_factor >= baseline_r.profit_factor:
        log(f"  ✅ 최적 조합 PF {final_r.profit_factor:.2f} ≥ 현행 PF {baseline_r.profit_factor:.2f}")
    else:
        log(f"  ⚠ 최적 조합 PF {final_r.profit_factor:.2f} < 현행 PF {baseline_r.profit_factor:.2f} (현행 유지 검토)")
    if fn_trail.get("avg_pnl", -1) >= 0:
        log(f"  ✅ trailing_stop 평균 PnL 양수 ({fn_trail.get('avg_pnl', 0):+,.0f}원)")
    else:
        log(f"  ⚠ trailing_stop 평균 PnL 음수 — Trail이 SL 역할 중")

    # ── 파일 저장 ──────────────────────────────────────────────────────────
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()

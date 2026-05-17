"""파라미터 최적화 — IS 그리드 서치 + Walk-Forward OOS 검증.

[분할]
  IS:  2014-01-02 ~ 2021-12-31  (8년)
  OOS: 2022-01-01 ~ 2026-05-15  (4년+)

[Phase 1]
  Round 1: 청산 파라미터 (4×4×5×4 = 320 조합)
            — precomp 1회 재사용 (청산 파라미터는 진입 조건에 영향 없음)
  Round 2: 진입 필터 + 랭킹 (3×3×4×3×4 = 432 조합, Round 1 최적 청산 고정)
            — precomp을 (adx, rs_threshold, ranking_rs) 27개 그룹으로 묶어 재사용

[Phase 2]  IS Top 10 → OOS 검증 (견고성 확인)
[Phase 3]  최종 선택 조합 → 전체 기간 백테스트 → 기준선 비교

실행:
    python experiments/experiment_param_optimize.py
"""
import sys
import time
from dataclasses import replace
from itertools import product
from pathlib import Path

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
from src.strategy.ranking import RankingWeights
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.scaling import ScalingParams
from src.utils.slippage_model import SlippageParams

# ─────────────────────────────────────────────────────────────────────────────
# 고정 설정 (기준선: A+B1+B2+B6, 10M/6종목)
# ─────────────────────────────────────────────────────────────────────────────
IS_CUTOFF  = "2021-12-31"
OOS_START  = "2022-01-01"

CAPITAL     = 10_000_000
MIN_AMOUNT  = 300_000
MIN_TRADES  = 200          # IS 기간 최소 거래 수 (미달 시 과최적화 위험)

# v2.6 기준값
BASE_PARAMS = StrategyParams(
    tp1_sell_ratio=0.10,
    tp2_atr=4.0,
    tp2_sell_ratio=0.10,
)
CURRENT_BREADTH  = 0.40
CURRENT_MAX_POS  = 6
CURRENT_RANK_RS  = 0.35

COST    = CostModel()
SECTOR  = SectorConstraint(enabled=False)
DYNHOLD = DynamicHoldParams(enabled=False)
SCALING = ScalingParams(enabled=False)
SLIP    = SlippageParams(
    enabled=True,
    base_slippage=0.0003,
    impact_coefficient=0.1,
    max_slippage=0.02,
)

# ─────────────────────────────────────────────────────────────────────────────
# 그리드 정의
# ─────────────────────────────────────────────────────────────────────────────
ROUND1_GRID = {
    "stop_loss_atr":   [1.5, 2.0, 2.5, 3.0],
    "take_profit_atr": [1.5, 2.0, 2.5, 3.0],
    "trailing_atr":    [2.5, 3.0, 3.5, 4.0, 5.0],
    "max_hold_days":   [15, 20, 25, 30],
}
# 4 × 4 × 5 × 4 = 320

ROUND2_GRID = {
    "adx_threshold":               [15.0, 20.0, 25.0],
    "relative_strength_threshold": [0.02, 0.05, 0.08],
    "breadth_gate":                [0.30, 0.35, 0.40, 0.45],
    "ranking_rs":                  [0.20, 0.35, 0.50],
    "max_positions":               [4, 5, 6, 8],
}
# 3 × 3 × 4 × 3 × 4 = 432


# ─────────────────────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _cagr_mdd(cagr_pct: float, mdd_pct: float) -> float:
    """CAGR/MDD 비율. mdd_pct는 양수(magnitude). 0이면 0 반환."""
    return cagr_pct / mdd_pct if mdd_pct > 0 else 0.0


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _filtered_preloaded(preloaded: dict, dates: list) -> dict:
    """trading_dates만 교체한 얕은 복사본 반환 (ticker_data 등은 공유)."""
    out = dict(preloaded)
    out["trading_dates"] = dates
    return out


def _compute_precomp(dates: list, preloaded: dict,
                     params: StrategyParams,
                     weights: RankingWeights) -> dict:
    return precompute_daily_signals(
        dates,
        preloaded["ticker_data"],
        preloaded["ticker_date_idx"],
        set(preloaded["initial_universe"]),
        params=params,
        kospi_ret_map=preloaded.get("kospi_ret_map"),
        kosdaq_ret_map=preloaded.get("kosdaq_ret_map"),
        ticker_market=preloaded.get("ticker_market"),
        weights=weights,
    )


def _run_bt(preloaded: dict, precomp: dict,
            params: StrategyParams,
            max_pos: int, breadth: float):
    return run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=max_pos,
        params=params,
        cost=COST,
        min_position_amount=MIN_AMOUNT,
        preloaded_data=preloaded,
        precomputed=precomp,
        sizing_mode="equity",
        breadth_gate_threshold=breadth,
        regime_gate_enabled=True,
        sector_constraint=SECTOR,
        dynamic_hold=DYNHOLD,
        scaling=SCALING,
        slippage_params=SLIP,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    out_path = ROOT / "experiments" / "results_param_optimize.txt"
    SEP  = "=" * 75
    SEP2 = "-" * 75

    output_lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        output_lines.append(msg)

    t_total = time.time()

    # ── 1. 데이터 로드 ────────────────────────────────────────────────────────
    print(f"\n[1/7] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    print(f"  완료: {time.time() - t0:.1f}s")

    all_dates = preloaded["trading_dates"]
    is_dates  = [d for d in all_dates if d <= IS_CUTOFF]
    oos_dates = [d for d in all_dates if d >= OOS_START]

    is_preloaded  = _filtered_preloaded(preloaded, is_dates)
    oos_preloaded = _filtered_preloaded(preloaded, oos_dates)

    r1_combos = list(product(
        ROUND1_GRID["stop_loss_atr"],
        ROUND1_GRID["take_profit_atr"],
        ROUND1_GRID["trailing_atr"],
        ROUND1_GRID["max_hold_days"],
    ))
    r2_combos = list(product(
        ROUND2_GRID["adx_threshold"],
        ROUND2_GRID["relative_strength_threshold"],
        ROUND2_GRID["breadth_gate"],
        ROUND2_GRID["ranking_rs"],
        ROUND2_GRID["max_positions"],
    ))
    precomp_groups = list(product(
        ROUND2_GRID["adx_threshold"],
        ROUND2_GRID["relative_strength_threshold"],
        ROUND2_GRID["ranking_rs"],
    ))

    print(
        f"  전체 {len(all_dates)}일  |  IS {len(is_dates)}일 ({is_dates[0]}~{is_dates[-1]})"
        f"  |  OOS {len(oos_dates)}일 ({oos_dates[0]}~{oos_dates[-1]})"
    )
    print(f"  Round 1: {len(r1_combos)}조합  |  Round 2: {len(r2_combos)}조합 ({len(precomp_groups)} precomp 그룹)")

    # ── 2. Round 1 — IS precomp (1회) ─────────────────────────────────────
    print(f"\n[2/7] Round 1 IS precomp (1회)...")
    t0 = time.time()
    base_weights    = RankingWeights()
    is_precomp_base = _compute_precomp(is_dates, preloaded, BASE_PARAMS, base_weights)
    print(f"  완료: {time.time() - t0:.1f}s")

    # ── 3. Round 1 — 청산 파라미터 그리드 ─────────────────────────────────
    print(f"\n[3/7] Round 1 — 청산 파라미터 그리드 ({len(r1_combos)}조합)...")
    r1_results: list[dict] = []
    t0 = time.time()

    for i, (sl, tp, trail, hold) in enumerate(r1_combos):
        p = replace(BASE_PARAMS,
                    stop_loss_atr=sl,
                    take_profit_atr=tp,
                    trailing_atr=trail,
                    max_hold_days=hold)
        r = _run_bt(is_preloaded, is_precomp_base, p,
                    max_pos=CURRENT_MAX_POS, breadth=CURRENT_BREADTH)
        r1_results.append({
            "sl": sl, "tp": tp, "trail": trail, "hold": hold,
            "pf":  r.profit_factor,
            "cagr": r.cagr_pct,
            "mdd":  r.max_drawdown_pct,
            "wr":   r.win_rate,
            "trades": r.total_trades,
            "cagr_mdd": _cagr_mdd(r.cagr_pct, r.max_drawdown_pct),
            "params": p,
        })
        if (i + 1) % 50 == 0:
            el = time.time() - t0
            eta = el / (i + 1) * (len(r1_combos) - i - 1)
            print(f"  {i+1}/{len(r1_combos)}  {el:.0f}s 경과  ETA {eta:.0f}s")

    print(f"  완료: {time.time() - t0:.1f}s")

    r1_valid  = [r for r in r1_results if r["trades"] >= MIN_TRADES]
    r1_sorted = sorted(r1_valid, key=lambda x: x["cagr_mdd"], reverse=True)
    best_exit = r1_sorted[0]

    # Round 1 현재값 위치
    r1_current = next(
        (r for r in r1_results
         if r["sl"] == 2.0 and r["tp"] == 2.0
         and r["trail"] == 4.0 and r["hold"] == 20),
        None,
    )

    print(f"  최적 청산: SL={best_exit['sl']} TP={best_exit['tp']} "
          f"Trail={best_exit['trail']} Hold={best_exit['hold']}  "
          f"CAGR/MDD={best_exit['cagr_mdd']:.2f}  건수={best_exit['trades']}")

    # ── 4. Round 2 — precomp 27그룹 계산 ──────────────────────────────────
    print(f"\n[4/7] Round 2 precomp ({len(precomp_groups)}그룹)...")
    BEST_EXIT_PARAMS = replace(BASE_PARAMS,
                               stop_loss_atr=best_exit["sl"],
                               take_profit_atr=best_exit["tp"],
                               trailing_atr=best_exit["trail"],
                               max_hold_days=best_exit["hold"])

    precomp_cache: dict[tuple, dict] = {}
    t0 = time.time()
    for gi, (adx, rs_thr, rk_rs) in enumerate(precomp_groups):
        g_params  = replace(BEST_EXIT_PARAMS,
                            adx_threshold=adx,
                            relative_strength_threshold=rs_thr)
        g_weights = RankingWeights(rs=rk_rs)
        precomp_cache[(adx, rs_thr, rk_rs)] = _compute_precomp(
            is_dates, preloaded, g_params, g_weights,
        )
        if (gi + 1) % 9 == 0:
            print(f"  {gi+1}/{len(precomp_groups)} 완료  ({time.time()-t0:.0f}s)")

    print(f"  완료: {time.time() - t0:.1f}s")

    # ── 5. Round 2 — 진입/랭킹 그리드 ────────────────────────────────────
    print(f"\n[5/7] Round 2 — 진입/랭킹 그리드 ({len(r2_combos)}조합)...")
    r2_results: list[dict] = []
    t0 = time.time()

    for i, (adx, rs_thr, breadth, rk_rs, max_pos) in enumerate(r2_combos):
        p = replace(BEST_EXIT_PARAMS,
                    adx_threshold=adx,
                    relative_strength_threshold=rs_thr)
        precomp = precomp_cache[(adx, rs_thr, rk_rs)]
        r = _run_bt(is_preloaded, precomp, p,
                    max_pos=max_pos, breadth=breadth)
        r2_results.append({
            "adx": adx, "rs_thr": rs_thr, "breadth": breadth,
            "rk_rs": rk_rs, "max_pos": max_pos,
            "pf":  r.profit_factor,
            "cagr": r.cagr_pct,
            "mdd":  r.max_drawdown_pct,
            "wr":   r.win_rate,
            "trades": r.total_trades,
            "cagr_mdd": _cagr_mdd(r.cagr_pct, r.max_drawdown_pct),
            "params":  p,
            "weights": RankingWeights(rs=rk_rs),
        })
        if (i + 1) % 50 == 0:
            el = time.time() - t0
            eta = el / (i + 1) * (len(r2_combos) - i - 1)
            print(f"  {i+1}/{len(r2_combos)}  {el:.0f}s 경과  ETA {eta:.0f}s")

    print(f"  완료: {time.time() - t0:.1f}s")

    r2_valid  = [r for r in r2_results if r["trades"] >= MIN_TRADES]
    r2_sorted = sorted(r2_valid, key=lambda x: x["cagr_mdd"], reverse=True)
    top10_is  = r2_sorted[:10]

    r2_current = next(
        (r for r in r2_results
         if r["adx"]    == 20.0
         and abs(r["rs_thr"] - 0.05) < 1e-9
         and abs(r["breadth"] - CURRENT_BREADTH) < 1e-9
         and abs(r["rk_rs"]  - CURRENT_RANK_RS) < 1e-9
         and r["max_pos"] == CURRENT_MAX_POS),
        None,
    )

    # ── 6. Phase 2 — OOS 검증 ─────────────────────────────────────────────
    print(f"\n[6/7] Phase 2 OOS 검증 (IS Top 10)...")
    oos_results: list[dict] = []

    for ci, combo in enumerate(top10_is):
        c_params  = combo["params"]
        c_weights = combo["weights"]
        oos_pc = _compute_precomp(oos_dates, preloaded, c_params, c_weights)
        r_oos  = _run_bt(oos_preloaded, oos_pc, c_params,
                         max_pos=combo["max_pos"], breadth=combo["breadth"])
        cm_oos    = _cagr_mdd(r_oos.cagr_pct, r_oos.max_drawdown_pct)
        retention = (cm_oos / combo["cagr_mdd"] * 100) if combo["cagr_mdd"] > 0 else 0.0
        robust    = retention >= 50.0
        oos_results.append({
            **combo,
            "oos_pf":     r_oos.profit_factor,
            "oos_cagr":   r_oos.cagr_pct,
            "oos_mdd":    r_oos.max_drawdown_pct,
            "oos_trades": r_oos.total_trades,
            "oos_cagr_mdd": cm_oos,
            "retention":  retention,
            "robust":     robust,
        })
        mark = "✅" if robust else "❌"
        print(
            f"  #{ci+1:2d}  OOS: PF={_fmt_pf(r_oos.profit_factor)}  "
            f"CAGR={r_oos.cagr_pct*100:+.1f}%  MDD=-{r_oos.max_drawdown_pct*100:.1f}%  "
            f"유지율={retention:.0f}%  {mark}"
        )

    # 최종 선택: 견고한 조합 중 IS CAGR/MDD 최고
    robust_list = [r for r in oos_results if r["robust"]]
    if not robust_list:
        print("  ⚠ 견고 조합 없음 — OOS CAGR/MDD 최고 조합 선택")
        best_combo = max(oos_results, key=lambda x: x["oos_cagr_mdd"])
    else:
        best_combo = max(robust_list, key=lambda x: x["cagr_mdd"])

    # ── 7. Phase 3 — 전체 기간 최종 확인 ─────────────────────────────────
    print(f"\n[7/7] Phase 3 전체 기간 최종 확인...")
    t0 = time.time()
    full_precomp = _compute_precomp(
        all_dates, preloaded,
        best_combo["params"], best_combo["weights"],
    )
    r_full = _run_bt(preloaded, full_precomp,
                     best_combo["params"],
                     max_pos=best_combo["max_pos"],
                     breadth=best_combo["breadth"])

    # 기준선 (현재 파라미터 전체 기간)
    base_full_pc = _compute_precomp(all_dates, preloaded, BASE_PARAMS, RankingWeights())
    r_base = _run_bt(preloaded, base_full_pc,
                     BASE_PARAMS,
                     max_pos=CURRENT_MAX_POS,
                     breadth=CURRENT_BREADTH)
    print(f"  완료: {time.time() - t0:.1f}s")

    # ─────────────────────────────────────────────────────────────────────────
    # 보고서 작성
    # ─────────────────────────────────────────────────────────────────────────
    log(SEP)
    log("파라미터 최적화 결과 — IS 그리드 서치 + Walk-Forward OOS 검증")
    log(f"  IS : {is_dates[0]} ~ {is_dates[-1]}  ({len(is_dates)}일)")
    log(f"  OOS: {oos_dates[0]} ~ {oos_dates[-1]}  ({len(oos_dates)}일)")
    log(SEP)

    # ── Round 1 결과 ──────────────────────────────────────────────────────
    log("")
    log("■ Round 1 — 청산 파라미터 (IS: 2014~2021)")
    log(f"  그리드: SL {ROUND1_GRID['stop_loss_atr']} × TP {ROUND1_GRID['take_profit_atr']}"
        f" × Trail {ROUND1_GRID['trailing_atr']} × Hold {ROUND1_GRID['max_hold_days']}")
    log(f"  총 {len(r1_combos)}조합  →  유효({MIN_TRADES}건+): {len(r1_valid)}개")
    log("")
    log("  Top 5 (CAGR/MDD 기준):")
    log("   #    SL    TP  Trail  Hold     PF    CAGR      MDD  CAGR/MDD  건수")
    log("  " + "-" * 68)
    for i, row in enumerate(r1_sorted[:5], 1):
        log(
            f"  {i}  {row['sl']:4.1f}  {row['tp']:4.1f}  {row['trail']:4.1f}  "
            f"  {row['hold']:3d}  {_fmt_pf(row['pf']):>5}  "
            f"{row['cagr']*100:+5.1f}%  -{row['mdd']*100:4.1f}%  "
            f"  {row['cagr_mdd']:5.2f}    {row['trades']:4d}"
        )
    log("")
    if r1_current:
        cur_cm = r1_current["cagr_mdd"]
        imp = (best_exit["cagr_mdd"] / cur_cm - 1) * 100 if cur_cm > 0 else float("nan")
        log(f"  현재값 (SL=2.0 TP=2.0 Trail=4.0 Hold=20):")
        log(
            f"    PF={_fmt_pf(r1_current['pf'])}  CAGR={r1_current['cagr']*100:+.1f}%  "
            f"MDD=-{r1_current['mdd']*100:.1f}%  CAGR/MDD={cur_cm:.2f}  건수={r1_current['trades']}"
        )
        log(f"    최적 대비 CAGR/MDD 개선: {imp:+.1f}%")
    log("")
    log(f"  → Round 2 고정 청산: SL={best_exit['sl']} / TP={best_exit['tp']}"
        f" / Trail={best_exit['trail']} / Hold={best_exit['hold']}")

    # ── Round 2 결과 ──────────────────────────────────────────────────────
    log("")
    log("■ Round 2 — 진입 필터 + 랭킹 (IS: 2014~2021, Round 1 최적 청산 고정)")
    log(f"  그리드: ADX {ROUND2_GRID['adx_threshold']} × RS {ROUND2_GRID['relative_strength_threshold']}"
        f" × Breadth {ROUND2_GRID['breadth_gate']}"
        f" × RankRS {ROUND2_GRID['ranking_rs']} × MaxPos {ROUND2_GRID['max_positions']}")
    log(f"  총 {len(r2_combos)}조합  →  유효({MIN_TRADES}건+): {len(r2_valid)}개")
    log("")
    log("  Top 10 (CAGR/MDD 기준):")
    log("   #   ADX    RS   Brd  RkRS  Pos     PF    CAGR      MDD  CAGR/MDD  건수")
    log("  " + "-" * 73)
    for i, row in enumerate(r2_sorted[:10], 1):
        log(
            f"  {i:2d}  {row['adx']:3.0f}  {row['rs_thr']:.2f}  "
            f"{row['breadth']:.2f}  {row['rk_rs']:.2f}  {row['max_pos']:3d}  "
            f"{_fmt_pf(row['pf']):>5}  "
            f"{row['cagr']*100:+5.1f}%  -{row['mdd']*100:4.1f}%  "
            f"  {row['cagr_mdd']:5.2f}    {row['trades']:4d}"
        )
    log("")
    if r2_current:
        log(
            f"  현재값 (ADX=20 RS=0.05 Brd=0.40 RkRS=0.35 Pos=6):\n"
            f"    PF={_fmt_pf(r2_current['pf'])}  CAGR={r2_current['cagr']*100:+.1f}%  "
            f"MDD=-{r2_current['mdd']*100:.1f}%  CAGR/MDD={r2_current['cagr_mdd']:.2f}  "
            f"건수={r2_current['trades']}"
        )

    # ── Phase 2 OOS 결과 ──────────────────────────────────────────────────
    log("")
    log("■ Phase 2 — OOS 검증 (2022~2026, IS Top 10)")
    log("   #   ADX    RS   Brd  RkRS  Pos  IS CAGR/MDD  OOS CAGR/MDD  유지율  견고")
    log("  " + "-" * 75)
    for i, row in enumerate(oos_results, 1):
        mark = "✅" if row["robust"] else "❌"
        log(
            f"  {i:2d}  {row['adx']:3.0f}  {row['rs_thr']:.2f}  "
            f"{row['breadth']:.2f}  {row['rk_rs']:.2f}  {row['max_pos']:3d}  "
            f"     {row['cagr_mdd']:5.2f}       {row['oos_cagr_mdd']:5.2f}  "
            f"   {row['retention']:5.0f}%   {mark}"
        )
    robust_cnt = sum(1 for r in oos_results if r["robust"])
    log(f"\n  견고한 조합: {robust_cnt}/{len(oos_results)}개  (유지율 50%+)")
    log(f"  선택: #{oos_results.index(best_combo) + 1}  "
        f"(ADX={best_combo['adx']} RS={best_combo['rs_thr']} "
        f"Brd={best_combo['breadth']} RkRS={best_combo['rk_rs']} Pos={best_combo['max_pos']})")

    # ── Phase 3 전체 기간 결과 ────────────────────────────────────────────
    log("")
    log("■ Phase 3 — 전체 기간 최종 확인 (2014~2026)")
    log(SEP2)
    pnl_base = r_base.final_capital - r_base.initial_capital
    pnl_opt  = r_full.final_capital - r_full.initial_capital
    dcagr = (r_full.cagr_pct    - r_base.cagr_pct)    * 100
    dmdd  = (r_base.max_drawdown_pct - r_full.max_drawdown_pct) * 100  # 개선이면 양수
    dpf   = r_full.profit_factor - r_base.profit_factor

    log(f"  기존 기준선 (현재 파라미터, 전체 기간):")
    log(
        f"    건수={r_base.total_trades}  WR={r_base.win_rate*100:.1f}%  "
        f"PF={_fmt_pf(r_base.profit_factor)}  CAGR={r_base.cagr_pct*100:+.1f}%  "
        f"MDD=-{r_base.max_drawdown_pct*100:.1f}%  순손익={pnl_base:+,.0f}원"
    )
    log(f"  최적 조합 (전체 기간):")
    log(
        f"    건수={r_full.total_trades}  WR={r_full.win_rate*100:.1f}%  "
        f"PF={_fmt_pf(r_full.profit_factor)}  CAGR={r_full.cagr_pct*100:+.1f}%  "
        f"MDD=-{r_full.max_drawdown_pct*100:.1f}%  순손익={pnl_opt:+,.0f}원"
    )
    log(f"  개선: ΔPF {dpf:+.2f}  ΔCAGR {dcagr:+.1f}%p  ΔMDD {dmdd:+.1f}%p (양수=MDD 감소)")

    # ── 최종 권장 파라미터 ────────────────────────────────────────────────
    bp = best_combo["params"]
    log("")
    log(SEP)
    log("■ 최종 권장 파라미터 (config.yaml 반영 기준)")
    log(SEP)
    log(f"  # 청산 파라미터 (Round 1 최적)")
    log(f"  stop_loss_atr:    {bp.stop_loss_atr}    (현재 2.0)")
    log(f"  take_profit_atr:  {bp.take_profit_atr}    (현재 2.0)")
    log(f"  trailing_atr:     {bp.trailing_atr}    (현재 4.0)")
    log(f"  max_hold_days:    {bp.max_hold_days}     (현재 20)")
    log(f"")
    log(f"  # 진입 필터 (Round 2 최적)")
    log(f"  adx_threshold:                  {bp.adx_threshold}    (현재 20)")
    log(f"  relative_strength_threshold:    {bp.relative_strength_threshold}  (현재 0.05)")
    log(f"")
    log(f"  # 국면/랭킹/포지션 (Round 2 최적)")
    log(f"  breadth_gate_threshold:         {best_combo['breadth']}  (현재 0.40)")
    log(f"  ranking_weights.rs:             {best_combo['rk_rs']}  (현재 0.35)")
    log(f"  max_positions:                  {best_combo['max_pos']}     (현재 6)")
    log(f"")
    log(f"  # Walk-Forward 지표")
    log(f"  IS  CAGR/MDD: {best_combo['cagr_mdd']:.2f}")
    log(f"  OOS CAGR/MDD: {best_combo['oos_cagr_mdd']:.2f}  (유지율 {best_combo['retention']:.0f}%)")
    log(f"  견고성: {'✅ 유지율 50%+' if best_combo['robust'] else '⚠ 유지율 50% 미만'}")
    log("")
    log(f"  총 실행 시간: {(time.time() - t_total) / 60:.1f}분")
    log(SEP)

    with open(out_path, "w", encoding="utf-8") as f:
        for line in output_lines:
            f.write(line + "\n")

    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()

"""Walk-Forward 비교 — 현행 청산 파라미터 vs 재최적화 결과.

[A] 현행: SL=3.0, TP1=1.5, TP2=4.0, Trail=3.0
[B] 최적: SL=2.5, TP1=1.0, TP2=6.0, Trail=4.0

진입 파라미터(ADX=25, RS=0.08 등)는 공통 고정.
precompute_daily_signals는 진입 파라미터만 의존하므로 윈도우당 1회만 계산 후 공유.

설정: 2년 Train / 1년 Test / 12개월 Step

Usage:
    python experiments/run_wf_new_exit.py
결과:
    experiments/results_wf_new_exit.txt
"""
from __future__ import annotations

import dataclasses
import sys
import time
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
from src.backtest.walk_forward import (
    WFResult,
    build_summary,
    generate_windows,
)
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.ranking import RankingWeights
from src.strategy.scaling import ScalingParams
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.cost_model import CostModel
from src.utils.slippage_model import SlippageParams


# ─────────────────────────────────────────────────────────────────────────────
# 공통 설정 (진입 파라미터 고정)
# ─────────────────────────────────────────────────────────────────────────────

DATA_START = "2014-01-02"
DATA_END   = "2026-05-15"

CAPITAL    = 10_000_000
MAX_POS    = 5
MIN_AMOUNT = 300_000
BREADTH    = 0.40

# 진입 공통 파라미터 — 청산만 교체할 기준
_ENTRY_BASE = StrategyParams(
    adx_threshold=25.0,
    relative_strength_threshold=0.08,
    tp1_sell_ratio=0.10,
    tp2_sell_ratio=0.10,
)

# [A] 현행 v2.7
PARAMS_A = dataclasses.replace(
    _ENTRY_BASE,
    stop_loss_atr=3.0,
    take_profit_atr=1.5,
    tp2_atr=4.0,
    trailing_atr=3.0,
)

# [B] 재최적화 (SL2.5/TP1.0/TP6.0/Tr4.0)
PARAMS_B = dataclasses.replace(
    _ENTRY_BASE,
    stop_loss_atr=2.5,
    take_profit_atr=1.0,
    tp2_atr=6.0,
    trailing_atr=4.0,
)

WEIGHTS = RankingWeights(
    rs=0.50,
    momentum_atr=0.20,
    adx=0.15,
    liquidity=0.10,
    ma_alignment=0.05,
)

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
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _filtered(preloaded: dict, dates: list[str]) -> dict:
    out = dict(preloaded)
    out["trading_dates"] = dates
    return out


def _precomp(dates: list[str], preloaded: dict) -> dict:
    """진입 파라미터 기반 precompute — [A]/[B] 공유."""
    return precompute_daily_signals(
        dates,
        preloaded["ticker_data"],
        preloaded["ticker_date_idx"],
        set(preloaded["initial_universe"]),
        params=_ENTRY_BASE,
        kospi_ret_map=preloaded.get("kospi_ret_map"),
        kosdaq_ret_map=preloaded.get("kosdaq_ret_map"),
        ticker_market=preloaded.get("ticker_market"),
        weights=WEIGHTS,
    )


def _run_bt(preloaded: dict, precomp: dict, params: StrategyParams):
    return run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=params,
        cost=COST,
        min_position_amount=MIN_AMOUNT,
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


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    out_path = ROOT / "experiments" / "results_wf_new_exit.txt"
    lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    SEP  = "═" * 72
    SEP2 = "─" * 72

    t_total = time.time()

    # ── 1. 윈도우 생성 ───────────────────────────────────────────────────────
    windows = generate_windows(DATA_START, DATA_END, train_years=2, test_years=1, step_months=12)
    print(f"[1/3] 윈도우 생성: {len(windows)}개")
    for i, w in enumerate(windows, 1):
        print(f"  {i:>2}. Train {w.train_start}~{w.train_end}  │  Test {w.test_start}~{w.test_end}")

    # ── 2. 데이터 로드 ───────────────────────────────────────────────────────
    print(f"\n[2/3] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(_ENTRY_BASE)
    all_dates: list[str] = preloaded["trading_dates"]
    print(f"  완료: {time.time() - t0:.1f}s  (거래일 {len(all_dates)}개)")

    # ── 3. 윈도우별 [A] + [B] 실행 ──────────────────────────────────────────
    print(f"\n[3/3] Walk-Forward 실행 ([A] 현행 vs [B] 최적)...")
    results_a: list[WFResult] = []
    results_b: list[WFResult] = []

    for i, w in enumerate(windows, 1):
        train_dates = [d for d in all_dates if w.train_start <= d <= w.train_end]
        test_dates  = [d for d in all_dates if w.test_start  <= d <= w.test_end]
        if not train_dates or not test_dates:
            print(f"  [{i}/{len(windows)}] 데이터 없음 — 건너뜀")
            continue

        t_win = time.time()

        # precompute: 진입 파라미터만 의존 → [A]/[B] 공유
        train_pl  = _filtered(preloaded, train_dates)
        train_pc  = _precomp(train_dates, train_pl)
        test_pl   = _filtered(preloaded, test_dates)
        test_pc   = _precomp(test_dates, test_pl)

        # [A] 현행
        tr_a = _run_bt(train_pl, train_pc, PARAMS_A)
        te_a = _run_bt(test_pl,  test_pc,  PARAMS_A)
        wf_a = WFResult(
            window=w,
            train_pf=tr_a.profit_factor, train_cagr=tr_a.cagr_pct,
            train_mdd=abs(tr_a.max_drawdown_pct), train_trades=tr_a.total_trades,
            test_pf=te_a.profit_factor,  test_cagr=te_a.cagr_pct,
            test_mdd=abs(te_a.max_drawdown_pct),  test_trades=te_a.total_trades,
        )
        results_a.append(wf_a)

        # [B] 최적
        tr_b = _run_bt(train_pl, train_pc, PARAMS_B)
        te_b = _run_bt(test_pl,  test_pc,  PARAMS_B)
        wf_b = WFResult(
            window=w,
            train_pf=tr_b.profit_factor, train_cagr=tr_b.cagr_pct,
            train_mdd=abs(tr_b.max_drawdown_pct), train_trades=tr_b.total_trades,
            test_pf=te_b.profit_factor,  test_cagr=te_b.cagr_pct,
            test_mdd=abs(te_b.max_drawdown_pct),  test_trades=te_b.total_trades,
        )
        results_b.append(wf_b)

        elapsed_win = time.time() - t_win
        rob_a = "✅" if wf_a.is_robust else "❌"
        rob_b = "✅" if wf_b.is_robust else "❌"
        print(
            f"  [{i:>2}/{len(windows)}] Test {w.test_start[:7]}~{w.test_end[:7]}  "
            f"[A] PF {_fmt_pf(wf_a.test_pf):>5} {rob_a}  "
            f"[B] PF {_fmt_pf(wf_b.test_pf):>5} {rob_b}  "
            f"({elapsed_win:.1f}s)"
        )

    # ── 4. 요약 + 보고서 ─────────────────────────────────────────────────────
    sum_a = build_summary(results_a)
    sum_b = build_summary(results_b)
    elapsed = time.time() - t_total

    # ── 보고서 ────────────────────────────────────────────────────────────────
    log()
    log(SEP)
    log("📋 Walk-Forward 비교 (2년 Train / 1년 Test / 12개월 Step)")
    log(SEP)
    log()
    log("  [A] 현행: SL=3.0, TP1=1.5, TP2=4.0, Trail=3.0")
    log("  [B] 최적: SL=2.5, TP1=1.0, TP2=6.0, Trail=4.0")
    log()

    # 윈도우별 비교 테이블
    log("■ 윈도우별 Test PF 비교")
    hdr = (
        f"  {'#':>2}  {'Test 기간':<16}  "
        f"{'[A] Train':>9}  {'[A] Test':>8}  {'[A]견고':>5}  "
        f"{'[B] Train':>9}  {'[B] Test':>8}  {'[B]견고':>5}  {'차이':>6}"
    )
    log(hdr)
    log("  " + SEP2[:len(hdr) - 2])

    for i, (wa, wb) in enumerate(zip(results_a, results_b), 1):
        w = wa.window
        test_label = f"{w.test_start[:7]}~{w.test_end[:7]}"
        diff = wb.test_pf - wa.test_pf
        diff_str = f"{diff:+.2f}" if wb.test_pf != float("inf") else "  inf"
        rob_a = "✅" if wa.is_robust else "❌"
        rob_b = "✅" if wb.is_robust else "❌"
        log(
            f"  {i:>2}  {test_label:<16}  "
            f"{_fmt_pf(wa.train_pf):>9}  {_fmt_pf(wa.test_pf):>8}  {rob_a:>5}  "
            f"{_fmt_pf(wb.train_pf):>9}  {_fmt_pf(wb.test_pf):>8}  {rob_b:>5}  "
            f"{diff_str:>6}"
        )
        # 서브라인: CAGR/MDD
        log(
            f"       Train CAGR {wa.train_cagr*100:+.1f}% MDD {wa.train_mdd*100:.1f}% "
            f"({wa.train_trades}건)"
            f"  │  Test CAGR {wa.test_cagr*100:+.1f}% MDD {wa.test_mdd*100:.1f}% "
            f"({wa.test_trades}건)"
        )
        log(
            f"  [B] Train CAGR {wb.train_cagr*100:+.1f}% MDD {wb.train_mdd*100:.1f}% "
            f"({wb.train_trades}건)"
            f"  │  Test CAGR {wb.test_cagr*100:+.1f}% MDD {wb.test_mdd*100:.1f}% "
            f"({wb.test_trades}건)"
        )
        log("")

    log(SEP2)
    log()

    # 요약
    log("■ 요약")
    log(f"  {'항목':<18}  {'[A] 현행':>10}  {'[B] 최적':>10}  {'변화':>8}")
    log("  " + SEP2[:52])

    def _pf_str(s) -> str:
        return "inf" if s.avg_test_pf == float("inf") else f"{s.avg_test_pf:.2f}"

    rob_a_str = f"{sum_a.robust_windows}/{sum_a.total_windows}"
    rob_b_str = f"{sum_b.robust_windows}/{sum_b.total_windows}"
    rob_diff  = sum_b.robust_windows - sum_a.robust_windows
    pf_diff   = sum_b.avg_test_pf - sum_a.avg_test_pf

    log(f"  {'견고 윈도우':<18}  {rob_a_str:>10}  {rob_b_str:>10}  {rob_diff:>+8}")
    log(f"  {'평균 Test PF':<18}  {_pf_str(sum_a):>10}  {_pf_str(sum_b):>10}  {pf_diff:>+8.2f}")
    log(f"  {'판정':<18}  {sum_a.overall_verdict:>10}  {sum_b.overall_verdict:>10}")
    log()

    # 판정 설명
    verdict_desc = {
        "PASS": "70%+ 견고 — 과최적화 없음",
        "WARN": "50~70% 견고 — 부분 과최적화 의심, 페이퍼에서 재관찰",
        "FAIL": "50% 미만 견고 — 과최적화 가능성 높음, 파라미터 재검토 필요",
    }
    log(f"  [A] {sum_a.overall_verdict}: {verdict_desc[sum_a.overall_verdict]}")
    log(f"  [B] {sum_b.overall_verdict}: {verdict_desc[sum_b.overall_verdict]}")
    log()

    # 결론
    log("■ 결론")
    log(SEP2)
    if sum_b.overall_verdict == "PASS":
        log("  ✅ [B] PASS — config.yaml 적용 권장")
        log("     stop_loss_atr: 3.0→2.5 / take_profit_atr: 1.5→1.0 / tp2_atr: 4.0→6.0 / trailing_atr: 3.0→4.0")
    elif sum_b.overall_verdict == "WARN":
        if sum_b.robust_windows >= sum_a.robust_windows:
            log("  ⚠ [B] WARN (현행 이상) — 페이퍼 트레이딩에서 병행 모니터링 후 적용 결정")
            log("     개선 방향은 유효, 실전 검증 권장")
        else:
            log("  ⚠ [B] WARN (현행 미만) — 현행 [A] 유지, 부분 파라미터 조정 검토")
    else:
        log("  ❌ [B] FAIL — 현행 [A] 유지. 재최적화 IS 과적합 가능성.")
        log("     부분 조정(SL만 2.5 / Trail만 4.0) 개별 검토 권장")

    log()
    log(f"  소요 시간: {elapsed:.1f}s")
    log(SEP)

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()

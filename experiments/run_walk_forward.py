"""Walk-Forward 검증 실행 스크립트.

v2.7 파라미터 기준으로 12년 데이터를 2년 Train / 1년 Test / 12개월 Step으로
롤링 검증.

Usage:
    python experiments/run_walk_forward.py
    python experiments/run_walk_forward.py --train-years 3 --test-years 1
    python experiments/run_walk_forward.py --train-years 2 --test-years 1 --step-months 6
"""
from __future__ import annotations

import argparse
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
# 고정 설정 — v2.7 확정 파라미터
# ─────────────────────────────────────────────────────────────────────────────

DATA_START = "2014-01-02"
DATA_END   = "2026-05-15"

CAPITAL    = 10_000_000
MAX_POS    = 5
MIN_AMOUNT = 300_000
BREADTH    = 0.40

BASE_PARAMS = StrategyParams(
    adx_threshold=25.0,
    relative_strength_threshold=0.08,
    stop_loss_atr=3.0,
    take_profit_atr=1.5,
    trailing_atr=3.0,
    tp1_sell_ratio=0.10,
    tp2_atr=4.0,
    tp2_sell_ratio=0.10,
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

def _filtered_preloaded(preloaded: dict, dates: list[str]) -> dict:
    """trading_dates만 교체한 얕은 복사본 (ticker_data 등은 공유)."""
    out = dict(preloaded)
    out["trading_dates"] = dates
    return out


def _compute_precomp(dates: list[str], preloaded: dict) -> dict:
    return precompute_daily_signals(
        dates,
        preloaded["ticker_data"],
        preloaded["ticker_date_idx"],
        set(preloaded["initial_universe"]),
        params=BASE_PARAMS,
        kospi_ret_map=preloaded.get("kospi_ret_map"),
        kosdaq_ret_map=preloaded.get("kosdaq_ret_map"),
        ticker_market=preloaded.get("ticker_market"),
        weights=WEIGHTS,
    )


def _run_bt(preloaded: dict, precomp: dict) -> object:
    return run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=BASE_PARAMS,
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
# 보고서 출력
# ─────────────────────────────────────────────────────────────────────────────

def _print_report(
    summary,
    train_years: int,
    test_years: int,
    step_months: int,
    elapsed: float,
    log_fn,
) -> None:
    SEP  = "═" * 72
    SEP2 = "─" * 72

    log_fn(SEP)
    log_fn(
        f"  Walk-Forward 검증  (v2.7 / "
        f"{train_years}년 Train / {test_years}년 Test / {step_months}개월 Step)"
    )
    log_fn(SEP)
    log_fn("")

    # 윈도우별 결과 테이블
    header = (
        f"{'#':>2}  {'Train 기간':<20}  {'Test 기간':<20}  "
        f"{'Train PF':>8}  {'Test PF':>7}  {'유지율':>6}  {'견고'}"
    )
    log_fn("■ 윈도우별 결과")
    log_fn(header)
    log_fn(SEP2)

    for i, r in enumerate(summary.results, 1):
        w = r.window
        train_label = f"{w.train_start[:7]}~{w.train_end[:7]}"
        test_label  = f"{w.test_start[:7]}~{w.test_end[:7]}"
        robust_mark = "✅" if r.is_robust else "❌"
        retention   = f"{r.pf_retention * 100:.0f}%"
        log_fn(
            f"{i:>2}  {train_label:<20}  {test_label:<20}  "
            f"{_fmt_pf(r.train_pf):>8}  {_fmt_pf(r.test_pf):>7}  "
            f"{retention:>6}  {robust_mark}"
        )
        low_trades = " (거래 없음)" if r.test_trades == 0 else ""
        log_fn(
            f"      Train: {r.train_trades}건 CAGR {r.train_cagr * 100:+.1f}% MDD {r.train_mdd * 100:.1f}%  │  "
            f"Test: {r.test_trades}건 CAGR {r.test_cagr * 100:+.1f}% MDD {r.test_mdd * 100:.1f}%{low_trades}"
        )

    log_fn("")
    log_fn(SEP2)

    # 요약
    log_fn("■ 요약")
    log_fn(f"  윈도우 수      : {summary.total_windows}")
    log_fn(
        f"  견고 윈도우    : {summary.robust_windows}/{summary.total_windows} "
        f"({summary.robustness_rate * 100:.0f}%)"
    )
    inf_count = sum(1 for r in summary.results if r.test_pf == float("inf"))
    inf_note = f"  (inf {inf_count}건 제외)" if inf_count else ""
    log_fn(f"  평균 Test PF   : {_fmt_pf(summary.avg_test_pf)}{inf_note}")
    ret_str = (
        "inf" if summary.avg_pf_retention == float("inf")
        else f"{summary.avg_pf_retention * 100:.0f}%"
    )
    log_fn(f"  평균 PF 유지율 : {ret_str}{inf_note}")
    log_fn("")

    verdict_desc = {
        "PASS": "70%+ 견고 — 과최적화 없음",
        "WARN": "50~70% 견고 — 부분 과최적화 의심, 페이퍼에서 재관찰",
        "FAIL": "50% 미만 견고 — 과최적화 가능성 높음, 파라미터 재검토 필요",
    }
    log_fn(f"■ 판정: {summary.overall_verdict}  ({verdict_desc[summary.overall_verdict]})")
    log_fn("")
    log_fn(f"  소요 시간: {elapsed:.1f}s")
    log_fn(SEP)


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main(train_years: int = 2, test_years: int = 1, step_months: int = 12) -> None:
    out_path = ROOT / "experiments" / "results_walk_forward.txt"
    output_lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        output_lines.append(msg)

    t_total = time.time()

    # ── 1. 윈도우 생성 ───────────────────────────────────────────────────────
    windows = generate_windows(
        DATA_START, DATA_END,
        train_years=train_years,
        test_years=test_years,
        step_months=step_months,
    )
    print(f"\n[1/3] 윈도우 생성: {len(windows)}개")
    for i, w in enumerate(windows, 1):
        print(f"  {i:>2}. Train {w.train_start}~{w.train_end}  │  Test {w.test_start}~{w.test_end}")

    # ── 2. 데이터 로드 (1회) ─────────────────────────────────────────────────
    print(f"\n[2/3] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    all_dates: list[str] = preloaded["trading_dates"]
    print(f"  완료: {time.time() - t0:.1f}s  (거래일 {len(all_dates)}개)")

    # ── 3. 윈도우별 실행 ──────────────────────────────────────────────────────
    print(f"\n[3/3] Walk-Forward 실행...")
    results: list[WFResult] = []

    for i, w in enumerate(windows, 1):
        train_dates = [d for d in all_dates if w.train_start <= d <= w.train_end]
        test_dates  = [d for d in all_dates if w.test_start  <= d <= w.test_end]

        if not train_dates or not test_dates:
            print(f"  [{i}/{len(windows)}] 윈도우 {i}: 데이터 없음 — 건너뜀")
            continue

        t_win = time.time()

        # Train
        train_preloaded = _filtered_preloaded(preloaded, train_dates)
        train_precomp   = _compute_precomp(train_dates, train_preloaded)
        train_result    = _run_bt(train_preloaded, train_precomp)

        # Test (동일 파라미터, look-ahead 금지)
        test_preloaded = _filtered_preloaded(preloaded, test_dates)
        test_precomp   = _compute_precomp(test_dates, test_preloaded)
        test_result    = _run_bt(test_preloaded, test_precomp)

        wf = WFResult(
            window=w,
            train_pf=train_result.profit_factor,
            train_cagr=train_result.cagr_pct,
            train_mdd=abs(train_result.max_drawdown_pct),
            train_trades=train_result.total_trades,
            test_pf=test_result.profit_factor,
            test_cagr=test_result.cagr_pct,
            test_mdd=abs(test_result.max_drawdown_pct),
            test_trades=test_result.total_trades,
        )
        results.append(wf)

        elapsed_win = time.time() - t_win
        robust_mark = "✅" if wf.is_robust else "❌"
        print(
            f"  [{i}/{len(windows)}] "
            f"Train PF {_fmt_pf(wf.train_pf):>5} / "
            f"Test PF {_fmt_pf(wf.test_pf):>5} / "
            f"유지율 {wf.pf_retention * 100:.0f}% "
            f"{robust_mark}  ({elapsed_win:.1f}s)"
        )

    if not results:
        print("\n결과 없음 — 윈도우 생성 또는 데이터 범위를 확인하세요.")
        return

    # ── 4. 요약 + 보고서 ─────────────────────────────────────────────────────
    summary = build_summary(results)
    elapsed = time.time() - t_total

    print()
    _print_report(summary, train_years, test_years, step_months, elapsed, log)

    out_path.write_text("\n".join(output_lines), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-Forward 검증")
    parser.add_argument("--train-years", type=int, default=2, metavar="N",
                        help="훈련 기간 (년, 기본 2)")
    parser.add_argument("--test-years",  type=int, default=1, metavar="N",
                        help="테스트 기간 (년, 기본 1)")
    parser.add_argument("--step-months", type=int, default=12, metavar="N",
                        help="윈도우 이동 간격 (월, 기본 12)")
    args = parser.parse_args()
    main(
        train_years=args.train_years,
        test_years=args.test_years,
        step_months=args.step_months,
    )

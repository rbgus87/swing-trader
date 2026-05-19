"""점진적 트레일링 실험 — 수익 구간별 Trail 배수 축소 효과 검증.

가설: 수익이 쌓일수록 Trail을 좁혀 이익을 더 빨리 보호하면
      TRAILING 청산의 평균 손익이 높아지고 MDD가 개선된다.

현행 v2.7.1 기준선:
  SL=2.5 / TP1=1.0(10%) / TP2=6.0(10%) / Trail=4.0(고정)

6가지 변형:
  [0] FIXED_4.0       고정 Trail 4.0 (기준선)
  [1] PROG_GENTLE     0→4.0 / +1ATR→3.5 / +2ATR→3.0 / +3ATR→2.5
  [2] PROG_AGGRESSIVE 0→4.0 / +1ATR→3.0 / +2ATR→2.5 / +3ATR→2.0
  [3] PROG_LATE       0→4.0 / +2ATR→3.5 / +4ATR→3.0 / +6ATR→2.5
  [4] PROG_STEP       0→4.5 / +1.5ATR→3.0 / +3ATR→2.0
  [5] PROG_BREAKEVEN  미수익→4.0 / 본전 이상→3.0 / +2ATR→2.5

불변:
  - 진입 로직(v2.7.1) 완전 고정
  - SL=2.5 / TP1=1.0(10%) / TP2=6.0(10%) 고정
  - 비용 모델(수수료·세금·슬리피지) 고정
  - Trail 배수만 변형

구현 방식:
  `evaluate_exit` 를 mock.patch 로 교체하여 per-call dynamic trailing_atr_mult 주입.
  TRAILING 청산의 exit_price 는 바탕 params.trailing_atr(=4.0) 기반으로 계산됨.
  - 타이트한 변형(trail < 4.0): exit_price 소폭 저평가 (보수적 편향)
  - PROG_STEP 초기 구간(trail = 4.5): exit_price 소폭 고평가 (낙관적 편향)
  exploratory 실험이므로 방향성 판단에 충분.

실행:
    python experiments/experiment_progressive_trail.py
결과:
    experiments/results_progressive_trail.txt
"""
from __future__ import annotations

import dataclasses
import sys
import time
import unittest.mock
from pathlib import Path
from typing import Callable

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
from src.strategy.exit_evaluator import (
    ExitContext, ExitParams,
    evaluate_exit as _orig_evaluate_exit,
)
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.cost_model import CostModel
from src.utils.slippage_model import SlippageParams
from src.strategy.ranking import RankingWeights
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.scaling import ScalingParams


# ─────────────────────────────────────────────────────────────────────────────
# v2.7.1 설정
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
COST    = CostModel()
_sm     = _tf.get("slippage_model", {})
SLIP    = SlippageParams(
    enabled=bool(_sm.get("enabled", True)),
    base_slippage=float(_sm.get("base_slippage", 0.0003)),
    impact_coefficient=float(_sm.get("impact_coefficient", 0.1)),
    max_slippage=float(_sm.get("max_slippage", 0.02)),
)
SECTOR  = SectorConstraint(enabled=False)
DYNHOLD = DynamicHoldParams(enabled=False)
SCALING = ScalingParams(enabled=False)


# ─────────────────────────────────────────────────────────────────────────────
# 점진적 트레일링 함수
# ─────────────────────────────────────────────────────────────────────────────

def progressive_trail_atr(
    current_price: float,
    entry_price: float,
    atr: float,
    tiers: list[tuple[float, float]],
    default_trail: float = 4.0,
) -> float:
    """수익(ATR 단위) 구간에 따라 Trail 배수를 반환한다.

    Args:
        current_price:  보유 중 최고가 (highest_since_entry)
        entry_price:    진입 가격
        atr:            진입 시점 ATR
        tiers:          [(profit_in_atr_threshold, trail_mult), ...]  오름차순
        default_trail:  tiers 어느 것도 충족 못할 때 (수익 불충분 구간)

    Returns:
        해당 수익 구간에 적용할 trailing_atr_mult
    """
    if atr <= 0:
        return default_trail
    profit_in_atr = (current_price - entry_price) / atr
    trail = default_trail
    for threshold, trail_val in tiers:
        if profit_in_atr >= threshold:
            trail = trail_val
    return trail


# ─────────────────────────────────────────────────────────────────────────────
# 변형 정의
# ─────────────────────────────────────────────────────────────────────────────

VARIANTS: list[dict] = [
    {
        "label": "FIXED_4.0",
        "desc":  "고정 Trail 4.0 (기준선, v2.7.1)",
        "tiers": None,           # None = 패치 없이 원본 evaluate_exit 사용
        "default_trail": 4.0,
    },
    {
        "label": "PROG_GENTLE",
        "desc":  "0→4.0 / +1ATR→3.5 / +2ATR→3.0 / +3ATR→2.5",
        "tiers": [(1.0, 3.5), (2.0, 3.0), (3.0, 2.5)],
        "default_trail": 4.0,
    },
    {
        "label": "PROG_AGGRESSIVE",
        "desc":  "0→4.0 / +1ATR→3.0 / +2ATR→2.5 / +3ATR→2.0",
        "tiers": [(1.0, 3.0), (2.0, 2.5), (3.0, 2.0)],
        "default_trail": 4.0,
    },
    {
        "label": "PROG_LATE",
        "desc":  "0→4.0 / +2ATR→3.5 / +4ATR→3.0 / +6ATR→2.5",
        "tiers": [(2.0, 3.5), (4.0, 3.0), (6.0, 2.5)],
        "default_trail": 4.0,
    },
    {
        "label": "PROG_STEP",
        "desc":  "0→4.5 / +1.5ATR→3.0 / +3ATR→2.0",
        "tiers": [(1.5, 3.0), (3.0, 2.0)],
        "default_trail": 4.5,
    },
    {
        "label": "PROG_BREAKEVEN",
        "desc":  "미수익→4.0 / 본전 이상→3.0 / +2ATR→2.5",
        "tiers": [(0.0, 3.0), (2.0, 2.5)],
        "default_trail": 4.0,
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _make_patched_evaluate_exit(
    tiers: list[tuple[float, float]],
    default_trail: float,
) -> Callable[[ExitContext, ExitParams], object]:
    """동적 trailing_atr_mult 를 주입하는 evaluate_exit 래퍼를 반환한다."""

    def _patched(ctx: ExitContext, params: ExitParams):
        dynamic_mult = progressive_trail_atr(
            ctx.high_since_entry, ctx.entry_price, ctx.atr_at_entry,
            tiers, default_trail,
        )
        patched_params = dataclasses.replace(params, trailing_atr_mult=dynamic_mult)
        return _orig_evaluate_exit(ctx, patched_params)

    return _patched


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
    """청산 사유별 {건수, 평균 PnL, 합계 PnL}."""
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


def _fmt_short(r) -> str:
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
    out_path = ROOT / "experiments" / "results_progressive_trail.txt"
    SEP  = "=" * 72
    SEP2 = "-" * 72

    lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    t_total = time.time()

    # ── 1. 데이터 로드 ─────────────────────────────────────────────────────
    print("[1/3] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    n_tickers = len(preloaded["ticker_data"])
    print(f"  완료: {time.time() - t0:.1f}s  ({n_tickers} 종목)")

    # ── 2. 신호 사전 계산 (진입 신호 — 1회만, 변형 간 공유) ──────────────
    print("[2/3] v2.7.1 진입 신호 사전 계산...")
    t0 = time.time()
    precomp = precompute_daily_signals(
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
    total_cands = sum(len(v) for v in precomp["candidates"].values())
    print(f"  완료: {time.time() - t0:.1f}s  (총 후보 {total_cands:,}건)")

    # ── 3. 변형 실행 ──────────────────────────────────────────────────────
    print(f"[3/3] {len(VARIANTS)}개 변형 실행...")

    results: list[tuple[dict, object, dict]] = []
    for i, variant in enumerate(VARIANTS):
        label         = variant["label"]
        tiers         = variant["tiers"]
        default_trail = variant["default_trail"]

        print(f"  [{i}/{len(VARIANTS)-1}] {label}...", end="", flush=True)
        t0 = time.time()

        if tiers is None:
            # FIXED_4.0 기준선: 패치 없이 원본 실행
            r = _run_bt(preloaded, precomp, BASE_PARAMS)
        else:
            _eval_fn = _make_patched_evaluate_exit(tiers, default_trail)
            with unittest.mock.patch(
                "src.backtest.portfolio_backtester.evaluate_exit",
                new=_eval_fn,
            ):
                r = _run_bt(preloaded, precomp, BASE_PARAMS)

        elapsed = time.time() - t0
        print(f" ({elapsed:.1f}s)")
        stats = _exit_stats(r.trades)
        results.append((variant, r, stats))

    # ─────────────────────────────────────────────────────────────────────
    # 보고서
    # ─────────────────────────────────────────────────────────────────────
    baseline_r     = results[0][1]
    baseline_stats = results[0][2]

    log(SEP)
    log("점진적 트레일링 실험 (Progressive Trailing Stop)")
    log(f"기준선 v2.7.1: SL=2.5 / TP1=1.0(10%) / TP2=6.0(10%) / Trail=4.0(고정)")
    log(SEP)
    log()

    # ── 요약 테이블 ────────────────────────────────────────────────────────
    log("■ 결과 요약")
    log()
    hdr = (
        f"  {'변형':<22}  {'건수':>5}  {'WR':>6}  {'PF':>5}  {'CAGR':>7}  "
        f"{'MDD':>7}  {'순손익':>14}  {'Trail청산:건수/평균PnL':>24}"
    )
    log(hdr)
    log(f"  {'-'*22}  {'-----':>5}  {'------':>6}  {'-----':>5}  {'-------':>7}  "
        f"{'-------':>7}  {'------':>14}  {'-'*24}")

    for variant, r, stats in results:
        label      = variant["label"]
        t_stat     = stats.get("TRAILING", {})
        t_cnt      = t_stat.get("count", 0)
        t_avg      = t_stat.get("avg_pnl", 0.0)
        t_info     = f"{t_cnt}건 {t_avg:+,.0f}원" if t_cnt > 0 else "0건 —"

        pf_diff    = r.profit_factor - baseline_r.profit_factor
        pf_marker  = " *" if abs(pf_diff) >= 0.03 else ""

        log(
            f"  [{label:<20}]"
            f"  {r.total_trades:5d}"
            f"  {r.win_rate*100:5.1f}%"
            f"  {r.profit_factor:5.2f}{pf_marker:<2}"
            f"  {r.cagr_pct*100:+6.1f}%"
            f"  -{r.max_drawdown_pct*100:5.1f}%"
            f"  {int(r.final_capital - r.initial_capital):>14,}원"
            f"  {t_info:>24}"
        )
    log()
    log("  (* PF 차이 ±0.03 이상)")
    log()

    # ── 청산 사유별 상세 ──────────────────────────────────────────────────
    log(SEP)
    log("■ 청산 사유별 상세 (vs FIXED_4.0 기준선)")
    log(SEP)
    log()

    REASON_LABELS = [
        ("STOP_LOSS",     "SL 손절   "),
        ("TRAILING",      "Trail 청산"),
        ("TAKE_PROFIT_1", "TP1 익절  "),
        ("TAKE_PROFIT_2", "TP2 익절  "),
        ("TREND_EXIT",    "추세이탈  "),
        ("TIME_EXIT",     "보유기간  "),
    ]

    for variant, r, stats in results:
        label = variant["label"]
        log(f"  [{label}]  {variant['desc']}")
        for key, lbl in REASON_LABELS:
            s  = stats.get(key, {"count": 0, "avg_pnl": 0.0, "total_pnl": 0.0})
            bs = baseline_stats.get(key, {"count": 0, "avg_pnl": 0.0, "total_pnl": 0.0})
            cnt     = s["count"]
            avg_pnl = s.get("avg_pnl", 0.0)
            tot_pnl = s.get("total_pnl", 0.0)

            if label == "FIXED_4.0":
                log(
                    f"    {lbl}: {cnt:4d}건  avg {avg_pnl:+9,.0f}원  "
                    f"합계 {int(tot_pnl):+14,}원"
                )
            else:
                d_cnt  = cnt - bs["count"]
                d_avg  = avg_pnl - bs.get("avg_pnl", 0.0)
                d_tot  = tot_pnl - bs.get("total_pnl", 0.0)
                s_cnt  = "+" if d_cnt >= 0 else ""
                s_avg  = "+" if d_avg >= 0 else ""
                s_tot  = "+" if d_tot >= 0 else ""
                log(
                    f"    {lbl}: {cnt:4d}건({s_cnt}{d_cnt:+d})  "
                    f"avg {avg_pnl:+9,.0f}원({s_avg}{int(d_avg):+,}원)  "
                    f"합계 {int(tot_pnl):+14,}원({s_tot}{int(d_tot):+,}원)"
                )
        log()

    # ── 결론 ──────────────────────────────────────────────────────────────
    log(SEP)
    log("■ 결론")
    log(SEP)
    log()

    best_i = max(range(len(results)), key=lambda i: results[i][1].profit_factor)
    best_v, best_r, _ = results[best_i]
    bpf = baseline_r.profit_factor
    diff = best_r.profit_factor - bpf

    log(f"  기준선 (FIXED_4.0): PF {bpf:.2f} / "
        f"CAGR {baseline_r.cagr_pct*100:+.1f}% / "
        f"MDD -{baseline_r.max_drawdown_pct*100:.1f}%")
    log(f"  최고 변형  [{best_v['label']}]: PF {best_r.profit_factor:.2f} / "
        f"CAGR {best_r.cagr_pct*100:+.1f}% / "
        f"MDD -{best_r.max_drawdown_pct*100:.1f}%")
    log()

    if diff >= 0.03:
        log(f"  점진적 트레일링 유효: PF {diff:+.2f}")
        log(f"  [{best_v['label']}] 적용 권장: {best_v['desc']}")
        log(f"  Walk-Forward 검증 후 config.yaml 적용 고려.")
    elif diff >= 0.01:
        log(f"  미미한 개선 (PF {diff:+.2f}): 통계적 유의성 부족.")
        log(f"  현행 고정 Trail 4.0 유지 권장.")
    else:
        log(f"  개선 없음 (PF {diff:+.2f}): 점진적 트레일링은 현 전략에서 알파 없음.")
        log(f"  결론: 고정 Trail 4.0(FIXED_4.0) 유지.")
    log()
    log(f"  주의: TRAILING 청산 exit_price 는 params.trailing_atr(=4.0) 기반 계산.")
    log(f"        타이트 변형은 exit_price 소폭 저평가(보수적), PROG_STEP 초기 고평가(낙관적).")
    log()
    log(f"  소요 시간: {time.time() - t_total:.1f}s")
    log(SEP)
    log(f"\n결과 저장: {out_path}")

    out_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

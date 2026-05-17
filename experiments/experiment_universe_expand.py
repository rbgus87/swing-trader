"""유니버스 확대 백테스트 — 시총 하한 6개 변형 비교.

[0] MCAP_3T         : 3조 + TV 50억 (현재 기준선)
[1] MCAP_2T         : 2조 + TV 50억
[2] MCAP_1T         : 1조 + TV 50억
[3] MCAP_5000B      : 5000억 + TV 50억
[4] MCAP_2T_TV30    : 2조 + TV 30억
[5] MCAP_1T_TV30    : 1조 + TV 30억

v2.7 파라미터 동일. 변형별로 load_backtest_data + precompute 재생성.

Usage:
    python experiments/experiment_universe_expand.py
"""
from __future__ import annotations

import math
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
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.ranking import RankingWeights
from src.strategy.scaling import ScalingParams
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.cost_model import CostModel
from src.utils.slippage_model import SlippageParams, compute_slippage

# ─────────────────────────────────────────────────────────────────────────────
# 고정 설정 — v2.7 확정 파라미터
# ─────────────────────────────────────────────────────────────────────────────

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
# 시나리오 정의
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS: list[tuple[str, int, int]] = [
    ("MCAP_3T",        3_000_000_000_000, 5_000_000_000),
    ("MCAP_2T",        2_000_000_000_000, 5_000_000_000),
    ("MCAP_1T",        1_000_000_000_000, 5_000_000_000),
    ("MCAP_5000B",       500_000_000_000, 5_000_000_000),
    ("MCAP_2T_TV30",   2_000_000_000_000, 3_000_000_000),
    ("MCAP_1T_TV30",   1_000_000_000_000, 3_000_000_000),
]


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

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


def _avg_universe_size(precomp: dict) -> float:
    sizes = [len(v) for v in precomp["universe_at"].values()]
    return sum(sizes) / len(sizes) if sizes else 0.0


def _avg_candidates_per_day(precomp: dict) -> float:
    counts = [len(v) for v in precomp["candidates"].values()]
    return sum(counts) / len(counts) if counts else 0.0


def _est_slippage(precomp: dict, order_value: float) -> float:
    """후보군 평균 거래대금 기준 예상 슬리피지."""
    tvs = []
    for cands in precomp["candidates"].values():
        for c in cands:
            tv = c.get("avg_trading_value_20", 0)
            if tv > 0:
                tvs.append(tv)
    if not tvs:
        return 0.0
    avg_tv = sum(tvs) / len(tvs)
    return compute_slippage(order_value, avg_tv, SLIP)


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# 보고서
# ─────────────────────────────────────────────────────────────────────────────

def print_report(
    results: list[dict],
    baseline_idx: int,
    elapsed: float,
    log_fn,
) -> None:
    SEP  = "═" * 80
    SEP2 = "─" * 80

    log_fn(SEP)
    log_fn("  유니버스 확대 백테스트 (v2.7, 5종목, 2014~2026)")
    log_fn(SEP)
    log_fn("")

    # ── 유니버스 크기 ──────────────────────────────────────────────────────────
    log_fn("■ 유니버스 크기 및 후보군")
    base_univ = results[baseline_idx]["avg_univ"]
    log_fn(f"  {'변형':<22} {'Univ 평균':>10} {'vs 기준':>8} {'후보/일':>8} {'슬리피지':>10}")
    log_fn(SEP2)
    for r in results:
        diff_pct = (r["avg_univ"] / base_univ - 1) * 100 if base_univ > 0 else 0.0
        diff_str = f"+{diff_pct:.0f}%" if diff_pct >= 0 else f"{diff_pct:.0f}%"
        log_fn(
            f"  [{r['tag']:<20}] {r['avg_univ']:>10.0f} {diff_str:>8}"
            f" {r['avg_cands']:>8.2f} {r['est_slip'] * 100:>8.3f}%"
        )
    log_fn("")

    # ── 성과 비교 ─────────────────────────────────────────────────────────────
    log_fn("■ 성과 비교")
    log_fn(
        f"  {'변형':<22} {'건':>5} {'WR':>6} {'PF':>6} {'CAGR':>7}"
        f" {'MDD':>7} {'활용':>6} {'C/M':>5}"
    )
    log_fn(SEP2)
    for r in results:
        cm = r["cagr"] / r["mdd"] if r["mdd"] > 0 else 0.0
        log_fn(
            f"  [{r['tag']:<20}]"
            f" {r['trades']:>5}"
            f" {r['wr'] * 100:>5.1f}%"
            f" {_fmt_pf(r['pf']):>6}"
            f" {r['cagr'] * 100:>+6.1f}%"
            f" {r['mdd'] * 100:>+6.1f}%"
            f" {r['util']:>5.0f}%"
            f" {cm:>5.2f}"
        )
    log_fn("")

    # ── [MCAP_3T] 대비 변화 ───────────────────────────────────────────────────
    base = results[baseline_idx]
    log_fn(f"■ [{base['tag']}] 대비 변화")
    log_fn(
        f"  {'변형':<22} {'Δ건':>5} {'ΔWR':>7} {'ΔPF':>6} {'ΔCAGR':>7}"
        f" {'ΔMDD':>7} {'Δ활용':>7}"
    )
    log_fn(SEP2)
    for r in results:
        if r["tag"] == base["tag"]:
            continue
        log_fn(
            f"  [{r['tag']:<20}]"
            f" {r['trades'] - base['trades']:>+5}"
            f" {(r['wr'] - base['wr']) * 100:>+6.1f}%p"
            f" {r['pf'] - base['pf']:>+6.2f}"
            f" {(r['cagr'] - base['cagr']) * 100:>+6.1f}%"
            f" {(r['mdd'] - base['mdd']) * 100:>+6.1f}%p"
            f" {r['util'] - base['util']:>+6.0f}%p"
        )
    log_fn("")

    # ── Q&A ──────────────────────────────────────────────────────────────────
    log_fn("■ 핵심 질문 답변")

    # Q1: 유니버스 확대 → 건수/자본활용 개선?
    best_util = max(results, key=lambda x: x["util"])
    log_fn(
        f"  Q1 자본활용 최고: [{best_util['tag']}] {best_util['util']:.0f}%"
        f"  (기준선 {base['util']:.0f}%,"
        f" +{best_util['util'] - base['util']:.0f}%p)"
    )
    trade_gain = [(r["tag"], r["trades"] - base["trades"]) for r in results if r["tag"] != base["tag"]]
    log_fn(
        f"     건수 증가: " +
        ", ".join(f"{t} +{d}건" for t, d in trade_gain)
    )

    # Q2: 중소형주 진입 → PF/WR 하락?
    pf_drop = [(r["tag"], r["pf"] - base["pf"]) for r in results if r["tag"] != base["tag"]]
    worst_pf = min(pf_drop, key=lambda x: x[1])
    log_fn(
        f"  Q2 PF 변화: "
        + ", ".join(f"{t} {d:+.2f}" for t, d in pf_drop)
    )
    log_fn(
        f"     PF 최대 하락: [{worst_pf[0]}] {worst_pf[1]:+.2f}"
    )

    # Q3: 최적 시총 하한 (CAGR/MDD 기준)
    best_cm = max(results, key=lambda x: x["cagr"] / x["mdd"] if x["mdd"] > 0 else 0)
    log_fn(
        f"  Q3 CAGR/MDD 최고: [{best_cm['tag']}]"
        f" C/M {best_cm['cagr'] / best_cm['mdd']:.2f}"
        f" (CAGR {best_cm['cagr'] * 100:+.1f}%, MDD {best_cm['mdd'] * 100:.1f}%)"
    )

    # Q4: TV 30억 완화 유동성 리스크
    tv30_pairs = [
        ("MCAP_2T", "MCAP_2T_TV30"),
        ("MCAP_1T", "MCAP_1T_TV30"),
    ]
    log_fn("  Q4 거래대금 50억→30억 완화:")
    for base_tag, tv30_tag in tv30_pairs:
        rb = next((r for r in results if r["tag"] == base_tag), None)
        rt = next((r for r in results if r["tag"] == tv30_tag), None)
        if rb and rt:
            slip_diff = (rt["est_slip"] - rb["est_slip"]) * 100
            log_fn(
                f"     {base_tag} → {tv30_tag}: "
                f"건수 +{rt['trades'] - rb['trades']}건, "
                f"PF {rb['pf']:+.2f}→{rt['pf']:+.2f} ({rt['pf'] - rb['pf']:+.2f}), "
                f"슬리피지 {slip_diff:+.3f}%p"
            )
    log_fn("")

    # ── 권장 설정 ─────────────────────────────────────────────────────────────
    log_fn("■ 권장 설정")
    rec = best_cm
    tv_note = "50억 (유동성 우선)" if "TV30" not in rec["tag"] else "30억 (거래 확대)"
    log_fn(f"  CAGR/MDD 최우선: [{rec['tag']}]")
    log_fn(f"  → min_market_cap: {rec['mcap'] // 1_000_000_000_000}조  근거: CAGR/MDD {rec['cagr'] / rec['mdd']:.2f} 최고")
    log_fn(f"  → min_trading_value: {tv_note}")
    log_fn("")

    log_fn(f"  소요 시간: {elapsed:.1f}s")
    log_fn(SEP)


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    out_path = ROOT / "experiments" / "results_universe_expand.txt"
    output_lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        output_lines.append(msg)

    t_total = time.time()
    results: list[dict] = []
    order_value = CAPITAL / MAX_POS  # 전형적 주문 금액

    for i, (tag, mcap, tv) in enumerate(SCENARIOS):
        mcap_str = f"{mcap // 1_000_000_000_000}조" if mcap >= 1_000_000_000_000 else f"{mcap // 100_000_000_000}00억"
        tv_str = f"{tv // 1_000_000_000}0억"
        print(f"\n[{i + 1}/{len(SCENARIOS)}] {tag}  (시총≥{mcap_str}, TV≥{tv_str})")

        # ── 데이터 로드 ─────────────────────────────────────────────────────
        t0 = time.time()
        print(f"  데이터 로드...", end="", flush=True)
        preloaded = load_backtest_data(BASE_PARAMS, mcap_threshold=mcap, trading_value_threshold=tv)
        print(f"  {time.time() - t0:.1f}s")

        # ── Precompute ───────────────────────────────────────────────────────
        t0 = time.time()
        print(f"  Precompute...", end="", flush=True)
        precomp = precompute_daily_signals(
            preloaded["trading_dates"],
            preloaded["ticker_data"],
            preloaded["ticker_date_idx"],
            set(preloaded["initial_universe"]),
            params=BASE_PARAMS,
            kospi_ret_map=preloaded.get("kospi_ret_map"),
            kosdaq_ret_map=preloaded.get("kosdaq_ret_map"),
            ticker_market=preloaded.get("ticker_market"),
            weights=WEIGHTS,
            mcap_threshold=mcap,
            trading_value_threshold=tv,
        )
        print(f"  {time.time() - t0:.1f}s")

        # ── 백테스트 ────────────────────────────────────────────────────────
        t0 = time.time()
        print(f"  백테스트...", end="", flush=True)
        r = _run_bt(preloaded, precomp)
        elapsed_bt = time.time() - t0

        avg_univ  = _avg_universe_size(precomp)
        avg_cands = _avg_candidates_per_day(precomp)
        est_slip  = _est_slippage(precomp, order_value)
        util      = r.avg_positions / MAX_POS * 100

        print(
            f"  {elapsed_bt:.1f}s  "
            f"Univ={avg_univ:.0f}  후보={avg_cands:.2f}/일  "
            f"PF={_fmt_pf(r.profit_factor)}  CAGR={r.cagr_pct * 100:+.1f}%  "
            f"WR={r.win_rate * 100:.1f}%  건={r.total_trades}  활용={util:.0f}%"
        )

        results.append({
            "tag":       tag,
            "mcap":      mcap,
            "tv":        tv,
            "avg_univ":  avg_univ,
            "avg_cands": avg_cands,
            "est_slip":  est_slip,
            "trades":    r.total_trades,
            "wr":        r.win_rate,
            "pf":        r.profit_factor,
            "cagr":      r.cagr_pct,
            "mdd":       abs(r.max_drawdown_pct),
            "util":      util,
        })

    elapsed = time.time() - t_total
    baseline_idx = next(i for i, r in enumerate(results) if r["tag"] == "MCAP_3T")

    print()
    print_report(results, baseline_idx, elapsed, log)

    out_path.write_text("\n".join(output_lines), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()

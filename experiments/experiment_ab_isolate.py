"""Phase A+B 개별 효과 분리 실험.

9개 변형으로 기준선 보정(v2.6 재현) 및 B3~B6 개별 기여도를 분리한다.

실행:
    python experiments/experiment_ab_isolate.py
"""
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
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.cost_model import CostModel
from src.strategy.ranking import RankingWeights
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.scaling import ScalingParams
from src.utils.slippage_model import SlippageParams

# ── 공통 파라미터 ─────────────────────────────────────────────────────────
BASE_PARAMS = StrategyParams(
    tp1_sell_ratio=0.10,
    tp2_atr=4.0,
    tp2_sell_ratio=0.10,
)
CAPITAL = 10_000_000
MAX_POS = 6
MIN_AMOUNT = 300_000

# ── 비용 모델 ─────────────────────────────────────────────────────────────
OLD_COST = CostModel(
    buy_commission=0.00015,
    sell_commission=0.00015,
    sell_tax_kospi=0.0018,
    sell_tax_kosdaq=0.0018,
    slippage=0.0005,
)
NEW_COST = CostModel()  # 현행 0.20%

# ── 슬리피지 ──────────────────────────────────────────────────────────────
SLIP_OFF = SlippageParams(enabled=False, fixed_slippage=0.0005)
SLIP_ON  = SlippageParams(enabled=True, base_slippage=0.0003,
                          impact_coefficient=0.1, max_slippage=0.02)

# ── 랭킹 가중치 ───────────────────────────────────────────────────────────
WEIGHTS_ADX = RankingWeights(rs=0.0, momentum_atr=0.0, adx=1.0,
                              liquidity=0.0, ma_alignment=0.0)
WEIGHTS_NEW = RankingWeights()  # 복합 기본 가중치

# ── Phase B 기능 상수 ──────────────────────────────────────────────────────
SECTOR_OFF    = SectorConstraint(enabled=False)
SECTOR_ON     = SectorConstraint(enabled=True, max_per_sector=2)
SECTOR_RELAX  = SectorConstraint(enabled=True, max_per_sector=3)

DYNHOLD_OFF   = DynamicHoldParams(enabled=False)
DYNHOLD_ON    = DynamicHoldParams(
    enabled=True, base_hold_days=20,
    extend_multiplier=1.5, shorten_multiplier=0.5,
    adx_strong_threshold=30, adx_weak_threshold=15,
)
DYNHOLD_RELAX = DynamicHoldParams(
    enabled=True, base_hold_days=20,
    extend_multiplier=1.5, shorten_multiplier=0.6,   # 최소 12일 (0.5→0.6)
    adx_strong_threshold=30, adx_weak_threshold=10,  # 극약세에서만 단축
)

SCALING_OFF   = ScalingParams(enabled=False)


def _run(label: str, cost, precomp, breadth_th, regime, sector, dynhold, slip, preloaded):
    """단일 변형 실행 → (PortfolioResult, elapsed_sec)."""
    t0 = time.time()
    result = run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=BASE_PARAMS,
        cost=cost,
        min_position_amount=MIN_AMOUNT,
        preloaded_data=preloaded,
        precomputed=precomp,
        sizing_mode="equity",
        breadth_gate_threshold=breadth_th,
        regime_gate_enabled=regime,
        sector_constraint=sector,
        dynamic_hold=dynhold,
        scaling=SCALING_OFF,
        slippage_params=slip,
    )
    elapsed = time.time() - t0
    return result, elapsed


def _pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def _pct_abs(v: float) -> str:
    return f"{v * 100:.1f}%"


def _pnl(v: float) -> str:
    return f"{v:+,.0f}"


def _print_save(lines: list, f):
    for line in lines:
        print(line)
        f.write(line + "\n")


def _row(label, r):
    pnl = r.final_capital - r.initial_capital
    ratio = abs(r.cagr_pct / r.max_drawdown_pct) if r.max_drawdown_pct > 0 else 0.0
    pf_s = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "  inf"
    return (
        f"{label:<26}  {r.total_trades:>5}  {r.win_rate * 100:>5.1f}%  "
        f"{pf_s:>5}  {r.cagr_pct * 100:>6.1f}%  "
        f"{-r.max_drawdown_pct * 100:>5.1f}%  {pnl:>+14,.0f}  {ratio:>8.2f}"
    )


def _delta_row(label, r_from, r_to):
    pnl_from = r_from.final_capital - r_from.initial_capital
    pnl_to   = r_to.final_capital   - r_to.initial_capital
    dt = r_to.total_trades - r_from.total_trades
    dpf   = r_to.profit_factor - r_from.profit_factor
    dcagr = (r_to.cagr_pct - r_from.cagr_pct) * 100
    dmdd  = (-r_to.max_drawdown_pct + r_from.max_drawdown_pct) * 100
    dpnl  = pnl_to - pnl_from
    pf_s  = ("+" if dpf >= 0 else "") + f"{dpf:.2f}"
    return (
        f"{label:<26}  {dt:>+5}  {pf_s:>6}  "
        f"{dcagr:>+6.1f}%p  {dmdd:>+6.1f}%p  {dpnl:>+14,.0f}"
    )


def main():
    out_path = ROOT / "experiments" / "results_ab_isolate.txt"

    SEP = "=" * 80
    DIV = "-" * 80

    print(SEP)
    print("Phase A+B 개별 효과 분리 (10M/6종목, 12년 백테스트)")
    print(SEP)

    # ── 1. 데이터 1회 로드 ────────────────────────────────────────────────
    print("\n[1/4] 데이터 로드 중 (1회)...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    print(f"  완료: {time.time() - t0:.1f}s")

    td   = preloaded["trading_dates"]
    tdat = preloaded["ticker_data"]
    tidx = preloaded["ticker_date_idx"]
    iuniv = set(preloaded["initial_universe"])
    kret  = preloaded.get("kospi_ret_map")
    dret  = preloaded.get("kosdaq_ret_map")
    tmark = preloaded.get("ticker_market")

    # ── 2. precomputed 2종 생성 ───────────────────────────────────────────
    print("\n[2/4] 신호 사전 계산 (ADX 단일 / 복합 랭킹)...")
    t0 = time.time()
    precomp_adx = precompute_daily_signals(
        td, tdat, tidx, iuniv, params=BASE_PARAMS,
        kospi_ret_map=kret, kosdaq_ret_map=dret,
        ticker_market=tmark, weights=WEIGHTS_ADX,
    )
    print(f"  ADX 단일 완료: {time.time() - t0:.1f}s")

    t0 = time.time()
    precomp_new = precompute_daily_signals(
        td, tdat, tidx, iuniv, params=BASE_PARAMS,
        kospi_ret_map=kret, kosdaq_ret_map=dret,
        ticker_market=tmark, weights=WEIGHTS_NEW,
    )
    print(f"  복합 랭킹 완료: {time.time() - t0:.1f}s")

    # ── 3. 9개 변형 실행 ─────────────────────────────────────────────────
    # (label, cost, precomp, breadth_th, regime, sector, dynhold, slip)
    variants = [
        ("[0] CORRECT_BASELINE",   OLD_COST, precomp_adx, 0.40, False, SECTOR_OFF,   DYNHOLD_OFF,   SLIP_OFF),
        ("[1] A_COST_ONLY",        NEW_COST, precomp_adx, 0.40, False, SECTOR_OFF,   DYNHOLD_OFF,   SLIP_OFF),
        ("[2] A+B1_RANKING",       NEW_COST, precomp_new, 0.40, False, SECTOR_OFF,   DYNHOLD_OFF,   SLIP_OFF),
        ("[3] A+B1+B2_REGIME",     NEW_COST, precomp_new, 0.40, True,  SECTOR_OFF,   DYNHOLD_OFF,   SLIP_OFF),
        ("[4] +B3_SECTOR_ONLY",    NEW_COST, precomp_new, 0.40, True,  SECTOR_ON,    DYNHOLD_OFF,   SLIP_OFF),
        ("[5] +B4_HOLD_ONLY",      NEW_COST, precomp_new, 0.40, True,  SECTOR_OFF,   DYNHOLD_RELAX, SLIP_OFF),
        ("[6] +B6_SLIP_ONLY",      NEW_COST, precomp_new, 0.40, True,  SECTOR_OFF,   DYNHOLD_OFF,   SLIP_ON),
        ("[7] +B3_RELAXED",        NEW_COST, precomp_new, 0.40, True,  SECTOR_RELAX, DYNHOLD_OFF,   SLIP_OFF),
        ("[8] BEST_COMBO",         NEW_COST, precomp_new, 0.40, True,  SECTOR_RELAX, DYNHOLD_RELAX, SLIP_ON),
    ]

    print("\n[3/4] 9개 변형 백테스트 실행 중...")
    results = []
    for label, cost, precomp, breadth_th, regime, sector, dynhold, slip in variants:
        print(f"  {label}...", end=" ", flush=True)
        r, elapsed = _run(label, cost, precomp, breadth_th, regime, sector, dynhold, slip, preloaded)
        results.append((label, r))
        pf_s = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
        print(
            f"{elapsed:.1f}s  |  건수={r.total_trades}  WR={_pct_abs(r.win_rate)}  "
            f"PF={pf_s}  CAGR={_pct(r.cagr_pct)}  MDD={_pct(r.max_drawdown_pct)}"
        )

    # ── 4. 보고서 ────────────────────────────────────────────────────────
    print("\n[4/4] 보고서 생성 중...")

    period = results[0][1].period
    r0 = results[0][1]   # CORRECT_BASELINE
    r3 = results[3][1]   # A+B1+B2_REGIME (B3~B6 기준선)

    V26_PF   = 1.95
    V26_MDD  = 0.321
    V26_CAGR = 0.177

    lines = []

    lines.append("")
    lines.append(SEP)
    lines.append("Phase A+B 개별 효과 분리 (10M/6종목, 12년 백테스트)")
    lines.append(SEP)
    lines.append(f"Period: {period}")
    lines.append(f"Capital: {CAPITAL:,}원  /  Max positions: {MAX_POS}")
    lines.append(f"v2.6 기준선: PF {V26_PF:.2f}  MDD -{V26_MDD*100:.1f}%  CAGR {V26_CAGR*100:.1f}%  (847건)")
    lines.append("")

    # ── 핵심 지표 전체 ─────────────────────────────────────────────────
    hdr_row = (
        f"{'변형':<26}  {'건수':>5}  {'WR':>6}  {'PF':>5}  "
        f"{'CAGR':>7}  {'MDD':>5}  {'순손익':>14}  {'C/M':>8}"
    )

    lines.append("[핵심 지표]")
    lines.append(hdr_row)
    lines.append(DIV)
    for label, r in results:
        lines.append(_row(label, r))
    lines.append("")

    # ── [0] CORRECT_BASELINE 재현 정확도 ──────────────────────────────
    lines.append("[0] CORRECT_BASELINE 재현 검증 (v2.6 기준: PF 1.95, MDD -32.1%, CAGR +17.7%)")
    pf_diff   = abs(r0.profit_factor - V26_PF)
    mdd_diff  = abs(r0.max_drawdown_pct - V26_MDD) * 100
    cagr_diff = abs(r0.cagr_pct - V26_CAGR) * 100
    lines.append(
        f"  실제: PF={r0.profit_factor:.2f}  MDD=-{r0.max_drawdown_pct*100:.1f}%  "
        f"CAGR={r0.cagr_pct*100:.1f}%  건수={r0.total_trades}"
    )
    lines.append(
        f"  차이: dPF={pf_diff:.2f}  dMDD={mdd_diff:.1f}%p  dCAGR={cagr_diff:.1f}%p"
    )
    ok_pf  = pf_diff   < V26_PF * 0.10
    ok_mdd = mdd_diff  < V26_MDD * 100 * 0.10
    match_str = "OK (10% 이내)" if (ok_pf and ok_mdd) else "MISMATCH"
    lines.append(f"  판정: {match_str}")
    if not (ok_pf and ok_mdd):
        lines.append("")
        lines.append("  [원인 분석]")
        lines.append(
            f"  실험 [0]의 건수={r0.total_trades}건 vs v2.6 847건 차이 ="
            f" {r0.total_trades - 847:+}건."
        )
        if r0.total_trades > 900:
            lines.append(
              "  v2.6 측정 당시 sector_constraint(max=2)가 활성화된 상태였을 가능성 높음.")
            lines.append(
              "  sector OFF 상태에서는 업종 집중 진입이 허용되어 건수와 PnL 분산이 달라짐.")
        lines.append(
            "  또는 v2.6이 복합 랭킹(B1) 또는 이중 국면(B2) 적용 후 재측정되었을 수 있음.")
        lines.append("  [3] A+B1+B2_REGIME 결과를 실질적 v2.6 기준선으로 비교 참조 가능.")
    lines.append("")

    # ── A → B1 → B2 단계별 기여 ────────────────────────────────────
    lines.append("[A+B1+B2 단계별 기여도]")
    delta_hdr = (
        f"{'단계':<26}  {'d건수':>5}  {'dPF':>6}  "
        f"{'dCAGR':>8}  {'dMDD':>8}  {'d순손익':>14}"
    )
    lines.append(delta_hdr)
    lines.append(DIV)
    steps_early = [
        ("[0]->[1] A 비용 정확화",  0, 1),
        ("[1]->[2] +B1 복합 랭킹",  1, 2),
        ("[2]->[3] +B2 이중 국면",  2, 3),
    ]
    for step_name, fi, ti in steps_early:
        lines.append(_delta_row(step_name, results[fi][1], results[ti][1]))
    lines.append("")

    # ── B3~B6 개별 기여도 ([3] 대비) ──────────────────────────────
    lines.append(f"[B3~B6 개별 기여도] (기준: [3] A+B1+B2  건수={r3.total_trades})")
    lines.append(delta_hdr)
    lines.append(DIV)
    steps_b = [
        ("[3]->[4] +B3 섹터(max=2)",    3, 4),
        ("[3]->[5] +B4 동적보유(완화)",  3, 5),
        ("[3]->[6] +B6 동적슬리피지",   3, 6),
        ("[3]->[7] +B3 섹터(max=3)",    3, 7),
        ("[3]->[8] BEST_COMBO",         3, 8),
    ]
    for step_name, fi, ti in steps_b:
        lines.append(_delta_row(step_name, results[fi][1], results[ti][1]))
    lines.append("")

    # ── 거래 차단 원인 분석 ────────────────────────────────────────
    lines.append("[거래 차단 원인 분석]")
    r4 = results[4][1]
    r5 = results[5][1]
    r6 = results[6][1]
    r7 = results[7][1]
    r8 = results[8][1]
    sec_block   = r3.total_trades - r4.total_trades
    hold_effect = r3.total_trades - r5.total_trades
    slip_effect = r3.total_trades - r6.total_trades
    sec_relax   = r3.total_trades - r7.total_trades
    lines.append(
        f"  B3 섹터(max=2) 단독 차단: {sec_block:+}건 "
        f"({sec_block / max(r3.total_trades, 1) * 100:.1f}%)"
    )
    lines.append(
        f"  B4 동적보유(완화) 단독 영향: {hold_effect:+}건 "
        f"({hold_effect / max(r3.total_trades, 1) * 100:.1f}%)"
    )
    lines.append(
        f"  B6 동적슬리피지 단독 영향: {slip_effect:+}건 "
        f"({slip_effect / max(r3.total_trades, 1) * 100:.1f}%)"
    )
    lines.append(
        f"  B3 섹터(max=3) 단독 차단: {sec_relax:+}건 "
        f"({sec_relax / max(r3.total_trades, 1) * 100:.1f}%)"
    )
    lines.append("")

    # ── 판정 ──────────────────────────────────────────────────────
    lines.append("[판정]")
    best_cm = max(
        results,
        key=lambda x: (
            abs(x[1].cagr_pct / x[1].max_drawdown_pct)
            if x[1].max_drawdown_pct > 0 else 0.0
        ),
    )
    best_pf  = max(results, key=lambda x: x[1].profit_factor
                   if x[1].profit_factor != float("inf") else 0.0)
    best_pnl = max(results, key=lambda x: x[1].final_capital - x[1].initial_capital)
    best_mdd = min(results, key=lambda x: x[1].max_drawdown_pct)

    def _cm(r): return abs(r.cagr_pct / r.max_drawdown_pct) if r.max_drawdown_pct > 0 else 0.0

    lines.append(f"  CAGR/MDD 최고: {best_cm[0].strip()} ({_cm(best_cm[1]):.2f})")
    lines.append(f"  PF 최고:       {best_pf[0].strip()} (PF {best_pf[1].profit_factor:.2f})")
    _bp_pnl = best_pnl[1].final_capital - best_pnl[1].initial_capital
    lines.append(f"  순손익 최고:   {best_pnl[0].strip()} ({_bp_pnl:+,.0f}원)")
    lines.append(f"  MDD 최저:      {best_mdd[0].strip()} ({best_mdd[1].max_drawdown_pct*100:.1f}%)")
    lines.append("")

    # [8] BEST_COMBO vs [3] 비교
    cm3 = _cm(r3)
    cm8 = _cm(r8)
    lines.append(f"  [8] BEST_COMBO CAGR/MDD: {cm8:.2f}  vs  [3] 기준: {cm3:.2f}")
    if cm8 >= cm3:
        lines.append("  => [8] BEST_COMBO가 [3] 대비 CAGR/MDD 개선됨.")
    else:
        lines.append("  => [8] BEST_COMBO가 [3] 대비 CAGR/MDD 개선 실패.")
    lines.append("  => Scaling(B5): disabled 유지 확정 (이전 실험에서 WR 46.7% 확인).")

    lines.append("")
    lines.append(SEP)

    with open(out_path, "w", encoding="utf-8") as f:
        _print_save(lines, f)

    print(f"\n결과 저장: {out_path}")
    print(SEP)


if __name__ == "__main__":
    main()

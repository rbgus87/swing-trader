"""기준선 재현 + 복합 랭킹 순수 효과 분리.

4개 변형으로 v2.6 기준선을 재현하고, 랭킹 변경의 순수 영향을 측정한다.
부가 분석: 데이터 기간 연장(2026-04-30 → 2026-05-15)의 영향.

실행:
    python experiments/experiment_baseline_restore.py
"""
import sys
import time
from pathlib import Path
import pandas as pd

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
# v2.6 확정 파라미터 (TP1 10%, TP2 10%, trailing 80%)
BASE_PARAMS = StrategyParams(tp1_sell_ratio=0.10, tp2_atr=4.0, tp2_sell_ratio=0.10)
CAPITAL  = 10_000_000
MAX_POS  = 6
MIN_AMT  = 300_000

# ── 비용 ─────────────────────────────────────────────────────────────────
OLD_COST = CostModel(sell_tax_kospi=0.0018, sell_tax_kosdaq=0.0018)  # v2.6 당시
NEW_COST = CostModel()                                                # 현행 0.20%

# ── 슬리피지 ──────────────────────────────────────────────────────────────
SLIP_FIXED = SlippageParams(enabled=False, fixed_slippage=0.0005)    # v2.6 당시 고정

# ── 랭킹 ─────────────────────────────────────────────────────────────────
WEIGHTS_ADX = RankingWeights(rs=0.0, momentum_atr=0.0, adx=1.0,
                              liquidity=0.0, ma_alignment=0.0)
WEIGHTS_CMP = RankingWeights()  # 복합 기본 (rs=0.35, mom=0.25, adx=0.20, ...)

# ── B features 전부 OFF ───────────────────────────────────────────────────
ALL_OFF_SECTOR  = SectorConstraint(enabled=False)
ALL_OFF_HOLD    = DynamicHoldParams(enabled=False)
ALL_OFF_SCALING = ScalingParams(enabled=False)

# v2.6 기준선
V26_TRADES = 847
V26_PF     = 1.95
V26_CAGR   = 0.177
V26_MDD    = 0.321
V26_WR     = 0.599
V26_PERIOD = "2014-01-02 ~ 2026-04-30"


def _run(cost, precomp, regime, slip, preloaded):
    return run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=BASE_PARAMS,
        cost=cost,
        min_position_amount=MIN_AMT,
        preloaded_data=preloaded,
        precomputed=precomp,
        sizing_mode="equity",
        breadth_gate_threshold=0.40,
        regime_gate_enabled=regime,
        sector_constraint=ALL_OFF_SECTOR,
        dynamic_hold=ALL_OFF_HOLD,
        scaling=ALL_OFF_SCALING,
        slippage_params=slip,
    )


def _pct(v): return f"{v * 100:+.1f}%"
def _pct_abs(v): return f"{v * 100:.1f}%"
def _pf(r): return f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"


def _row(label, r):
    pnl = r.final_capital - r.initial_capital
    return (
        f"  {label:<30}  {r.total_trades:>5}  {_pct_abs(r.win_rate):>6}  "
        f"{_pf(r):>5}  {_pct(r.cagr_pct):>7}  {-r.max_drawdown_pct * 100:>5.1f}%  "
        f"{pnl:>+14,.0f}"
    )


def _delta(r_from, r_to):
    pnl_f = r_from.final_capital - r_from.initial_capital
    pnl_t = r_to.final_capital   - r_to.initial_capital
    dt = r_to.total_trades - r_from.total_trades
    dpf = r_to.profit_factor - r_from.profit_factor
    dcagr = (r_to.cagr_pct - r_from.cagr_pct) * 100
    dmdd  = (-r_to.max_drawdown_pct + r_from.max_drawdown_pct) * 100
    dpnl  = pnl_t - pnl_f
    pf_s  = ("+" if dpf >= 0 else "") + f"{dpf:.2f}"
    return (
        f"    d건수={dt:>+4}  dPF={pf_s:>6}  dCAGR={dcagr:>+5.1f}%p  "
        f"dMDD={dmdd:>+5.1f}%p  d순손익={dpnl:>+14,.0f}"
    )


def main():
    out_path = ROOT / "experiments" / "results_baseline_restore.txt"
    SEP = "=" * 78
    DIV = "-" * 78

    print(SEP)
    print("기준선 재현 + 복합 랭킹 순수 효과 분리 실험")
    print(f"v2.6 기준: 건수={V26_TRADES}  PF={V26_PF}  CAGR={V26_CAGR*100:.1f}%"
          f"  MDD=-{V26_MDD*100:.1f}%  ({V26_PERIOD})")
    print(SEP)

    # ── 1. 데이터 로드 ────────────────────────────────────────────────────
    print("\n[1/4] 데이터 로드 중...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    dt_elapsed = time.time() - t0
    period_full = f"{preloaded['trading_dates'][0]} ~ {preloaded['trading_dates'][-1]}"
    print(f"  완료: {dt_elapsed:.1f}s  |  기간: {period_full}")

    # 데이터 기간 연장 분석 — 2026-04-30 이후 거래일 수
    cutoff = "2026-04-30"
    extra_dates = [d for d in preloaded['trading_dates'] if d > cutoff]
    print(f"  v2.6 마감(2026-04-30) 이후 추가 거래일: {len(extra_dates)}일"
          f"  ({extra_dates[0] if extra_dates else 'none'} ~ "
          f"{extra_dates[-1] if extra_dates else 'none'})")

    td   = preloaded["trading_dates"]
    tdat = preloaded["ticker_data"]
    tidx = preloaded["ticker_date_idx"]
    iuniv = set(preloaded["initial_universe"])
    kret  = preloaded.get("kospi_ret_map")
    dret  = preloaded.get("kosdaq_ret_map")
    tmrk  = preloaded.get("ticker_market")

    # ── 2. precomputed 2종 생성 ───────────────────────────────────────────
    print("\n[2/4] 신호 사전 계산 (ADX 단일 / 복합 랭킹)...")

    t0 = time.time()
    precomp_adx = precompute_daily_signals(
        td, tdat, tidx, iuniv, params=BASE_PARAMS,
        kospi_ret_map=kret, kosdaq_ret_map=dret,
        ticker_market=tmrk, weights=WEIGHTS_ADX,
    )
    print(f"  ADX 단일 완료: {time.time() - t0:.1f}s")

    t0 = time.time()
    precomp_cmp = precompute_daily_signals(
        td, tdat, tidx, iuniv, params=BASE_PARAMS,
        kospi_ret_map=kret, kosdaq_ret_map=dret,
        ticker_market=tmrk, weights=WEIGHTS_CMP,
    )
    print(f"  복합 랭킹 완료: {time.time() - t0:.1f}s")

    # ── 3. 4개 변형 실행 ─────────────────────────────────────────────────
    #   [A] TRUE_V26: ADX ranking, old cost 0.18%, breadth only, NO regime MA200
    #   [B] V26_CORRECT_TAX: ADX ranking, new cost 0.20%, breadth only
    #   [C] COMPOSITE_RANKING: composite ranking, new cost 0.20%, breadth only
    #   [D] COMPOSITE_PLUS_REGIME: composite ranking, new cost 0.20%, breadth + MA200

    print("\n[3/4] 4개 변형 백테스트 실행 중...")

    variants = [
        ("[A] TRUE_V26",            OLD_COST, precomp_adx, False, SLIP_FIXED),
        ("[B] V26_CORRECT_TAX",     NEW_COST, precomp_adx, False, SLIP_FIXED),
        ("[C] COMPOSITE_RANKING",   NEW_COST, precomp_cmp, False, SLIP_FIXED),
        ("[D] COMPOSITE_PLUS_REGIME", NEW_COST, precomp_cmp, True, SLIP_FIXED),
    ]

    results = []
    for label, cost, precomp, regime, slip in variants:
        print(f"  {label}...", end=" ", flush=True)
        t0 = time.time()
        r = _run(cost, precomp, regime, slip, preloaded)
        elapsed = time.time() - t0
        results.append((label, r))
        print(
            f"{elapsed:.1f}s  |  건수={r.total_trades}  WR={_pct_abs(r.win_rate)}  "
            f"PF={_pf(r)}  CAGR={_pct(r.cagr_pct)}  MDD={_pct(r.max_drawdown_pct)}"
        )

    # ── 4. 보고서 ────────────────────────────────────────────────────────
    print("\n[4/4] 보고서 생성 중...")

    rA, rB, rC, rD = [r for _, r in results]

    pf_diff   = abs(rA.profit_factor - V26_PF)
    mdd_diff  = abs(rA.max_drawdown_pct - V26_MDD) * 100
    cagr_diff = abs(rA.cagr_pct - V26_CAGR) * 100
    ok_pf     = pf_diff  < V26_PF * 0.10
    ok_mdd    = mdd_diff < V26_MDD * 100 * 0.10
    restored  = ok_pf and ok_mdd

    lines = []
    lines.append("")
    lines.append(SEP)
    lines.append("기준선 재현 + 복합 랭킹 순수 효과 분리 실험")
    lines.append(SEP)
    lines.append(f"현재 데이터 기간: {period_full}")
    lines.append(f"v2.6 기준 기간:   {V26_PERIOD}")
    lines.append(f"연장 거래일 수:   +{len(extra_dates)}일"
                 f"  ({extra_dates[0] if extra_dates else '-'} ~ "
                 f"{extra_dates[-1] if extra_dates else '-'})")
    lines.append("")

    # ── 핵심 지표 표 ─────────────────────────────────────────────────────
    hdr = (f"  {'변형':<30}  {'건수':>5}  {'WR':>6}  {'PF':>5}  "
           f"{'CAGR':>7}  {'MDD':>5}  {'순손익':>14}")
    lines.append("[핵심 지표]")
    lines.append(hdr)
    lines.append("  " + DIV)

    # v2.6 기준선 참고행
    lines.append(
        f"  {'[REF] v2.6 기준 (2026-04-30)':<30}  {V26_TRADES:>5}  "
        f"{V26_WR*100:>5.1f}%  {V26_PF:>5.2f}  "
        f"{V26_CAGR*100:>+6.1f}%  {V26_MDD*100:>5.1f}%  (기준)"
    )
    lines.append("  " + DIV)
    for label, r in results:
        lines.append(_row(label, r))
    lines.append("")

    # ── [A] 기준선 재현 검증 ─────────────────────────────────────────────
    lines.append("[A] TRUE_V26 재현 검증")
    lines.append(f"  실제:  건수={rA.total_trades}  PF={rA.profit_factor:.2f}  "
                 f"CAGR={rA.cagr_pct*100:.1f}%  MDD=-{rA.max_drawdown_pct*100:.1f}%")
    lines.append(f"  v2.6:  건수={V26_TRADES}  PF={V26_PF:.2f}  "
                 f"CAGR={V26_CAGR*100:.1f}%  MDD=-{V26_MDD*100:.1f}%")
    lines.append(f"  차이:  d건수={rA.total_trades - V26_TRADES:+}  "
                 f"dPF={rA.profit_factor - V26_PF:+.2f}  "
                 f"dCAGR={rA.cagr_pct*100 - V26_CAGR*100:+.1f}%p  "
                 f"dMDD={-(rA.max_drawdown_pct - V26_MDD)*100:+.1f}%p")
    restore_str = "OK (±10% 이내)" if restored else "MISMATCH"
    lines.append(f"  판정:  {restore_str}")
    lines.append("")

    # 재현 실패 시 원인 분석
    if not restored:
        lines.append("  [원인 분석]")
        lines.append("")
        lines.append("  가설 1 — 데이터 기간 연장 효과:")
        lines.append(
            f"    v2.6는 2026-04-30 마감. 현재는 +{len(extra_dates)}거래일 추가."
        )
        if len(extra_dates) > 0:
            lines.append(
                f"    추가 기간({extra_dates[0]} ~ {extra_dates[-1]})의 시장 상황이"
                " 불리했다면"
            )
            lines.append(
                "    손실 거래가 추가되어 PF 하락·건수 증가 가능."
            )
        lines.append("")
        lines.append("  가설 2 — compute_composite_score 기본값 적용:")
        lines.append(
            "    precompute_daily_signals(weights=None) 호출 시"
            " compute_composite_score가 weights=None →"
        )
        lines.append(
            "    RankingWeights() 기본값(복합) 적용됨."
            " v2.6 측정 당시 weights 파라미터 존재 여부 불명확."
        )
        lines.append(
            "    → [A] ADX 단일과 [C] 복합의 PF 차이로 이 가설 검증 가능 (아래 참조)."
        )
        lines.append("")
        lines.append("  가설 3 — Phase B 코드 추가로 인한 동작 변경:")
        lines.append(
            "    Phase B-4~B-6 구현 시 backtester 내부 로직이 변경되어"
        )
        lines.append(
            "    enabled=False 상태에서도 미세한 동작 차이가 생길 수 있음."
        )
        lines.append("")

    # ── 단계별 기여도 ─────────────────────────────────────────────────────
    lines.append("[단계별 기여도]")
    lines.append(DIV)

    step_labels = [
        ("[A]->[B] 거래세 0.18→0.20%",    rA, rB),
        ("[B]->[C] ADX→복합 랭킹 변경",    rB, rC),
        ("[C]->[D] +이중 국면 필터(B2)",   rC, rD),
    ]
    for name, r_from, r_to in step_labels:
        lines.append(f"  {name}")
        lines.append(_delta(r_from, r_to))

    lines.append("")

    # ── 핵심 질문 답변 ────────────────────────────────────────────────────
    lines.append("[핵심 질문 답변]")

    # Q1: 복합 랭킹이 PF를 변화시키는가?
    ranking_dpf = rC.profit_factor - rB.profit_factor
    ranking_verdict = "NEUTRAL" if abs(ranking_dpf) < 0.05 else ("UP" if ranking_dpf > 0 else "DOWN")
    lines.append(f"  Q1: 복합 랭킹이 PF를 변화시키는가? ([B] vs [C])")
    lines.append(f"  A1: dPF = {ranking_dpf:+.2f} → {ranking_verdict}")
    if abs(ranking_dpf) < 0.05:
        lines.append("      복합 랭킹이 PF에 실질적인 영향 없음.")
        lines.append("      랭킹 차이는 '어떤 종목'을 선택하느냐이지, 알파 자체는 동일.")
    else:
        lines.append(f"      복합 랭킹이 PF를 {'개선' if ranking_dpf > 0 else '악화'}시킴.")
    lines.append("")

    # Q2: PF 1.95→현재 갭 원인
    gap = V26_PF - rA.profit_factor
    lines.append(f"  Q2: v2.6 PF {V26_PF:.2f} → 현재 [A] PF {rA.profit_factor:.2f}"
                 f"  (갭 = {gap:.2f}) 의 원인은?")
    lines.append(f"  A2: [A] → [D] 전체 단계별 기여:")
    lines.append(f"      거래세 교정:   dPF = {rB.profit_factor - rA.profit_factor:+.2f}")
    lines.append(f"      복합 랭킹:     dPF = {rC.profit_factor - rB.profit_factor:+.2f}")
    lines.append(f"      이중 국면:     dPF = {rD.profit_factor - rC.profit_factor:+.2f}")
    lines.append(f"      합계 변화:     dPF = {rD.profit_factor - rA.profit_factor:+.2f}")
    lines.append(f"      설명 불가 잔여: {gap - (rA.profit_factor - rD.profit_factor):+.2f}"
                 f"  (데이터 기간 연장 or 코드 변경 영향)")
    lines.append("")

    # ── 권장 조치 ─────────────────────────────────────────────────────────
    lines.append("[권장 조치]")

    # 랭킹 판정
    if abs(ranking_dpf) < 0.05:
        lines.append("  복합 랭킹: 중립. 현행 유지 (WR 개선 측면에서 이득).")
    elif ranking_dpf > 0:
        lines.append("  복합 랭킹: PF 개선 확인. 유지.")
    else:
        lines.append("  복합 랭킹: PF 소폭 하락. ADX 단일 복귀 검토 가능.")

    # 기간 연장 영향
    if len(extra_dates) > 0 and not restored:
        lines.append(
            f"  기간 연장: 2026-05 추가 데이터({len(extra_dates)}일)가 PF 갭의"
            " 주요 원인일 가능성."
        )
        lines.append(
            "  데이터 기간을 2026-04-30으로 제한한 별도 백테스트로 검증 권장."
        )

    lines.append(
        "  현재 최종 조합 (A+B1+B2+B6, [D] 기준선)을 그대로 유지하고"
    )
    lines.append(
        "  v2.6 기준선 재현보다 페이퍼 트레이딩으로 실전 괴리 측정에 집중 권장."
    )

    lines.append("")
    lines.append(SEP)

    with open(out_path, "w", encoding="utf-8") as f:
        for line in lines:
            print(line)
            f.write(line + "\n")

    print(f"\n결과 저장: {out_path}")
    print(SEP)


if __name__ == "__main__":
    main()

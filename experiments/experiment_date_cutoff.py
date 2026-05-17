"""데이터 기간 제한 검증 — v2.6 PF 1.95 재현 시도.

trading_dates를 2026-04-30 이전으로 제한하여:
  [A] v2.6 조건 그대로 → PF 1.85~2.05이면 "기간 차이가 원인" 확정
  [B] 현재 최종 조합 cutoff 버전 → 동일 기간의 개선 효과 측정

실행:
    python experiments/experiment_date_cutoff.py
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

CAPITAL  = 10_000_000
MAX_POS  = 6
MIN_AMT  = 300_000
CUTOFF   = "2026-04-30"

# v2.6 파라미터
BASE_PARAMS = StrategyParams(tp1_sell_ratio=0.10, tp2_atr=4.0, tp2_sell_ratio=0.10)

# v2.6 기준선
V26 = dict(trades=847, pf=1.95, cagr=0.177, mdd=0.321, wr=0.599)

# 비용/슬리피지
OLD_COST   = CostModel(sell_tax_kospi=0.0018, sell_tax_kosdaq=0.0018)
NEW_COST   = CostModel()
SLIP_FIXED = SlippageParams(enabled=False, fixed_slippage=0.0005)
SLIP_DYN   = SlippageParams(enabled=True, base_slippage=0.0003,
                             impact_coefficient=0.1, max_slippage=0.02)

# 랭킹 가중치
W_ADX = RankingWeights(rs=0.0, momentum_atr=0.0, adx=1.0, liquidity=0.0, ma_alignment=0.0)
W_CMP = RankingWeights()

# B features OFF
OFF_SECTOR  = SectorConstraint(enabled=False)
OFF_HOLD    = DynamicHoldParams(enabled=False)
OFF_SCALING = ScalingParams(enabled=False)


def _pct(v): return f"{v * 100:+.1f}%"
def _pct_abs(v): return f"{v * 100:.1f}%"
def _pf(r): return f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"


def main():
    out_path = ROOT / "experiments" / "results_date_cutoff.txt"
    SEP = "=" * 72
    DIV = "-" * 72

    print(SEP)
    print(f"데이터 기간 제한 검증 (cutoff: {CUTOFF})")
    print(f"v2.6 기준: 건수={V26['trades']}  PF={V26['pf']}  "
          f"CAGR={V26['cagr']*100:.1f}%  MDD=-{V26['mdd']*100:.1f}%")
    print(SEP)

    # ── 1. 전체 데이터 로드 ───────────────────────────────────────────────
    print("\n[1/4] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    print(f"  완료: {time.time() - t0:.1f}s")

    full_dates   = preloaded['trading_dates']
    cut_dates    = [d for d in full_dates if d <= CUTOFF]
    extra_dates  = [d for d in full_dates if d > CUTOFF]
    print(f"  전체 기간: {full_dates[0]} ~ {full_dates[-1]}  ({len(full_dates)}일)")
    print(f"  cutoff 후: {cut_dates[0]} ~ {cut_dates[-1]}   ({len(cut_dates)}일)")
    print(f"  제거된 날: {len(extra_dates)}일  "
          f"({extra_dates[0] if extra_dates else '-'} ~ "
          f"{extra_dates[-1] if extra_dates else '-'})")

    # preloaded를 cutoff 버전으로 교체
    preloaded_cut = dict(preloaded)
    preloaded_cut['trading_dates'] = cut_dates

    td   = cut_dates
    tdat = preloaded['ticker_data']
    tidx = preloaded['ticker_date_idx']
    iuniv = set(preloaded['initial_universe'])
    kret  = preloaded.get('kospi_ret_map')
    dret  = preloaded.get('kosdaq_ret_map')
    tmrk  = preloaded.get('ticker_market')

    # ── 2. precomp 2종 생성 (cutoff 기간) ────────────────────────────────
    print("\n[2/4] 신호 사전 계산 (ADX / 복합 랭킹, cutoff 적용)...")

    t0 = time.time()
    precomp_adx = precompute_daily_signals(
        td, tdat, tidx, iuniv, params=BASE_PARAMS,
        kospi_ret_map=kret, kosdaq_ret_map=dret,
        ticker_market=tmrk, weights=W_ADX,
    )
    print(f"  ADX 단일 완료: {time.time() - t0:.1f}s")

    t0 = time.time()
    precomp_cmp = precompute_daily_signals(
        td, tdat, tidx, iuniv, params=BASE_PARAMS,
        kospi_ret_map=kret, kosdaq_ret_map=dret,
        ticker_market=tmrk, weights=W_CMP,
    )
    print(f"  복합 랭킹 완료: {time.time() - t0:.1f}s")

    # ── 3. 2개 변형 실행 ─────────────────────────────────────────────────
    print("\n[3/4] 백테스트 실행 중 (cutoff 기간)...")

    # [A] v2.6 조건 그대로: ADX 랭킹 + 거래세 0.18% + breadth만 + B features OFF
    print("  [A] v2.6 조건 (ADX+0.18%+breadth)...", end=" ", flush=True)
    t0 = time.time()
    rA = run_portfolio_backtest(
        initial_capital=CAPITAL, max_positions=MAX_POS,
        params=BASE_PARAMS, cost=OLD_COST,
        min_position_amount=MIN_AMT,
        preloaded_data=preloaded_cut, precomputed=precomp_adx,
        sizing_mode='equity',
        breadth_gate_threshold=0.40,
        regime_gate_enabled=False,
        sector_constraint=OFF_SECTOR,
        dynamic_hold=OFF_HOLD,
        scaling=OFF_SCALING,
        slippage_params=SLIP_FIXED,
    )
    tA = time.time() - t0
    print(f"{tA:.1f}s  |  건수={rA.total_trades}  WR={_pct_abs(rA.win_rate)}  "
          f"PF={_pf(rA)}  CAGR={_pct(rA.cagr_pct)}  MDD={_pct(rA.max_drawdown_pct)}")

    # [B] 현재 최종 조합 cutoff: 복합 랭킹 + 0.20% + 이중 국면 + 동적 슬리피지
    print("  [B] 현재 최종 조합 (복합+0.20%+이중국면+동적슬리피지)...", end=" ", flush=True)
    t0 = time.time()
    rB = run_portfolio_backtest(
        initial_capital=CAPITAL, max_positions=MAX_POS,
        params=BASE_PARAMS, cost=NEW_COST,
        min_position_amount=MIN_AMT,
        preloaded_data=preloaded_cut, precomputed=precomp_cmp,
        sizing_mode='equity',
        breadth_gate_threshold=0.40,
        regime_gate_enabled=True,
        sector_constraint=OFF_SECTOR,
        dynamic_hold=OFF_HOLD,
        scaling=OFF_SCALING,
        slippage_params=SLIP_DYN,
    )
    tB = time.time() - t0
    print(f"{tB:.1f}s  |  건수={rB.total_trades}  WR={_pct_abs(rB.win_rate)}  "
          f"PF={_pf(rB)}  CAGR={_pct(rB.cagr_pct)}  MDD={_pct(rB.max_drawdown_pct)}")

    # 참고: 전체 기간 [A]와 [B] 수치 (이전 실험 결과)
    FULL_A = dict(trades=1075, pf=1.19, cagr=0.057, mdd=0.470)  # experiment_baseline_restore [A]
    FULL_B = dict(trades=910,  pf=1.23, cagr=0.065, mdd=0.341)  # experiment_baseline_restore [D]

    # ── 4. 보고서 ────────────────────────────────────────────────────────
    print("\n[4/4] 보고서 생성 중...")

    pf_diff  = abs(rA.profit_factor - V26['pf'])
    restored = pf_diff < V26['pf'] * 0.10  # ±10%

    dpf_cut_vs_full_A   = rA.profit_factor - FULL_A['pf']
    dcagr_cut_vs_full_A = (rA.cagr_pct - FULL_A['cagr']) * 100
    dmdd_cut_vs_full_A  = (-rA.max_drawdown_pct + FULL_A['mdd']) * 100
    dtrades_A = rA.total_trades - FULL_A['trades']

    dpf_cut_vs_full_B   = rB.profit_factor - FULL_B['pf']
    dcagr_cut_vs_full_B = (rB.cagr_pct - FULL_B['cagr']) * 100
    dmdd_cut_vs_full_B  = (-rB.max_drawdown_pct + FULL_B['mdd']) * 100
    dtrades_B = rB.total_trades - FULL_B['trades']

    pnl_A = rA.final_capital - rA.initial_capital
    pnl_B = rB.final_capital - rB.initial_capital

    lines = []
    lines.append("")
    lines.append(SEP)
    lines.append(f"데이터 기간 제한 검증 (cutoff: {CUTOFF})")
    lines.append(SEP)
    lines.append(f"cutoff 기간: {cut_dates[0]} ~ {cut_dates[-1]}  ({len(cut_dates)}일)")
    lines.append(f"제거된 날:   {len(extra_dates)}일"
                 f"  ({extra_dates[0] if extra_dates else '-'} ~ "
                 f"{extra_dates[-1] if extra_dates else '-'})")
    lines.append("")

    hdr = (f"  {'변형':<38}  {'건수':>5}  {'WR':>6}  {'PF':>5}  "
           f"{'CAGR':>7}  {'MDD':>6}  {'순손익':>14}")
    lines.append("[결과 비교]")
    lines.append(hdr)
    lines.append("  " + DIV)

    # v2.6 참조행
    lines.append(
        f"  {'[REF] v2.6 (2026-04-30 cutoff)':<38}  {V26['trades']:>5}  "
        f"{V26['wr']*100:>5.1f}%  {V26['pf']:>5.2f}  "
        f"{V26['cagr']*100:>+6.1f}%  {V26['mdd']*100:>5.1f}%  (기준)"
    )
    lines.append("  " + DIV)

    def _res_row(label, r, pnl):
        return (
            f"  {label:<38}  {r.total_trades:>5}  {r.win_rate*100:>5.1f}%  "
            f"{_pf(r):>5}  {r.cagr_pct*100:>+6.1f}%  "
            f"{r.max_drawdown_pct*100:>5.1f}%  {pnl:>+14,.0f}"
        )

    lines.append(_res_row("[A] v2.6 조건 (ADX+0.18%, cutoff)", rA, pnl_A))
    lines.append(_res_row("[B] 최종 조합 (복합+이중국면, cutoff)", rB, pnl_B))
    lines.append("")

    # 전체 기간과 비교
    lines.append("[cutoff 적용 vs 전체 기간 비교]")
    lines.append(f"  {'변형':<38}  {'d건수':>5}  {'dPF':>6}  {'dCAGR':>8}  {'dMDD':>8}")
    lines.append("  " + DIV)
    lines.append(
        f"  {'[A] cutoff vs 전체':<38}  {dtrades_A:>+5}  "
        f"{dpf_cut_vs_full_A:>+6.2f}  {dcagr_cut_vs_full_A:>+7.1f}%p  "
        f"{dmdd_cut_vs_full_A:>+7.1f}%p"
    )
    lines.append(
        f"  {'[B] cutoff vs 전체':<38}  {dtrades_B:>+5}  "
        f"{dpf_cut_vs_full_B:>+6.2f}  {dcagr_cut_vs_full_B:>+7.1f}%p  "
        f"{dmdd_cut_vs_full_B:>+7.1f}%p"
    )
    lines.append("")

    # 재현 판정
    lines.append("[재현 판정]")
    lines.append(
        f"  [A] PF = {rA.profit_factor:.2f}  vs  v2.6 PF = {V26['pf']:.2f}  "
        f"(차이 {rA.profit_factor - V26['pf']:+.2f}, "
        f"허용범위 ±{V26['pf']*0.10:.2f})"
    )

    if restored:
        lines.append("  판정: OK — 데이터 기간 차이가 PF 갭의 주요 원인으로 확정.")
        lines.append("")
        lines.append("  결론:")
        lines.append(
            f"    2026-05 추가 {len(extra_dates)}거래일이 PF를 "
            f"{dpf_cut_vs_full_A:+.2f} 하락시켰음."
        )
        lines.append(
            "    현재 최종 조합(B1+B2+B6)을 동일 cutoff로 측정하면"
            f" PF {rB.profit_factor:.2f}."
        )
        lines.append(
            "    페이퍼 트레이딩 이후 실전 데이터로 재평가 권장."
        )
    else:
        lines.append("  판정: MISMATCH — 데이터 기간 이외의 원인 존재.")
        lines.append("")
        lines.append("  결론:")
        lines.append(
            f"    cutoff 적용으로 건수 {dtrades_A:+}건, PF {dpf_cut_vs_full_A:+.2f} 변화했으나"
        )
        lines.append(f"    v2.6 PF {V26['pf']:.2f}에는 도달하지 못함.")
        gap_remaining = V26['pf'] - rA.profit_factor
        lines.append(
            f"    잔여 갭 {gap_remaining:.2f} = 코드 변경 또는 v2.6 측정 파라미터 차이."
        )
        lines.append("    Phase B 구현 이전 코드로 재실행하거나")
        lines.append("    v2.6 측정 당시 설정을 정확히 재현해야 검증 가능.")
        lines.append("")
        lines.append("  실용적 권장:")
        lines.append(
            "    v2.6 기준선 재현보다 현재 확정 조합(PF"
            f" {rB.profit_factor:.2f}, cutoff 기준)을 새 기준으로 채택."
        )
        lines.append(
            "    페이퍼 트레이딩 개시 → 실전 성과로 백테스트 갭 측정."
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

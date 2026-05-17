"""Phase A+B 개선 효과 비교 백테스트.

7개 변형을 동일 데이터/기간/시드로 비교하여 Phase A+B 개선사항의
실제 성과 영향을 측정한다.

실행:
    python experiments/experiment_ab_compare.py
"""
import sys
import time
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows CP949 콘솔에서 Unicode 출력 가능하도록 설정
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger
logger.remove()  # 백테스터 로그 최소화
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

# ── 공통 파라미터 ────────────────────────────────────────────────────────────
BASE_PARAMS = StrategyParams(
    tp1_sell_ratio=0.10,
    tp2_atr=4.0,
    tp2_sell_ratio=0.10,
)
CAPITAL = 10_000_000
MAX_POS = 6
MIN_AMOUNT = 300_000

# ── 비용 모델 ────────────────────────────────────────────────────────────────
# 기존 v2.6 비용 (CLAUDE.md experiment 17c 기준)
OLD_COST = CostModel(
    buy_commission=0.00015,
    sell_commission=0.00015,
    sell_tax_kospi=0.0018,     # 구 세율 0.18%
    sell_tax_kosdaq=0.0018,
    slippage=0.0005,           # 고정 0.05%
)

# 현행 정확 비용 (Phase A-1: 0.20%)
NEW_COST = CostModel()  # defaults: sell_tax 0.20%, slippage 0.05%

# ── 슬리피지 파라미터 ────────────────────────────────────────────────────────
SLIP_OFF = SlippageParams(enabled=False, fixed_slippage=0.0005)    # 고정 슬리피지 (비활성)
SLIP_ON  = SlippageParams(enabled=True, base_slippage=0.0003,
                          impact_coefficient=0.1, max_slippage=0.02)  # 동적 슬리피지

# ── 랭킹 가중치 ─────────────────────────────────────────────────────────────
WEIGHTS_ADX  = RankingWeights(rs=0.0, momentum_atr=0.0, adx=1.0, liquidity=0.0, ma_alignment=0.0)
WEIGHTS_NEW  = RankingWeights()  # 기본 복합 가중치
WEIGHTS_CONS = RankingWeights(rs=0.50, momentum_atr=0.30, adx=0.10, liquidity=0.10, ma_alignment=0.0)

# ── Phase B 기능 on/off 헬퍼 ─────────────────────────────────────────────────
SECTOR_OFF   = SectorConstraint(enabled=False)
SECTOR_ON    = SectorConstraint(enabled=True, max_per_sector=2)
DYNHOLD_OFF  = DynamicHoldParams(enabled=False)
DYNHOLD_ON   = DynamicHoldParams(enabled=True, base_hold_days=20,
                                  extend_multiplier=1.5, shorten_multiplier=0.5,
                                  adx_strong_threshold=30, adx_weak_threshold=15)
SCALING_OFF  = ScalingParams(enabled=False)
SCALING_ON   = ScalingParams(enabled=True, first_entry_ratio=0.60,
                              second_entry_ratio=0.40, scale_in_atr_mult=1.0,
                              max_tranches=2, adjust_stop_on_scale=True)


def _fmt_pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def _fmt_pct_abs(v: float) -> str:
    return f"{v * 100:.1f}%"


def _fmt_pnl(v: float) -> str:
    return f"{v:+,.0f}"


def _run_variant(
    label: str,
    cost: CostModel,
    precomputed: dict,
    regime_gate: bool,
    sector: SectorConstraint,
    dynhold: DynamicHoldParams,
    scaling: ScalingParams,
    slip: SlippageParams,
    preloaded: dict,
) -> tuple:
    """단일 변형 실행 → (PortfolioResult, elapsed_sec)."""
    t0 = time.time()
    result = run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=BASE_PARAMS,
        cost=cost,
        min_position_amount=MIN_AMOUNT,
        preloaded_data=preloaded,
        precomputed=precomputed,
        sizing_mode="equity",
        regime_gate_enabled=regime_gate,
        sector_constraint=sector,
        dynamic_hold=dynhold,
        scaling=scaling,
        slippage_params=slip,
    )
    elapsed = time.time() - t0
    return result, elapsed


def _print_and_log(lines: list[str], f) -> None:
    for line in lines:
        print(line)
        f.write(line + "\n")


def main():
    out_path = ROOT / "experiments" / "results_ab_compare.txt"

    print("=" * 70)
    print("Phase A+B 개선 효과 비교 백테스트")
    print("=" * 70)

    # ── 1. 데이터 1회 로드 ──────────────────────────────────────────────────
    print("\n[1/4] 데이터 로드 중 (1회만 실행)...")
    t_load = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    print(f"  완료: {time.time() - t_load:.1f}s")

    td = preloaded["trading_dates"]
    ticker_data = preloaded["ticker_data"]
    ticker_date_idx = preloaded["ticker_date_idx"]
    init_univ = set(preloaded["initial_universe"])
    kospi_ret = preloaded.get("kospi_ret_map")
    kosdaq_ret = preloaded.get("kosdaq_ret_map")
    ticker_market = preloaded.get("ticker_market")

    # ── 2. 랭킹별 precomputed 3종 생성 ────────────────────────────────────
    print("\n[2/4] 신호 사전 계산 중 (랭킹 변형 3종)...")

    print("  ADX 단일 랭킹...")
    t0 = time.time()
    precomp_adx = precompute_daily_signals(
        td, ticker_data, ticker_date_idx, init_univ,
        params=BASE_PARAMS,
        kospi_ret_map=kospi_ret, kosdaq_ret_map=kosdaq_ret,
        ticker_market=ticker_market, weights=WEIGHTS_ADX,
    )
    print(f"    완료: {time.time() - t0:.1f}s")

    print("  복합 랭킹 (기본)...")
    t0 = time.time()
    precomp_new = precompute_daily_signals(
        td, ticker_data, ticker_date_idx, init_univ,
        params=BASE_PARAMS,
        kospi_ret_map=kospi_ret, kosdaq_ret_map=kosdaq_ret,
        ticker_market=ticker_market, weights=WEIGHTS_NEW,
    )
    print(f"    완료: {time.time() - t0:.1f}s")

    print("  복합 랭킹 (보수적)...")
    t0 = time.time()
    precomp_cons = precompute_daily_signals(
        td, ticker_data, ticker_date_idx, init_univ,
        params=BASE_PARAMS,
        kospi_ret_map=kospi_ret, kosdaq_ret_map=kosdaq_ret,
        ticker_market=ticker_market, weights=WEIGHTS_CONS,
    )
    print(f"    완료: {time.time() - t0:.1f}s")

    # ── 3. 7개 변형 실행 ──────────────────────────────────────────────────
    print("\n[3/4] 7개 변형 백테스트 실행 중...")

    variants_cfg = [
        # (label, cost, precomp, regime_gate, sector, dynhold, scaling, slip)
        ("[1] OLD_BASELINE",     OLD_COST, precomp_adx, False, SECTOR_OFF, DYNHOLD_OFF, SCALING_OFF, SLIP_OFF),
        ("[2] A_ONLY",           NEW_COST, precomp_adx, False, SECTOR_OFF, DYNHOLD_OFF, SCALING_OFF, SLIP_OFF),
        ("[3] A+B1_RANKING",     NEW_COST, precomp_new, False, SECTOR_OFF, DYNHOLD_OFF, SCALING_OFF, SLIP_OFF),
        ("[4] A+B1+B2_REGIME",   NEW_COST, precomp_new, True,  SECTOR_OFF, DYNHOLD_OFF, SCALING_OFF, SLIP_OFF),
        ("[5] A+B_ALL",          NEW_COST, precomp_new, True,  SECTOR_ON,  DYNHOLD_ON,  SCALING_OFF, SLIP_ON),
        ("[6] A+B_SCALING",      NEW_COST, precomp_new, True,  SECTOR_ON,  DYNHOLD_ON,  SCALING_ON,  SLIP_ON),
        ("[7] A+B_CONSERVATIVE", NEW_COST, precomp_cons,True,  SECTOR_ON,  DYNHOLD_ON,  SCALING_OFF, SLIP_ON),
    ]

    results = []
    for label, cost, precomp, regime_gate, sector, dynhold, scaling, slip in variants_cfg:
        print(f"  {label}...", end=" ", flush=True)
        r, elapsed = _run_variant(
            label, cost, precomp, regime_gate, sector, dynhold, scaling, slip, preloaded
        )
        results.append((label, r, elapsed))
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
        print(
            f"{elapsed:.1f}s  |  "
            f"건수={r.total_trades}  WR={_fmt_pct_abs(r.win_rate)}  "
            f"PF={pf_str}  CAGR={_fmt_pct(r.cagr_pct)}  "
            f"MDD={_fmt_pct(r.max_drawdown_pct)}"
        )

    # ── 4. 보고서 출력 ───────────────────────────────────────────────────
    print("\n[4/4] 보고서 생성 중...")

    period = results[0][1].period
    base_r = results[0][1]
    base_pnl = base_r.final_capital - base_r.initial_capital

    lines = []
    SEP = "=" * 80
    DIV = "-" * 80

    lines.append("")
    lines.append(SEP)
    lines.append("Phase A+B 개선 효과 비교 (10M/6종목, 12년 백테스트)")
    lines.append(SEP)
    lines.append(f"Period: {period}")
    lines.append(f"Capital: {CAPITAL:,}원 / Max positions: {MAX_POS}")
    lines.append("")

    # 핵심 지표 표
    lines.append("[핵심 지표]")
    hdr = (
        f"{'변형':<22}  {'건수':>5}  {'WR':>6}  {'PF':>5}  "
        f"{'CAGR':>7}  {'MDD':>7}  {'순손익':>14}  {'CAGR/MDD':>8}"
    )
    lines.append(hdr)
    lines.append(DIV)

    for label, r, elapsed in results:
        pnl = r.final_capital - r.initial_capital
        cagr_mdd = abs(r.cagr_pct / r.max_drawdown_pct) if r.max_drawdown_pct > 0 else 0
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "  ∞"
        row = (
            f"{label:<22}  {r.total_trades:>5}  "
            f"{r.win_rate * 100:>5.1f}%  {pf_str:>5}  "
            f"{r.cagr_pct * 100:>6.1f}%  {-r.max_drawdown_pct * 100:>6.1f}%  "
            f"{pnl:>+14,.0f}  {cagr_mdd:>8.2f}"
        )
        lines.append(row)

    lines.append("")

    # 기준선 대비 변화
    lines.append("[1] OLD_BASELINE 대비 변화")
    hdr2 = (
        f"{'변형':<22}  {'dPF':>6}  {'dCAGR':>7}  "
        f"{'dMDD':>7}  {'dWR':>7}  {'d순손익':>14}"
    )
    lines.append(hdr2)
    lines.append(DIV)

    for label, r, elapsed in results[1:]:
        pnl = r.final_capital - r.initial_capital
        dpf = r.profit_factor - base_r.profit_factor
        dcagr = (r.cagr_pct - base_r.cagr_pct) * 100
        dmdd = (-r.max_drawdown_pct + base_r.max_drawdown_pct) * 100  # 양수 = MDD 감소 = 개선
        dwr = (r.win_rate - base_r.win_rate) * 100
        dpnl = pnl - base_pnl
        pf_sign = "+" if dpf >= 0 else ""
        row = (
            f"{label:<22}  {pf_sign}{dpf:>5.2f}  "
            f"{dcagr:>+6.1f}%p  {dmdd:>+6.1f}%p  "
            f"{dwr:>+6.1f}%p  {dpnl:>+14,.0f}"
        )
        lines.append(row)

    lines.append("")

    # 판정
    lines.append("[판정]")

    best_cagr_mdd = max(
        results,
        key=lambda x: abs(x[1].cagr_pct / x[1].max_drawdown_pct) if x[1].max_drawdown_pct > 0 else 0,
    )
    best_pf = max(results, key=lambda x: x[1].profit_factor if x[1].profit_factor != float("inf") else 0)
    best_mdd = min(results, key=lambda x: x[1].max_drawdown_pct)
    best_pnl = max(results, key=lambda x: x[1].final_capital - x[1].initial_capital)

    _b_cm = best_cagr_mdd
    _cm_val = abs(_b_cm[1].cagr_pct / _b_cm[1].max_drawdown_pct) if _b_cm[1].max_drawdown_pct > 0 else 0
    lines.append(f"  CAGR/MDD 최고:  {_b_cm[0].strip()} ({_cm_val:.2f})")
    lines.append(f"  PF 최고:        {best_pf[0].strip()} (PF {best_pf[1].profit_factor:.2f})")
    lines.append(f"  MDD 최저:       {best_mdd[0].strip()} (-{best_mdd[1].max_drawdown_pct * 100:.1f}%)")
    _bp_pnl = best_pnl[1].final_capital - best_pnl[1].initial_capital
    lines.append(f"  순손익 최고:    {best_pnl[0].strip()} ({_bp_pnl:+,.0f}원)")

    lines.append("")

    # Phase별 기여도 분석
    lines.append("[Phase별 누적 기여도]")
    lines.append(f"  {'단계':<30}  {'dPF':>6}  {'dCAGR':>7}  {'dMDD':>8}  {'d순손익':>14}")
    lines.append("  " + "-" * 68)

    steps = [
        ("A  (비용 정확화 0.18→0.20%)", 0, 1),
        ("B1 (복합 랭킹)", 1, 2),
        ("B2 (이중 국면 필터 추가)", 2, 3),
        ("B3-B6 (섹터+동적보유+슬리피지)", 3, 4),
        ("B5 (분할 매수 추가)", 4, 5),
        ("보수적 랭킹 비교 [7]", 0, 6),
    ]
    for step_name, from_idx, to_idx in steps:
        r_from = results[from_idx][1]
        r_to   = results[to_idx][1]
        dpf   = r_to.profit_factor - r_from.profit_factor
        dcagr = (r_to.cagr_pct - r_from.cagr_pct) * 100
        dmdd  = (-r_to.max_drawdown_pct + r_from.max_drawdown_pct) * 100
        dpnl  = (r_to.final_capital - r_to.initial_capital) - (r_from.final_capital - r_from.initial_capital)
        pf_sign = "+" if dpf >= 0 else ""
        lines.append(
            f"  {step_name:<30}  {pf_sign}{dpf:>5.2f}  "
            f"{dcagr:>+6.1f}%p  {dmdd:>+6.1f}%p  {dpnl:>+14,.0f}"
        )

    lines.append("")
    lines.append(SEP)
    lines.append("검증: [1] OLD_BASELINE이 v2.6 기준선(PF~1.95, MDD~32%)에 근접하면 정상.")
    lines.append(f"      실제: PF={base_r.profit_factor:.2f}, MDD={base_r.max_drawdown_pct*100:.1f}%, "
                 f"CAGR={base_r.cagr_pct*100:.1f}%")
    lines.append(SEP)

    # 출력 (터미널 + 파일)
    with open(out_path, "w", encoding="utf-8") as f:
        _print_and_log(lines, f)

    print(f"\n결과 저장 완료: {out_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()

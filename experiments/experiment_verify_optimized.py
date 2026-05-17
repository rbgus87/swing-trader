"""v2.7 파라미터 검증 백테스트.

config.yaml의 최적화된 파라미터를 코드에서 직접 읽어
최적화 실험 결과(PF ~1.27, WR ~59.5%, CAGR ~7.3%)와 일치하는지 확인한다.

실행:
    python experiments/experiment_verify_optimized.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml
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

# ── config.yaml에서 파라미터 로드 ──────────────────────────────────────────
with open(ROOT / "config.yaml", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

tf  = cfg["trend_following"]
trd = cfg["trading"]

CAPITAL    = int(cfg["trading"]["initial_capital"])
MAX_POS    = int(trd["max_positions"])
MIN_AMOUNT = int(cfg["risk"]["min_position_amount"])

PARAMS = StrategyParams(
    adx_threshold=float(tf["adx_threshold"]),
    relative_strength_threshold=float(tf["relative_strength_threshold"]),
    stop_loss_atr=float(tf["stop_loss_atr"]),
    take_profit_atr=float(tf["take_profit_atr"]),
    trailing_atr=float(tf["trailing_atr"]),
    max_hold_days=int(tf["max_hold_days"]),
    tp1_sell_ratio=float(tf["tp1_sell_ratio"]),
    tp2_atr=float(tf["tp2_atr"]),
    tp2_sell_ratio=float(tf["tp2_sell_ratio"]),
    ma60_position_min=float(tf["ma60_position_min"]),
    ma60_position_max=float(tf["ma60_position_max"]),
    atr_price_min=float(tf["atr_price_min"]),
    atr_price_max=float(tf["atr_price_max"]),
    min_trading_value=float(tf["min_trading_value"]),
)

rw_cfg   = tf.get("ranking_weights", {})
WEIGHTS  = RankingWeights(
    rs=float(rw_cfg.get("rs", 0.35)),
    momentum_atr=float(rw_cfg.get("momentum_atr", 0.25)),
    adx=float(rw_cfg.get("adx", 0.20)),
    liquidity=float(rw_cfg.get("liquidity", 0.10)),
    ma_alignment=float(rw_cfg.get("ma_alignment", 0.10)),
)
BREADTH  = 0.40
COST     = CostModel()
SECTOR   = SectorConstraint(enabled=False)
DYNHOLD  = DynamicHoldParams(enabled=False)
SCALING  = ScalingParams(enabled=False)

slip_cfg = tf.get("slippage_model", {})
SLIP     = SlippageParams(
    enabled=bool(slip_cfg.get("enabled", True)),
    base_slippage=float(slip_cfg.get("base_slippage", 0.0003)),
    impact_coefficient=float(slip_cfg.get("impact_coefficient", 0.1)),
    max_slippage=float(slip_cfg.get("max_slippage", 0.02)),
)

EXPECTED = {
    "pf_min":   1.22,
    "pf_max":   1.32,
    "wr_min":   0.57,
    "wr_max":   0.62,
    "cagr_min": 0.06,
    "cagr_max": 0.09,
}


def main():
    SEP = "=" * 65
    print(SEP)
    print("v2.7 파라미터 검증 백테스트")
    print(f"  ADX={PARAMS.adx_threshold}  RS={PARAMS.relative_strength_threshold}")
    print(f"  SL={PARAMS.stop_loss_atr}  TP={PARAMS.take_profit_atr}  Trail={PARAMS.trailing_atr}")
    print(f"  MaxPos={MAX_POS}  RankRS={WEIGHTS.rs}")
    print(f"  Capital={CAPITAL:,}원  MinAmount={MIN_AMOUNT:,}원")
    total_w = WEIGHTS.rs + WEIGHTS.momentum_atr + WEIGHTS.adx + WEIGHTS.liquidity + WEIGHTS.ma_alignment
    print(f"  ranking_weights 합: {total_w:.2f}")
    assert abs(total_w - 1.0) < 1e-9, f"ranking_weights 합 오류: {total_w}"
    print(SEP)

    t0 = time.time()
    print("\n[1/3] 데이터 로드...")
    preloaded = load_backtest_data(PARAMS)
    print(f"  완료: {time.time()-t0:.1f}s")

    t0 = time.time()
    print("[2/3] 신호 사전 계산...")
    precomp = precompute_daily_signals(
        preloaded["trading_dates"],
        preloaded["ticker_data"],
        preloaded["ticker_date_idx"],
        set(preloaded["initial_universe"]),
        params=PARAMS,
        kospi_ret_map=preloaded.get("kospi_ret_map"),
        kosdaq_ret_map=preloaded.get("kosdaq_ret_map"),
        ticker_market=preloaded.get("ticker_market"),
        weights=WEIGHTS,
    )
    print(f"  완료: {time.time()-t0:.1f}s")

    t0 = time.time()
    print("[3/3] 백테스트 실행...")
    r = run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=PARAMS,
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
    print(f"  완료: {time.time()-t0:.1f}s")

    pf_s = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    pnl  = r.final_capital - r.initial_capital

    print(f"\n{SEP}")
    print("결과")
    print(SEP)
    print(f"  기간:   {r.period}")
    print(f"  건수:   {r.total_trades}")
    print(f"  WR:     {r.win_rate*100:.1f}%")
    print(f"  PF:     {pf_s}")
    print(f"  CAGR:   {r.cagr_pct*100:+.1f}%")
    print(f"  MDD:    -{r.max_drawdown_pct*100:.1f}%")
    print(f"  순손익: {pnl:+,.0f}원")
    print()
    print("  최적화 실험 기대값 비교:")
    print(f"    PF   {r.profit_factor:.2f}  (기대 ~1.27, 허용 {EXPECTED['pf_min']}~{EXPECTED['pf_max']})")
    print(f"    WR   {r.win_rate*100:.1f}%  (기대 ~59.5%, 허용 {EXPECTED['wr_min']*100:.0f}~{EXPECTED['wr_max']*100:.0f}%)")
    print(f"    CAGR {r.cagr_pct*100:+.1f}%  (기대 ~7.3%, 허용 {EXPECTED['cagr_min']*100:.0f}~{EXPECTED['cagr_max']*100:.0f}%)")

    ok_pf   = EXPECTED["pf_min"]   <= r.profit_factor    <= EXPECTED["pf_max"]
    ok_wr   = EXPECTED["wr_min"]   <= r.win_rate          <= EXPECTED["wr_max"]
    ok_cagr = EXPECTED["cagr_min"] <= r.cagr_pct          <= EXPECTED["cagr_max"]

    print()
    print(f"  PF   허용범위: {'✅' if ok_pf   else '❌'}")
    print(f"  WR   허용범위: {'✅' if ok_wr   else '❌'}")
    print(f"  CAGR 허용범위: {'✅' if ok_cagr else '❌'}")

    if ok_pf and ok_wr and ok_cagr:
        print(f"\n  ✅ config.yaml ↔ 코드 일관성 검증 통과")
    else:
        print(f"\n  ❌ 검증 실패 — config.yaml 파라미터와 실험 결과 불일치")
        print(f"     최적화 실험에서 사용한 파라미터와 config.yaml을 비교하세요.")

    print(SEP)


if __name__ == "__main__":
    main()

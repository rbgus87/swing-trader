"""최종 확정 조합 백테스트 (A+B1+B2+B6).

config.yaml 수정 후 확정 설정으로 단일 실행.

실행:
    python experiments/run_final_config.py
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

CAPITAL    = 10_000_000
MAX_POS    = 6
MIN_AMOUNT = 300_000

# v2.6 파라미터 (TP1 10%, TP2 10%, trailing 80%)
BASE_PARAMS = StrategyParams(tp1_sell_ratio=0.10, tp2_atr=4.0, tp2_sell_ratio=0.10)

# 확정 조합 설정
COST        = CostModel()                          # 현행 0.20%
WEIGHTS     = RankingWeights()                     # B1 복합 랭킹
SECTOR      = SectorConstraint(enabled=False)      # B3 비활성
DYNHOLD     = DynamicHoldParams(enabled=False)     # B4 비활성
SCALING     = ScalingParams(enabled=False)         # B5 비활성
SLIP        = SlippageParams(enabled=True,         # B6 동적 슬리피지
                              base_slippage=0.0003,
                              impact_coefficient=0.1,
                              max_slippage=0.02)


def main():
    out_path = ROOT / "experiments" / "results_final_config.txt"
    SEP = "=" * 70

    print(SEP)
    print("최종 확정 조합 백테스트 (A+B1+B2+B6)")
    print("설정: B1(복합 랭킹) + B2(이중 국면) + B6(동적 슬리피지)")
    print("비활성: B3(섹터) / B4(동적보유) / B5(분할매수)")
    print(SEP)

    print("\n[1/3] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    print(f"  완료: {time.time() - t0:.1f}s")

    print("\n[2/3] 신호 사전 계산 (복합 랭킹)...")
    t0 = time.time()
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
    )
    print(f"  완료: {time.time() - t0:.1f}s")

    print("\n[3/3] 백테스트 실행...")
    t0 = time.time()
    r = run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=BASE_PARAMS,
        cost=COST,
        min_position_amount=MIN_AMOUNT,
        preloaded_data=preloaded,
        precomputed=precomp,
        sizing_mode="equity",
        breadth_gate_threshold=0.40,
        regime_gate_enabled=True,          # B2 이중 국면
        sector_constraint=SECTOR,          # B3 OFF
        dynamic_hold=DYNHOLD,              # B4 OFF
        scaling=SCALING,                   # B5 OFF
        slippage_params=SLIP,              # B6 ON
    )
    elapsed = time.time() - t0
    print(f"  완료: {elapsed:.1f}s")

    pnl   = r.final_capital - r.initial_capital
    ratio = abs(r.cagr_pct / r.max_drawdown_pct) if r.max_drawdown_pct > 0 else 0.0
    pf_s  = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"

    lines = [
        "",
        SEP,
        "최종 확정 조합 백테스트 결과",
        "조합: A+B1(복합 랭킹)+B2(이중 국면)+B6(동적 슬리피지)",
        SEP,
        f"Period: {r.period}",
        f"Capital: {CAPITAL:,}원  /  Max positions: {MAX_POS}",
        "",
        f"  거래 건수:  {r.total_trades}건",
        f"  승률:       {r.win_rate * 100:.1f}%",
        f"  PF:         {pf_s}",
        f"  CAGR:       {r.cagr_pct * 100:+.1f}%",
        f"  MDD:        -{r.max_drawdown_pct * 100:.1f}%",
        f"  순손익:     {pnl:+,.0f}원",
        f"  CAGR/MDD:   {ratio:.2f}",
        f"  최종 자본:  {r.final_capital:,.0f}원",
        "",
        "--- 참고: 이전 실험 [3] A+B1+B2_REGIME (B6 없음) ---",
        "  건수=910  WR=54.4%  PF=1.23  CAGR=+6.5%  MDD=-34.1%",
        "",
        SEP,
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        for line in lines:
            print(line)
            f.write(line + "\n")

    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()

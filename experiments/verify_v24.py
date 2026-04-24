"""v2.4 검증 — 통합된 precompute_daily_signals가 실험 11 결과를 재현하는지 확인.

기대값 (2026-04-24 기준):
  PF 1.94 / CAGR 12.1% / MDD 23.8% / trades 560 / net +15,461,951
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from loguru import logger

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v2 import StrategyParams


def main():
    t0 = time.time()
    params = StrategyParams()

    logger.info("데이터 로드")
    pre = load_backtest_data(params)

    # v2.4 통합 경로: kosdaq_ret_map + ticker_market 함께 전달
    logger.info("precompute v2.4 (시장별)")
    t1 = time.time()
    pc = precompute_daily_signals(
        pre['trading_dates'], pre['ticker_data'], pre['ticker_date_idx'],
        pre['initial_universe'], params,
        kospi_ret_map=pre['kospi_ret_map'],
        kosdaq_ret_map=pre['kosdaq_ret_map'],
        ticker_market=pre['ticker_market'],
    )
    logger.info(f"  precompute {time.time()-t1:.1f}s")

    logger.info("백테스트 실행")
    r = run_portfolio_backtest(
        initial_capital=5_000_000, max_positions=4, params=params,
        preloaded_data=pre, precomputed=pc, risk=None,
    )
    net = r.final_capital - r.initial_capital
    total_time = time.time() - t0

    print("\n" + "=" * 80)
    print("v2.4 재현 검증 결과")
    print("=" * 80)
    print(f"Period      : {r.period}")
    print(f"Trades      : {r.total_trades}")
    print(f"WR          : {r.win_rate:.1%}")
    print(f"PF          : {r.profit_factor:.2f}")
    print(f"CAGR        : {r.cagr_pct:.1%}")
    print(f"MDD         : {r.max_drawdown_pct:.1%}")
    print(f"Net         : {net:+,.0f}")
    print(f"소요        : {total_time:.1f}s")

    # 재현 판정 (허용 오차 ±0.01 PF, ±1 trade)
    expected_pf = 1.94
    expected_trades = 560
    pf_ok = abs(r.profit_factor - expected_pf) <= 0.01
    trades_ok = abs(r.total_trades - expected_trades) <= 1

    print()
    print(f"기대 PF={expected_pf} → 실측 PF={r.profit_factor:.2f}  [{'✅' if pf_ok else '❌'}]")
    print(f"기대 trades={expected_trades} → 실측 {r.total_trades}  [{'✅' if trades_ok else '❌'}]")

    if pf_ok and trades_ok:
        print("\n✅ v2.4 재현 성공")
    else:
        print("\n❌ v2.4 재현 실패 — 실험 11 결과와 불일치")


if __name__ == "__main__":
    main()

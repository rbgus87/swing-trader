"""전략 비교 백테스트.

4개 등록된 전략(golden_cross, macd_rsi, bb_bounce, breakout)을
동일 조건에서 실행하여 성과를 비교합니다.

Usage:
    python -m src.backtest.strategy_compare
"""

import sys
from datetime import datetime

import numpy as np
from loguru import logger

from src.backtest.engine import BacktestEngine
from src.backtest.report import BacktestReporter
from src.strategy import available_strategies

# watchlist 20종목 (config.yaml과 동일)
CODES = [
    "005930", "000660", "005380", "000270", "068270",
    "035420", "035720", "105560", "055550", "066570",
    "006400", "003670", "012330", "028260", "096770",
    "003550", "034730", "032830", "030200", "017670",
]

START_DATE = "20230101"
END_DATE = "20250314"


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    engine = BacktestEngine(initial_capital=3_000_000)
    reporter = BacktestReporter()

    strategies = available_strategies()
    logger.info(f"등록된 전략: {strategies}")

    results = {}

    for strategy_name in strategies:
        print(f"\n{'='*60}")
        print(f"  {strategy_name}")
        print(f"{'='*60}")

        result = engine.run(
            CODES, START_DATE, END_DATE, strategy_name=strategy_name
        )
        results[strategy_name] = result
        reporter.print_summary(result)

        # HTML 리포트 생성
        report_path = reporter.generate_html(
            result,
            output_path=f"reports/strategy_{strategy_name}.html",
            equity=engine._last_equity,
            trades=engine._last_trades,
        )
        print(f"  리포트: {report_path}")

    # === 비교 테이블 출력 ===
    print(f"\n\n{'='*90}")
    print(f"  전략 비교 종합표 (watchlist 20종목, {START_DATE}~{END_DATE})")
    print(f"{'='*90}")

    header = (
        f"{'전략':<18} {'수익률':>8} {'연환산':>8} {'MDD':>8} "
        f"{'Sharpe':>8} {'승률':>8} {'손익비':>8} {'거래수':>6} {'보유일':>6}"
    )
    print(header)
    print("-" * len(header))

    for name, r in results.items():
        pf_str = (
            f"{r.profit_factor:.2f}"
            if r.profit_factor != float("inf")
            else "inf"
        )
        print(
            f"{name:<18} {r.total_return:>7.2f}% {r.annual_return:>7.2f}% "
            f"{r.max_drawdown:>7.2f}% {r.sharpe_ratio:>8.2f} "
            f"{r.win_rate:>7.2f}% {pf_str:>8} {r.trade_count:>6d} "
            f"{r.avg_hold_days:>5.1f}"
        )

    print("-" * len(header))

    # 최고 전략 표시
    if results:
        best_return = max(results.items(), key=lambda x: x[1].total_return)
        best_sharpe = max(results.items(), key=lambda x: x[1].sharpe_ratio)
        best_winrate = max(results.items(), key=lambda x: x[1].win_rate)
        print(f"\n  최고 수익률: {best_return[0]} ({best_return[1].total_return:.2f}%)")
        print(f"  최고 Sharpe: {best_sharpe[0]} ({best_sharpe[1].sharpe_ratio:.2f})")
        print(f"  최고 승률:   {best_winrate[0]} ({best_winrate[1].win_rate:.2f}%)")
    print()


if __name__ == "__main__":
    main()

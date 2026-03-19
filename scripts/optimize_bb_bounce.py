"""bb_bounce 전략 파라미터 최적화.

핵심 리스크/수익 파라미터에 집중한 그리드 서치.
Usage: python scripts/optimize_bb_bounce.py
"""

import sys
import itertools
from datetime import datetime

import numpy as np
import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestEngine

# watchlist 20종목
CODES = [
    "005930", "000660", "005380", "000270", "068270",
    "035420", "035720", "105560", "055550", "066570",
    "006400", "003670", "012330", "028260", "096770",
    "003550", "034730", "032830", "030200", "017670",
]

START_DATE = "20230101"
END_DATE = "20250314"
STRATEGY = "bb_bounce"

# 핵심 파라미터 그리드 (리스크/수익 관련만)
PARAM_GRID = {
    "stop_atr_mult": [1.5, 2.0, 2.5],
    "max_stop_pct": [0.07, 0.10, 0.12],
    "target_return": [0.06, 0.08, 0.10, 0.12],
    "max_hold_days": [7, 10, 15],
    "trailing_atr_mult": [2.0, 2.5, 3.0],
    "trailing_activate_pct": [0.03, 0.05, 0.07],
}


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    engine = BacktestEngine(initial_capital=3_000_000)

    # 조합 생성
    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = [dict(zip(keys, combo)) for combo in itertools.product(*values)]
    total = len(combos)

    logger.info(f"bb_bounce 최적화 시작: {total}개 조합")

    # 데이터를 한 번만 로드
    price_data = engine.load_price_data(CODES, START_DATE, END_DATE)
    if not price_data:
        logger.error("데이터 로드 실패")
        return

    results = []
    for i, params in enumerate(combos):
        try:
            result = engine.run(
                CODES, START_DATE, END_DATE,
                params=params,
                strategy_name=STRATEGY,
            )

            row = {**params}
            row["total_return"] = result.total_return
            row["annual_return"] = result.annual_return
            row["max_drawdown"] = result.max_drawdown
            row["sharpe_ratio"] = result.sharpe_ratio
            row["win_rate"] = result.win_rate
            row["profit_factor"] = result.profit_factor
            row["trade_count"] = result.trade_count
            row["avg_hold_days"] = result.avg_hold_days
            results.append(row)

            if (i + 1) % 50 == 0:
                logger.info(f"진행: {i + 1}/{total}")
        except Exception as e:
            logger.warning(f"조합 {i + 1} 실패: {e}")

    if not results:
        print("유효한 결과 없음")
        return

    df = pd.DataFrame(results)

    # 정렬: Sharpe 기준
    df = df.sort_values("sharpe_ratio", ascending=False).reset_index(drop=True)

    # 상위 20개 출력
    print(f"\n{'='*100}")
    print(f"  bb_bounce 최적화 결과 (상위 20개, {total}개 조합 중)")
    print(f"{'='*100}")

    cols = [
        "stop_atr_mult", "max_stop_pct", "target_return",
        "max_hold_days", "trailing_atr_mult", "trailing_activate_pct",
        "total_return", "annual_return", "max_drawdown",
        "sharpe_ratio", "win_rate", "profit_factor", "trade_count",
    ]

    print(df[cols].head(20).to_string())

    # 현재 값과 비교
    print(f"\n{'='*60}")
    print("  현재 설정 vs 최적 설정 비교")
    print(f"{'='*60}")

    best = df.iloc[0]
    current = {
        "stop_atr_mult": 2.5, "max_stop_pct": 0.10,
        "target_return": 0.12, "max_hold_days": 15,
        "trailing_atr_mult": 2.5, "trailing_activate_pct": 0.07,
    }

    print(f"{'파라미터':<25} {'현재':>10} {'최적':>10}")
    print("-" * 50)
    for k in current:
        print(f"{k:<25} {current[k]:>10} {best[k]:>10}")

    print(f"\n{'성과 지표':<25} {'현재':>10} {'최적':>10}")
    print("-" * 50)
    # 현재 bb_bounce 결과 (기본 파라미터)
    current_perf = {"total_return": 2.25, "annual_return": 1.25,
                    "max_drawdown": -25.20, "sharpe_ratio": 0.15,
                    "win_rate": 61.23, "trade_count": 108}
    for k in current_perf:
        curr_val = current_perf[k]
        best_val = best[k]
        fmt = ".2f" if isinstance(curr_val, float) else "d"
        print(f"{k:<25} {curr_val:>10{fmt}} {best_val:>10{fmt}}")

    # CSV 저장
    csv_path = f"reports/optimize_bb_bounce_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n전체 결과 CSV: {csv_path}")


if __name__ == "__main__":
    main()

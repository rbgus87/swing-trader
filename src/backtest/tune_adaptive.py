"""Adaptive 전략 ([GC+MP]/BB) 파라미터 튜닝.

성과 목표:
- 승률 50-55%, 손익비 1:1.5, 월 수익률 +1.5-3%
- MDD -15~20%, Sharpe 1.0-1.3, PF 1.3-1.5
- 월 거래 횟수 8-12회

Usage:
    python -m src.backtest.tune_adaptive
"""

import itertools
import random
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestEngine

# 20종목 watchlist
CODES = [
    "005930", "000660", "005380", "000270", "068270",
    "035420", "035720", "105560", "055550", "066570",
    "006400", "003670", "012330", "028260", "096770",
    "003550", "034730", "032830", "030200", "017670",
]

START_DATE = "20230101"
END_DATE = "20250314"

# 테스트 기간: ~26개월
TOTAL_MONTHS = 26

# 튜닝 대상 파라미터 그리드
PARAM_GRID = {
    # 진입 조건 완화 (거래 빈도 증가 핵심)
    "adx_threshold": [15, 18, 20],
    "rsi_entry_min": [30, 35, 40],
    "rsi_pullback": [40, 45, 50],        # macd_pullback용
    "rsi_oversold": [35, 40, 45],        # bb_bounce용
    "bb_touch_pct": [0.10, 0.15, 0.20],  # bb_bounce용
    "volume_multiplier": [0.8, 1.0],
    "screening_lookback": [3, 5],        # golden_cross용

    # 청산/리스크 (수익성 조절)
    "target_return": [0.04, 0.06, 0.08],
    "stop_atr_mult": [2.0, 2.5],
    "max_hold_days": [7, 10, 15],
    "max_stop_pct": [0.05, 0.07],
    "trailing_activate_pct": [0.04, 0.06],
    "trailing_atr_mult": [1.5, 2.0],

    # 포트폴리오
    "max_positions": [3, 5, 7],
}


def generate_combos(grid: dict, n_samples: int = 200) -> list[dict]:
    """파라미터 조합 생성 (랜덤 샘플링)."""
    keys = list(grid.keys())
    values = list(grid.values())
    all_combos = [dict(zip(keys, combo)) for combo in itertools.product(*values)]
    total = len(all_combos)

    if total <= n_samples:
        logger.info(f"전수 검사: {total}개 조합")
        return all_combos

    sampled = random.sample(all_combos, n_samples)
    logger.info(f"랜덤 샘플링: {n_samples}/{total}개 조합")
    return sampled


def evaluate(engine: BacktestEngine, params: dict, context: dict) -> dict | None:
    """단일 파라미터 조합 평가."""
    max_pos = params.pop("max_positions", 5)

    # adaptive regime 설정 고정
    params["regime_strategy"] = {
        "trending": ["golden_cross", "macd_pullback"],
        "sideways": "bb_bounce",
    }

    try:
        result = engine.run_portfolio(
            CODES, START_DATE, END_DATE, params,
            strategy_name="adaptive",
            max_positions=max_pos,
            use_market_filter=True,
            _context=context,
        )
    except Exception as e:
        logger.warning(f"실패: {e}")
        params["max_positions"] = max_pos
        return None

    params["max_positions"] = max_pos

    # 월간 거래 횟수 계산
    monthly_trades = result.trade_count / TOTAL_MONTHS if TOTAL_MONTHS > 0 else 0

    row = {**params}
    row["total_return"] = result.total_return
    row["annual_return"] = result.annual_return
    row["max_drawdown"] = result.max_drawdown
    row["sharpe_ratio"] = result.sharpe_ratio
    row["sortino_ratio"] = result.sortino_ratio
    row["win_rate"] = result.win_rate
    row["profit_factor"] = result.profit_factor
    row["avg_trade_return"] = result.avg_trade_return
    row["trade_count"] = result.trade_count
    row["avg_hold_days"] = result.avg_hold_days
    row["monthly_trades"] = round(monthly_trades, 1)
    row["monthly_return"] = round(result.total_return / TOTAL_MONTHS, 2)

    return row


def score_result(row: dict) -> float:
    """다목적 스코어링 — 목표값 달성도 기반.

    목표:
    - Sharpe 1.0+ (가중치 30%)
    - 월 거래 8-12회 (가중치 25%)
    - 승률 50-55% (가중치 15%)
    - MDD > -20% (가중치 15%)
    - PF 1.3+ (가중치 15%)
    """
    score = 0.0

    # Sharpe (30%) — 1.0 이상이면 만점, 0이면 0점
    sharpe = row["sharpe_ratio"]
    score += 0.30 * min(sharpe / 1.0, 1.5)

    # 월 거래 (25%) — 8-12회 최적, 5 미만 페널티
    mt = row["monthly_trades"]
    if 8 <= mt <= 12:
        score += 0.25
    elif 5 <= mt < 8:
        score += 0.25 * (mt - 5) / 3
    elif 12 < mt <= 20:
        score += 0.25 * (1 - (mt - 12) / 8)
    elif mt < 5:
        score += 0.25 * mt / 5 * 0.5  # 5 미만은 크게 감점

    # 승률 (15%) — 45% 이상이면 가점
    wr = row["win_rate"]
    if wr >= 50:
        score += 0.15
    elif wr >= 45:
        score += 0.15 * (wr - 45) / 5
    elif wr >= 40:
        score += 0.15 * (wr - 40) / 10

    # MDD (15%) — -20% 이내
    mdd = row["max_drawdown"]
    if mdd >= -15:
        score += 0.15
    elif mdd >= -20:
        score += 0.15 * (mdd + 20) / 5
    elif mdd >= -25:
        score += 0.15 * (mdd + 25) / 10

    # PF (15%) — 1.3 이상
    pf = row["profit_factor"]
    if pf == float("inf"):
        pf = 3.0
    if pf >= 1.5:
        score += 0.15
    elif pf >= 1.3:
        score += 0.15 * (pf - 1.3) / 0.2
    elif pf >= 1.0:
        score += 0.15 * (pf - 1.0) / 0.6

    return round(score, 4)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    logger.info("=" * 60)
    logger.info("Adaptive 전략 파라미터 튜닝 시작")
    logger.info(f"전략: [golden_cross + macd_pullback] / bb_bounce")
    logger.info(f"기간: {START_DATE} ~ {END_DATE} ({TOTAL_MONTHS}개월)")
    logger.info("=" * 60)

    engine = BacktestEngine(initial_capital=3_000_000)

    # 데이터 프리로드 (1회)
    logger.info("데이터/지표 프리컴퓨팅 중...")
    context = engine.prepare_portfolio_context(
        CODES, START_DATE, END_DATE, "adaptive", use_market_filter=True,
    )
    if context is None:
        logger.error("데이터 로드 실패")
        return

    # 파라미터 조합 생성
    combos = generate_combos(PARAM_GRID, n_samples=200)

    # 평가
    results = []
    for i, params in enumerate(combos):
        row = evaluate(engine, params.copy(), context)
        if row is not None:
            row["score"] = score_result(row)
            results.append(row)

        if (i + 1) % 20 == 0:
            logger.info(f"진행: {i + 1}/{len(combos)}")

    if not results:
        logger.error("유효한 결과 없음")
        return

    df = pd.DataFrame(results).sort_values("score", ascending=False).reset_index(drop=True)

    # Top 20 출력
    print(f"\n{'='*100}")
    print(f"  Top 20 파라미터 조합 (스코어 순)")
    print(f"{'='*100}")

    display_cols = [
        "score", "sharpe_ratio", "total_return", "annual_return",
        "max_drawdown", "win_rate", "profit_factor",
        "trade_count", "monthly_trades", "avg_hold_days",
        "adx_threshold", "rsi_entry_min", "rsi_pullback", "rsi_oversold",
        "bb_touch_pct", "volume_multiplier", "screening_lookback",
        "target_return", "stop_atr_mult", "max_hold_days",
        "max_stop_pct", "trailing_activate_pct", "trailing_atr_mult",
        "max_positions",
    ]
    existing_cols = [c for c in display_cols if c in df.columns]

    top20 = df.head(20)[existing_cols]
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda x: f"{x:.3f}")
    print(top20.to_string())

    # 목표 달성 여부 체크
    print(f"\n{'='*100}")
    print("  목표 달성 분석 (Top 1)")
    print(f"{'='*100}")
    best = df.iloc[0]
    targets = {
        "승률 50-55%": 50 <= best["win_rate"] <= 55,
        "Sharpe 1.0+": best["sharpe_ratio"] >= 1.0,
        "MDD -20% 이내": best["max_drawdown"] >= -20,
        "PF 1.3+": best["profit_factor"] >= 1.3,
        "월 거래 8-12회": 8 <= best["monthly_trades"] <= 12,
        "월 수익률 +1.5%+": best.get("monthly_return", 0) >= 1.5,
    }
    for name, passed in targets.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")

    # 최적 config.yaml 값 출력
    print(f"\n{'='*100}")
    print("  config.yaml 적용 권장값 (Top 1)")
    print(f"{'='*100}")
    param_keys = [
        "adx_threshold", "rsi_entry_min", "rsi_pullback", "rsi_oversold",
        "bb_touch_pct", "volume_multiplier", "screening_lookback",
        "target_return", "stop_atr_mult", "max_hold_days",
        "max_stop_pct", "trailing_activate_pct", "trailing_atr_mult",
        "max_positions",
    ]
    for k in param_keys:
        if k in best:
            print(f"  {k}: {best[k]}")

    # CSV 저장
    output_path = "reports/tune_adaptive_results.csv"
    try:
        import os
        os.makedirs("reports", exist_ok=True)
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"\n  전체 결과 저장: {output_path}")
    except Exception as e:
        logger.warning(f"CSV 저장 실패: {e}")


if __name__ == "__main__":
    main()

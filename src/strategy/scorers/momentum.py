"""모멘텀 점수 스코어러.

입력: daily_df (종목 일봉), market_df (시장(KOSPI/KOSDAQ) 일봉)
출력: 0~100 float
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategy.scorers import normalize_score, weighted_average

# 서브 점수 가중치 (합 = 1.0)
_W_PRICE_MOMENTUM = 0.40
_W_RELATIVE_STRENGTH = 0.35
_W_ACCELERATION = 0.25


def _pct_return(series: pd.Series, days: int) -> float | None:
    """n일 수익률 계산. 데이터 부족 시 None."""
    if len(series) <= days:
        return None
    start = series.iloc[-(days + 1)]
    end = series.iloc[-1]
    if pd.isna(start) or pd.isna(end) or start == 0:
        return None
    return float((end - start) / start * 100.0)


def _score_price_momentum(df: pd.DataFrame) -> float:
    """가격 모멘텀 점수 (0~100): 20/60/120일 수익률 가중 평균.

    20d: 40%, 60d: 35%, 120d: 25%
    """
    if df is None or len(df) < 20:
        return 50.0

    close = df["close"].astype(float)

    returns = []
    weights_inner = []

    r20 = _pct_return(close, 20)
    r60 = _pct_return(close, 60)
    r120 = _pct_return(close, 120)

    if r20 is not None:
        returns.append(normalize_score(r20, -20.0, 30.0))
        weights_inner.append(0.40)
    if r60 is not None:
        returns.append(normalize_score(r60, -25.0, 40.0))
        weights_inner.append(0.35)
    if r120 is not None:
        returns.append(normalize_score(r120, -30.0, 50.0))
        weights_inner.append(0.25)

    if not returns:
        return 50.0

    return weighted_average(returns, weights_inner)


def _score_relative_strength(
    df: pd.DataFrame, market_df: pd.DataFrame | None
) -> float:
    """상대 강도 점수 (0~100): 종목 60일 수익률 - 시장 60일 수익률.

    market_df 없으면 절대 수익률만 사용.
    """
    if df is None or len(df) < 60:
        return 50.0

    close = df["close"].astype(float)
    stock_r60 = _pct_return(close, 60)

    if stock_r60 is None:
        return 50.0

    if market_df is None or len(market_df) < 60:
        # 시장 데이터 없으면 절대 수익률 기준
        return normalize_score(stock_r60, -25.0, 40.0)

    mkt_close = market_df["close"].astype(float)
    mkt_r60 = _pct_return(mkt_close, 60)

    if mkt_r60 is None:
        return normalize_score(stock_r60, -25.0, 40.0)

    # 초과 수익률 (종목 - 시장)
    excess = stock_r60 - mkt_r60
    # -20% 초과수익 → 0점, +20% 초과수익 → 100점
    return normalize_score(excess, -20.0, 20.0)


def _score_acceleration(df: pd.DataFrame) -> float:
    """모멘텀 가속도 점수 (0~100): 최근 20일 vs 이전 20일 수익률 비교.

    최근 20일 > 이전 20일이면 가속 중.
    """
    if df is None or len(df) < 40:
        return 50.0

    close = df["close"].astype(float)

    # 최근 20일 수익률
    recent_start = close.iloc[-21]
    recent_end = close.iloc[-1]

    # 이전 20일 수익률 (40일 전 ~ 20일 전)
    prev_start = close.iloc[-41] if len(close) >= 41 else close.iloc[0]
    prev_end = close.iloc[-21]

    if any(pd.isna([recent_start, recent_end, prev_start, prev_end])):
        return 50.0
    if recent_start == 0 or prev_start == 0:
        return 50.0

    recent_r = (recent_end - recent_start) / recent_start * 100.0
    prev_r = (prev_end - prev_start) / prev_start * 100.0

    # 가속도 = 최근 - 이전
    acceleration = recent_r - prev_r

    # -15%p 이하 감속 → 0점, +15%p 이상 가속 → 100점
    return normalize_score(acceleration, -15.0, 15.0)


def compute_momentum_score(
    daily_df: pd.DataFrame,
    market_df: pd.DataFrame | None = None,
) -> float:
    """모멘텀 종합 점수 계산 (0~100).

    Args:
        daily_df: 종목 일봉 데이터. 필수 컬럼: close
        market_df: 시장(KOSPI/KOSDAQ) 일봉 데이터. 선택. 컬럼: close

    Returns:
        0~100 float (50 = 중립, 데이터 부족 시 50 반환)
    """
    if daily_df is None or len(daily_df) == 0:
        return 50.0

    if "close" not in daily_df.columns:
        return 50.0

    scores = [
        _score_price_momentum(daily_df),
        _score_relative_strength(daily_df, market_df),
        _score_acceleration(daily_df),
    ]
    weights = [_W_PRICE_MOMENTUM, _W_RELATIVE_STRENGTH, _W_ACCELERATION]
    return weighted_average(scores, weights)

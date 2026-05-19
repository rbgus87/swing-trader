"""기술적 점수 스코어러.

입력: daily_df (일봉 DataFrame), weekly_df (주봉 DataFrame, 선택)
출력: 0~100 float
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategy.scorers import normalize_score, weighted_average
from src.strategy.scorers.indicators import (
    adx,
    atr,
    bollinger_bands,
    ema,
    macd,
    rsi,
    sma,
)

# 서브 점수 가중치 (합 = 1.0)
_W_TREND = 0.25
_W_RSI = 0.15
_W_MACD = 0.20
_W_VOLUME = 0.10
_W_BOLLINGER = 0.15
_W_WEEKLY = 0.15


def _score_trend(df: pd.DataFrame) -> float:
    """MA 배열 + ADX 기반 추세 점수 (0~100).

    MA5 > MA20 > MA60: 배열 점수 (0/33/67/100)
    ADX > 25: 추세 강도 보정
    """
    if len(df) < 60:
        return 50.0

    close = df["close"].astype(float)
    ma5 = sma(close, 5).iloc[-1]
    ma20 = sma(close, 20).iloc[-1]
    ma60 = sma(close, 60).iloc[-1]

    if any(pd.isna([ma5, ma20, ma60])):
        return 50.0

    # MA 상승 배열 점수
    bullish = sum([ma5 > ma20, ma20 > ma60, ma5 > ma60])
    ma_score = [0.0, 33.0, 67.0, 100.0][bullish]

    # ADX 추세 강도 반영 (ADX > 25 → 보정 추가)
    try:
        adx_val = adx(df["high"].astype(float), df["low"].astype(float), close).iloc[-1]
    except Exception:
        adx_val = np.nan

    if pd.isna(adx_val):
        return ma_score

    # ADX 강도 보정: 25 미만은 중립(0), 이상은 +10 보정 (상승 배열일 때)
    if adx_val >= 25 and bullish >= 2:
        ma_score = min(100.0, ma_score + 10.0)
    elif adx_val < 20 and bullish <= 1:
        ma_score = max(0.0, ma_score - 10.0)

    return float(ma_score)


def _score_rsi(df: pd.DataFrame) -> float:
    """RSI(14) 구간 기반 점수 (0~100).

    30~70 구간 내 위치 + 과매도/과매수 처리
    """
    if len(df) < 20:
        return 50.0

    close = df["close"].astype(float)
    rsi_series = rsi(close, 14)
    rsi_val = rsi_series.iloc[-1]

    if pd.isna(rsi_val):
        return 50.0

    # 과매도(≤30) → 매수 기회: 높은 점수
    if rsi_val <= 30:
        return normalize_score(rsi_val, 0.0, 30.0) * 0.3 + 70.0
    # 과매수(≥70) → 위험: 낮은 점수
    if rsi_val >= 70:
        return normalize_score(100.0 - rsi_val, 0.0, 30.0) * 0.3
    # 30~70 구간: 50 근방이 최고 (스윙 관점)
    # 40~60이 최적 구간 → 100점, 극단으로 갈수록 감소
    distance_from_center = abs(rsi_val - 55.0)  # 55 기준 (약상승 선호)
    return float(max(0.0, 100.0 - distance_from_center * 2.5))


def _score_macd(df: pd.DataFrame) -> float:
    """MACD 크로스오버/방향 점수 (0~100).

    히스토그램 양전환 + 방향 고려
    """
    if len(df) < 35:
        return 50.0

    close = df["close"].astype(float)
    _, _, hist = macd(close)

    if len(hist.dropna()) < 3:
        return 50.0

    recent = hist.dropna().iloc[-3:]
    curr = recent.iloc[-1]
    prev = recent.iloc[-2]

    if pd.isna(curr) or pd.isna(prev):
        return 50.0

    # 히스토그램 양수 + 증가: 강한 매수 신호
    if curr > 0 and curr > prev:
        return 80.0
    # 히스토그램 양수 + 감소
    if curr > 0 and curr <= prev:
        return 60.0
    # 음→양 전환 (골든크로스)
    if curr > 0 and prev <= 0:
        return 90.0
    # 양→음 전환 (데드크로스)
    if curr <= 0 and prev > 0:
        return 15.0
    # 히스토그램 음수 + 개선(덜 음수)
    if curr < 0 and curr > prev:
        return 40.0
    # 히스토그램 음수 + 악화
    return 20.0


def _score_volume(df: pd.DataFrame) -> float:
    """거래량 추세 점수 (0~100).

    최근 5일 평균 거래량 vs 20일 평균 거래량 + 가격 방향 고려
    """
    if len(df) < 20:
        return 50.0

    volume = df["volume"].astype(float)
    close = df["close"].astype(float)

    vol5 = volume.iloc[-5:].mean()
    vol20 = volume.iloc[-20:].mean()

    if vol20 == 0 or pd.isna(vol20):
        return 50.0

    vol_ratio = vol5 / vol20

    # 가격 방향 (최근 3일)
    price_up = close.iloc[-1] > close.iloc[-4] if len(close) >= 4 else True

    if price_up:
        # 가격 상승 + 거래량 증가: 강세
        if vol_ratio >= 1.5:
            return 90.0
        elif vol_ratio >= 1.2:
            return 75.0
        elif vol_ratio >= 0.8:
            return 60.0
        else:
            return 45.0
    else:
        # 가격 하락 + 거래량 증가: 약세
        if vol_ratio >= 1.5:
            return 15.0
        elif vol_ratio >= 1.2:
            return 25.0
        elif vol_ratio >= 0.8:
            return 40.0
        else:
            return 55.0  # 가격 하락 + 거래량 감소: 하락 소진 가능성


def _score_bollinger(df: pd.DataFrame) -> float:
    """%B (볼린저 밴드 내 위치) 기반 점수 (0~100).

    %B = (Close - Lower) / (Upper - Lower)
    스윙 관점: 0.2~0.8 구간이 선호, 0.2 이하는 반등 기대
    """
    if len(df) < 20:
        return 50.0

    close = df["close"].astype(float)
    upper, _, lower = bollinger_bands(close, 20)

    ub = upper.iloc[-1]
    lb = lower.iloc[-1]
    c = close.iloc[-1]

    if pd.isna(ub) or pd.isna(lb) or (ub - lb) == 0:
        return 50.0

    pct_b = (c - lb) / (ub - lb)

    # %B 기반 점수
    # 0.2 이하: 과매도 구간 → 반등 기대 (높은 점수)
    if pct_b <= 0.2:
        return float(80.0 + (0.2 - pct_b) * 50.0)  # 80~90점
    # 0.8 이상: 과매수 구간 → 상단 저항
    if pct_b >= 0.8:
        return float(max(0.0, 30.0 - (pct_b - 0.8) * 100.0))
    # 0.4~0.6: 중립 구간
    if 0.4 <= pct_b <= 0.6:
        return 65.0
    # 0.2~0.4: 하단 → 중립
    if 0.2 < pct_b < 0.4:
        return float(normalize_score(pct_b, 0.2, 0.4) * 25.0 + 55.0)
    # 0.6~0.8: 중립 → 상단
    return float(max(30.0, 65.0 - (pct_b - 0.6) * 175.0))


def _score_weekly_trend(weekly_df: pd.DataFrame | None) -> float:
    """주봉 MA 배열 점수 (MTF 확인용). weekly_df 없으면 50 반환."""
    if weekly_df is None or len(weekly_df) < 10:
        return 50.0

    close = weekly_df["close"].astype(float)
    ma5w = sma(close, 5).iloc[-1]
    ma10w = sma(close, 10).iloc[-1]

    if pd.isna(ma5w) or pd.isna(ma10w):
        return 50.0

    curr_close = close.iloc[-1]

    # 주봉: 종가 > MA5W > MA10W → 강한 상승 추세
    if curr_close > ma5w > ma10w:
        return 85.0
    if curr_close > ma5w:
        return 65.0
    if ma5w > ma10w:
        return 55.0
    if curr_close < ma5w < ma10w:
        return 20.0
    return 40.0


def compute_technical_score(
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame | None = None,
) -> float:
    """기술적 종합 점수 계산 (0~100).

    Args:
        daily_df: 일봉 데이터. 필수 컬럼: open, high, low, close, volume
        weekly_df: 주봉 데이터. 선택. 컬럼: close (최소 10행 필요)

    Returns:
        0~100 float (50 = 중립, 데이터 부족 시 50 반환)
    """
    if daily_df is None or len(daily_df) == 0:
        return 50.0

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(daily_df.columns):
        return 50.0

    scores = [
        _score_trend(daily_df),
        _score_rsi(daily_df),
        _score_macd(daily_df),
        _score_volume(daily_df),
        _score_bollinger(daily_df),
        _score_weekly_trend(weekly_df),
    ]
    weights = [_W_TREND, _W_RSI, _W_MACD, _W_VOLUME, _W_BOLLINGER, _W_WEEKLY]
    return weighted_average(scores, weights)

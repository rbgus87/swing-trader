"""순수 pandas/numpy 기술적 지표 계산 모듈. TA-Lib 의존 없음."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """단순 이동평균."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """지수 이동평균."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI (Wilder 평균 방식).

    Returns 0~100 범위 Series. 데이터 부족 시 NaN.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    # Wilder smoothing — 첫 번째 평균은 단순 평균, 이후 EWM
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss가 0인 경우(연속 상승) → RSI = 100
    return rsi_val.where(avg_loss > 0, 100.0)


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD 라인, 시그널, 히스토그램 반환.

    Returns:
        (macd_line, signal_line, histogram)
    """
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _true_range(high: pd.Series, low: pd.Series, prev_close: pd.Series) -> pd.Series:
    hl = high - low
    hc = (high - prev_close).abs()
    lc = (low - prev_close).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = _true_range(high, low, prev_close)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """ADX (Average Directional Index). 0~100 범위."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    tr = _true_range(high, low, prev_close)
    atr_val = tr.ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100.0 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_val.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_val.replace(0, np.nan)

    di_sum = plus_di + minus_di
    dx = (100.0 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan))
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def bollinger_bands(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저 밴드 (상단, 중간, 하단) 반환.

    Returns:
        (upper, middle, lower)
    """
    middle = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower

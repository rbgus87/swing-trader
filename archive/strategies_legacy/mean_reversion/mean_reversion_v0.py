"""MeanReversion v0 — Baseline 평균회귀 전략.

Phase 2 Step 2a. RSI 과매도 + BB 하단 이탈 + 반등 확인.
목표: MA20 복귀. 보유 7일 이내.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from loguru import logger


@dataclass
class MRParams:
    """MeanReversion v0 파라미터."""
    # 지표
    rsi_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    ma_target: int = 20
    ma_floor: int = 60
    atr_period: int = 14

    # 진입
    rsi_entry: float = 40.0       # v0.1: 30 → 40 완화

    # 청산
    stop_loss_atr: float = 1.5
    max_hold_days: int = 7
    rsi_panic: float = 20.0

    # Universe (TrendFollowing과 공유)
    min_trading_value: float = 5e9


def calculate_mr_indicators(df: pd.DataFrame, params: MRParams) -> pd.DataFrame:
    """OHLCV DataFrame에 MeanReversion 지표 추가."""
    df = df.copy()

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(params.rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(params.rsi_period).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))

    df['bb_mid'] = df['close'].rolling(params.bb_period).mean()
    bb_std = df['close'].rolling(params.bb_period).std()
    df['bb_upper'] = df['bb_mid'] + params.bb_std * bb_std
    df['bb_lower'] = df['bb_mid'] - params.bb_std * bb_std

    df['ma60'] = df['close'].rolling(params.ma_floor).mean()
    df['ma20'] = df['close'].rolling(params.ma_target).mean()

    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(params.atr_period).mean()

    df['trading_value'] = df['close'] * df['volume']
    df['avg_trading_value_20'] = df['trading_value'].rolling(20).mean()

    return df

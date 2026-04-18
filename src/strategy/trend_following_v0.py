"""TrendFollowing v0 — Baseline 돌파 전략.

Phase 2 Step 1a. 가장 단순한 형태의 추세 추종.
신고가 돌파 + 정배열 + 거래량 확인.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional
from loguru import logger


@dataclass
class StrategyParams:
    """v0 파라미터."""
    ma_short: int = 5
    ma_mid: int = 20
    ma_long: int = 60
    adx_period: int = 14
    adx_threshold: float = 20.0
    breakout_period: int = 60          # 신고가 기간
    volume_multiplier: float = 1.5     # 거래량 배수
    atr_period: int = 14
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 2.0
    trailing_atr: float = 4.0
    max_hold_days: int = 20
    tp1_sell_ratio: float = 0.5    # TP1 도달 시 매도할 비율 (0~1, 0이면 TP1 비활성화와 동일)
    min_trading_value: float = 5e9     # 50억원


@dataclass
class Signal:
    """매매 신호."""
    date: str
    ticker: str
    signal_type: str
    price: float
    atr: float
    score: float = 0.0
    reason: str = ''


def calculate_indicators(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """OHLCV DataFrame에 전략 지표 추가."""
    df = df.copy()

    df['ma5'] = df['close'].rolling(params.ma_short).mean()
    df['ma20'] = df['close'].rolling(params.ma_mid).mean()
    df['ma60'] = df['close'].rolling(params.ma_long).mean()

    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(params.atr_period).mean()

    df['adx'] = _calculate_adx(df, params.adx_period)

    df['highest_n'] = df['high'].rolling(params.breakout_period).max()

    df['avg_volume_20'] = df['volume'].rolling(params.ma_mid).mean()

    df['trading_value'] = df['close'] * df['volume']
    df['avg_trading_value_20'] = df['trading_value'].rolling(params.ma_mid).mean()

    return df


def _calculate_adx(df: pd.DataFrame, period: int) -> pd.Series:
    """ADX 계산."""
    high = df['high']
    low = df['low']
    close = df['close']

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10))
    adx = dx.rolling(period).mean()

    return adx


def scan_entry_signals(df: pd.DataFrame, ticker: str, params: StrategyParams) -> list[Signal]:
    """진입 신호 스캔. 전체 기간에서 신호 발생일 리스트 반환."""
    df = calculate_indicators(df, params)
    signals = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]

        if pd.isna(row['ma60']) or pd.isna(row['adx']) or pd.isna(row['atr']):
            continue
        if row['atr'] <= 0:
            continue
        if pd.isna(prev['highest_n']):
            continue

        aligned = row['ma5'] > row['ma20'] > row['ma60']
        trending = row['adx'] >= params.adx_threshold
        liquid = row['avg_trading_value_20'] >= params.min_trading_value

        if not (aligned and trending and liquid):
            continue

        breakout = row['close'] > prev['highest_n']
        vol_confirm = row['volume'] > row['avg_volume_20'] * params.volume_multiplier

        if breakout and vol_confirm:
            if 'date' in df.columns:
                d = row['date']
                date_str = d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)[:10]
            else:
                date_str = df.index[i].strftime('%Y-%m-%d')

            signals.append(Signal(
                date=date_str,
                ticker=ticker,
                signal_type='ENTRY',
                price=row['close'],
                atr=row['atr'],
                score=row['adx'],
                reason=f"60d breakout, ADX={row['adx']:.1f}, vol_ratio={row['volume']/row['avg_volume_20']:.1f}x"
            ))

    return signals

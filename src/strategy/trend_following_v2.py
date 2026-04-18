"""TrendFollowing v2.2 — 상태 기반 추세추종.

진입: "60일 신고가 돌파"(v2.1) → "상태 기반 추세추종"
  완전 정배열(MA20>MA60>MA120) + MA60 기울기(+) + MA60 위치(+5~20%)
  MACD(12,26,9) histogram > 0
  상대강도: 종목 20일 수익률 - KOSPI 20일 수익률 ≥ 5%p
  거래량: 5일 평균 > 20일 평균
  공통 필터: ADX≥20, 거래대금 50억+, ATR/close 2.5~8%

청산: SL ATR×2.0 / TP1 ATR×2.0(50%) / Trail ATR×4.0 / Hold 20 / MA5<MA20 (유지)

12년 백테스트: PF 1.30 / CAGR +6.3% / MDD 24.3% / WR 55% / 634건
비용 전 PF 1.42.
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger


@dataclass
class StrategyParams:
    """v2.2 파라미터."""
    # 이동평균
    ma_short: int = 5       # TP/트레일 이탈 참고용
    ma_mid: int = 20
    ma_mid2: int = 60
    ma_long: int = 120

    # 위치 적정성 (MA60 대비)
    ma60_position_min: float = 0.05
    ma60_position_max: float = 0.20

    # MA60 기울기 측정 윈도우 (거래일)
    ma60_slope_lookback: int = 5

    # MACD
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # 상대강도 (vs KOSPI)
    relative_strength_period: int = 20
    relative_strength_threshold: float = 0.05

    # 거래량 (단기 > 중기)
    vol_short: int = 5
    vol_long: int = 20

    # 공통 필터
    adx_period: int = 14
    adx_threshold: float = 20.0
    atr_period: int = 14
    atr_price_min: float = 0.025
    atr_price_max: float = 0.08
    min_trading_value: float = 5e9

    # 청산 (v2.1 ATR 청산 유지)
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 2.0
    trailing_atr: float = 4.0
    max_hold_days: int = 20
    tp1_sell_ratio: float = 0.5


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


def _calculate_adx(df: pd.DataFrame, period: int) -> pd.Series:
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
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10))
    return dx.rolling(period).mean()


def calculate_indicators(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """OHLCV DataFrame에 v2.2 전략 지표 추가."""
    df = df.copy()

    # 이동평균
    df['ma5'] = df['close'].rolling(params.ma_short).mean()
    df['ma20'] = df['close'].rolling(params.ma_mid).mean()
    df['ma60'] = df['close'].rolling(params.ma_mid2).mean()
    df['ma120'] = df['close'].rolling(params.ma_long).mean()

    # MA60 기울기 (Δ over lookback)
    df['ma60_slope'] = df['ma60'] - df['ma60'].shift(params.ma60_slope_lookback)

    # MA60 대비 위치
    df['ma60_dist'] = df['close'] / df['ma60'] - 1.0

    # ATR
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(params.atr_period).mean()

    # ADX
    df['adx'] = _calculate_adx(df, params.adx_period)

    # MACD (12,26,9) histogram
    ema_fast = df['close'].ewm(span=params.macd_fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=params.macd_slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=params.macd_signal, adjust=False).mean()
    df['macd_hist'] = macd - signal

    # 거래량
    df[f'avg_volume_{params.vol_short}'] = df['volume'].rolling(params.vol_short).mean()
    df[f'avg_volume_{params.vol_long}'] = df['volume'].rolling(params.vol_long).mean()
    df['avg_volume_5'] = df[f'avg_volume_{params.vol_short}']
    df['avg_volume_20'] = df[f'avg_volume_{params.vol_long}']

    # 거래대금
    df['trading_value'] = df['close'] * df['volume']
    df['avg_trading_value_20'] = df['trading_value'].rolling(params.vol_long).mean()

    # 종목 N일 수익률
    df['stock_ret_n'] = df['close'].pct_change(params.relative_strength_period)

    return df


def scan_entry_signals(
    df: pd.DataFrame,
    ticker: str,
    params: StrategyParams,
    kospi_ret_map: Optional[dict] = None,
) -> list[Signal]:
    """상태 기반 추세추종 진입 신호 스캔.

    kospi_ret_map: {pd.Timestamp: float} — KOSPI N일 수익률. 없으면 상대강도 조건 무시.
    """
    df = calculate_indicators(df, params)
    signals = []

    for i in range(params.ma_long, len(df)):
        row = df.iloc[i]

        # 필수값 NaN 체크
        req = ['ma20', 'ma60', 'ma120', 'ma60_slope', 'ma60_dist',
               'atr', 'adx', 'macd_hist', 'avg_volume_5', 'avg_volume_20',
               'avg_trading_value_20', 'stock_ret_n']
        if any(pd.isna(row.get(k)) for k in req):
            continue
        if row['atr'] <= 0 or row['close'] <= 0:
            continue

        # 정배열 + MA120
        if not (row['close'] > row['ma20'] > row['ma60'] > row['ma120']):
            continue
        # MA60 기울기 (+)
        if row['ma60_slope'] <= 0:
            continue
        # MA60 위치
        if not (params.ma60_position_min <= row['ma60_dist'] <= params.ma60_position_max):
            continue
        # MACD hist > 0
        if row['macd_hist'] <= 0:
            continue
        # 거래량
        if row['avg_volume_5'] <= row['avg_volume_20']:
            continue
        # ADX / 거래대금
        if row['adx'] < params.adx_threshold:
            continue
        if row['avg_trading_value_20'] < params.min_trading_value:
            continue
        # ATR 밴드
        atr_ratio = row['atr'] / row['close']
        if not (params.atr_price_min <= atr_ratio <= params.atr_price_max):
            continue
        # 상대강도
        if kospi_ret_map is not None:
            if 'date' in df.columns:
                d = row['date']
                ts = pd.Timestamp(d) if not isinstance(d, pd.Timestamp) else d
            else:
                ts = df.index[i]
            kospi_ret = kospi_ret_map.get(ts)
            if kospi_ret is None or pd.isna(kospi_ret):
                continue
            if (row['stock_ret_n'] - float(kospi_ret)) < params.relative_strength_threshold:
                continue

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
            score=float(row['adx']),
            reason=(f"trend state: MA aligned, ADX={row['adx']:.1f}, "
                    f"MA60_dist={row['ma60_dist']:+.1%}, MACD_hist={row['macd_hist']:+.3f}"),
        ))

    return signals

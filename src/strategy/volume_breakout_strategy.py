"""거래량 돌파 전략.

매수: 거래량 N일 최대 돌파 + 가격 추세(SMA20) + OBV 상승 + RSI 적정 구간
매도: 데드크로스 또는 ATR 손절 또는 거래량 급감
거래량이 가격에 선행한다는 원리를 활용. 기관/세력 매집 시점을 포착.
"""

import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class VolumeBreakoutStrategy(BaseStrategy):
    """거래량 돌파 전략."""

    name = "volume_breakout"
    category = "trend"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: 거래량 N일 최대 돌파 + 추세 확인."""
        vol_lookback = self.params.get("vol_lookback", 20)
        if len(df) < vol_lookback + 1:
            return False

        latest = df.iloc[-1]
        vol_multiplier = self.params.get("vol_breakout_multiplier", 0.8)

        # 1. 거래량 돌파: 당일 거래량 > 직전 N일 최대 거래량 × multiplier
        prev_vol_max = df["volume"].iloc[-(vol_lookback + 1) : -1].max()
        if latest["volume"] <= prev_vol_max * vol_multiplier:
            return False

        # 2. 가격 추세: 종가 > SMA20
        if latest.get("sma20", 0) <= 0:
            return False
        if latest["close"] <= latest["sma20"]:
            return False

        # 3. OBV 상승: OBV > OBV_SMA20 (매집 확인)
        obv = latest.get("obv", 0)
        obv_sma = latest.get("obv_sma20", 0)
        if obv <= obv_sma:
            return False

        # 4. RSI 적정 구간 (과매수 아닌 모멘텀)
        rsi_min = self.params.get("rsi_entry_min", 40)
        rsi_max = self.params.get("rsi_entry_max", 70)
        rsi = latest.get("rsi", 50)
        if not (rsi_min <= rsi <= rsi_max):
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: 거래량 돌파 + 추세 + OBV + RSI + 60분봉 확인."""
        vol_lookback = self.params.get("vol_lookback", 20)
        if len(df_daily) < vol_lookback + 1:
            return False

        latest = df_daily.iloc[-1]
        vol_multiplier = self.params.get("vol_breakout_multiplier", 0.8)

        # 1. 거래량 돌파
        prev_vol_max = df_daily["volume"].iloc[-(vol_lookback + 1) : -1].max()
        if latest["volume"] <= prev_vol_max * vol_multiplier:
            return False

        # 2. 가격 추세: 종가 > SMA20
        if latest.get("sma20", 0) <= 0:
            return False
        if latest["close"] <= latest["sma20"]:
            return False

        # 3. OBV 상승
        obv = latest.get("obv", 0)
        obv_sma = latest.get("obv_sma20", 0)
        if obv <= obv_sma:
            return False

        # 4. RSI 적정 구간
        rsi_min = self.params.get("rsi_entry_min", 40)
        rsi_max = self.params.get("rsi_entry_max", 70)
        rsi = latest.get("rsi", 50)
        if not (rsi_min <= rsi <= rsi_max):
            return False

        # 5. ADX 추세 강도 (거짓 돌파 방지)
        adx_threshold = self.params.get("adx_threshold", 15)
        if latest.get("adx", 0) < adx_threshold:
            return False

        # 6. 60분봉 확인 (선택)
        if df_60m is not None and len(df_60m) >= 5:
            df_60m_ind = calculate_indicators(df_60m)
            if not df_60m_ind.empty:
                last_60m = df_60m_ind.iloc[-1]
                sma5 = last_60m.get("sma5", 0)
                sma20 = last_60m.get("sma20", 0)
                if sma5 > 0 and sma20 > 0 and sma5 <= sma20:
                    return False

        return True

    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트: 거래량 돌파 entry / 데드크로스+거래량감소 exit."""
        p = self.params
        indicator_params = {
            "macd_fast": p.get("macd_fast", 12),
            "macd_slow": p.get("macd_slow", 26),
            "macd_signal": p.get("macd_signal", 9),
            "rsi_period": p.get("rsi_period", 14),
        }

        df_ind = calculate_indicators(df, **indicator_params)

        vol_lookback = p.get("vol_lookback", 20)
        vol_multiplier = p.get("vol_breakout_multiplier", 0.8)
        rsi_min = p.get("rsi_entry_min", 40)
        rsi_max = p.get("rsi_entry_max", 70)
        stop_atr_mult = p.get("stop_atr_mult", 2.0)

        # Entry: 거래량 N일 최대 돌파 + SMA20 위 + OBV 상승 + RSI 적정
        rolling_vol_max = df_ind["volume"].rolling(vol_lookback).max().shift(1)
        cond_vol_break = df_ind["volume"] > rolling_vol_max * vol_multiplier
        cond_trend = df_ind["close"] > df_ind["sma20"]
        cond_obv = df_ind["obv"] > df_ind["obv_sma20"]
        cond_rsi = (df_ind["rsi"] >= rsi_min) & (df_ind["rsi"] <= rsi_max)

        raw_entries = cond_vol_break & cond_trend & cond_obv & cond_rsi

        # Exit: SMA5/20 데드크로스 OR RSI > 75 OR ATR 손절 OR 거래량 급감
        cond_dead_cross = (df_ind["sma5"] < df_ind["sma20"]) & (
            df_ind["sma5"].shift(1) >= df_ind["sma20"].shift(1)
        )
        cond_rsi_high = df_ind["rsi"] > 75
        cond_stop = df_ind["close"] < (
            df_ind["close"].shift(1) - df_ind["atr"] * stop_atr_mult
        )
        cond_vol_dry = df_ind["volume"] < df_ind["volume_sma20"] * 0.5

        raw_exits = cond_dead_cross | cond_rsi_high | cond_stop | cond_vol_dry

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).astype("boolean").fillna(False).astype(bool)
        exits = raw_exits.shift(1).astype("boolean").fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

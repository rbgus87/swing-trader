"""MACD-RSI 스윙 전략.

매수: MACD 히스토그램 양전환 + RSI 40~65 + 거래량 + 60분봉 SMA
매도: MACD 데드크로스, RSI > 70, ATR 손절
"""

import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class MacdRsiStrategy(BaseStrategy):
    """MACD-RSI 스윙 전략."""

    name = "macd_rsi"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: MACD 양전환 + RSI 적정 + 거래량."""
        if len(df) < 2:
            return False
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        rsi_min = self.params.get("rsi_entry_min", 40)
        rsi_max = self.params.get("rsi_entry_max", 65)
        volume_multiplier = self.params.get("volume_multiplier", 1.5)

        # 종가 > SMA20
        if latest["close"] <= latest["sma20"]:
            return False
        # MACD 히스토그램 양전환
        if not (prev["macd_hist"] < 0 and latest["macd_hist"] > 0):
            return False
        # RSI 범위
        if not (rsi_min <= latest["rsi"] <= rsi_max):
            return False
        # 거래량
        if latest["volume"] < latest["volume_sma20"] * volume_multiplier:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: MACD 양전환 + RSI + 거래량 + 60분봉."""
        if len(df_daily) < 2:
            return False
        latest = df_daily.iloc[-1]
        prev = df_daily.iloc[-2]

        rsi_min = self.params.get("rsi_entry_min", 40)
        rsi_max = self.params.get("rsi_entry_max", 65)
        volume_multiplier = self.params.get("volume_multiplier", 1.5)

        # 1. 종가 > SMA20
        if latest["close"] <= latest["sma20"]:
            return False
        # 2. MACD 히스토그램 양전환
        if not (prev["macd_hist"] < 0 and latest["macd_hist"] > 0):
            return False
        # 3. RSI 범위
        if not (rsi_min <= latest["rsi"] <= rsi_max):
            return False
        # 4. 거래량
        if latest["volume"] < latest["volume_sma20"] * volume_multiplier:
            return False

        # 5. 60분봉 SMA5 > SMA20 (선택)
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
        """백테스트: MACD 양전환 entry / 데드크로스 exit."""
        p = self.params
        indicator_params = {
            "macd_fast": p.get("macd_fast", 12),
            "macd_slow": p.get("macd_slow", 26),
            "macd_signal": p.get("macd_signal", 9),
            "rsi_period": p.get("rsi_period", 14),
        }

        df_ind = calculate_indicators(df, **indicator_params)

        rsi_min = p.get("rsi_min", 35)
        rsi_max = p.get("rsi_max", 70)
        volume_multiplier = p.get("volume_multiplier", 1.2)
        stop_atr_mult = p.get("stop_atr_mult", 2.0)

        # Entry: MACD 양전환 + RSI + 종가 > SMA20 + 거래량
        cond_ma = df_ind["close"] > df_ind["sma20"]
        cond_macd = (df_ind["macd_hist"] > 0) & (
            df_ind["macd_hist"].shift(1) < 0
        )
        cond_rsi = (df_ind["rsi"] >= rsi_min) & (df_ind["rsi"] <= rsi_max)
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * volume_multiplier

        raw_entries = cond_ma & cond_macd & cond_rsi & cond_vol

        # Exit: MACD 데드크로스 OR RSI > 70 OR ATR 손절
        cond_macd_dead = (df_ind["macd_hist"] < 0) & (
            df_ind["macd_hist"].shift(1) > 0
        )
        cond_rsi_high = df_ind["rsi"] > 70
        cond_stop = df_ind["close"] < (
            df_ind["close"].shift(1) - df_ind["atr"] * stop_atr_mult
        )

        raw_exits = cond_macd_dead | cond_rsi_high | cond_stop

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).astype("boolean").fillna(False).astype(bool)
        exits = raw_exits.shift(1).astype("boolean").fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

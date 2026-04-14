"""MACD Pullback 전략 — 추세 지속 중 눌림목 매수.

golden_cross가 추세 시작을 포착한다면,
이 전략은 추세 유지 중 조정(pullback) 후 재진입을 포착.

진입: MACD > Signal (추세 확인) + RSI 눌림 후 반등
청산: MACD 데드크로스 OR RSI > 70 OR ATR 손절
"""

import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class MacdPullbackStrategy(BaseStrategy):
    """MACD Pullback 전략 — 추세 중 눌림목 재진입."""

    name = "macd_pullback"
    category = "trend"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: MACD > Signal + RSI 눌림 후 반등."""
        if len(df) < 3:
            return False
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        rsi_pullback = self.params.get("rsi_pullback", 45)
        adx_threshold = self.params.get("adx_threshold", 20)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)

        # 1. MACD > Signal (상승 추세 유지 중)
        if latest.get("macd", 0) <= latest.get("macd_signal", 0):
            return False

        # 2. 종가 > SMA20 (추세선 위)
        if latest["close"] <= latest.get("sma20", 0):
            return False

        # 3. ADX 추세 강도
        if latest.get("adx", 0) < adx_threshold:
            return False

        # 4. RSI 눌림: 전일 RSI < rsi_pullback (조정 확인)
        prev_rsi = prev.get("rsi", 50)
        if prev_rsi >= rsi_pullback:
            return False

        # 5. RSI 반등: 당일 RSI > 전일 RSI
        curr_rsi = latest.get("rsi", 50)
        if curr_rsi <= prev_rsi:
            return False

        # 6. 거래량 확인
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: MACD > Signal + RSI 눌림 반등 + 추세 확인."""
        if len(df_daily) < 3:
            return False
        latest = df_daily.iloc[-1]
        prev = df_daily.iloc[-2]

        rsi_pullback = self.params.get("rsi_pullback", 45)
        adx_threshold = self.params.get("adx_threshold", 20)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)

        # 1. MACD > Signal (상승 추세)
        if latest.get("macd", 0) <= latest.get("macd_signal", 0):
            return False

        # 2. 종가 > SMA20
        if latest["close"] <= latest.get("sma20", 0):
            return False

        # 3. ADX >= 임계값
        if latest.get("adx", 0) < adx_threshold:
            return False

        # 4. RSI 눌림 후 반등
        prev_rsi = prev.get("rsi", 50)
        curr_rsi = latest.get("rsi", 50)
        if prev_rsi >= rsi_pullback:
            return False
        if curr_rsi <= prev_rsi:
            return False

        # 5. 거래량
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        # 6. 60분봉 확인 (선택)
        if df_60m is not None and len(df_60m) >= 5:
            df_60m_ind = calculate_indicators(df_60m)
            if not df_60m_ind.empty:
                last_60m = df_60m_ind.iloc[-1]
                if last_60m.get("sma5", 0) > 0 and last_60m.get("sma20", 0) > 0:
                    if last_60m["sma5"] <= last_60m["sma20"]:
                        return False

        return True

    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트: MACD > Signal + RSI 눌림 반등 entry / 데드크로스 exit."""
        p = self.params
        indicator_params = {
            "macd_fast": p.get("macd_fast", 12),
            "macd_slow": p.get("macd_slow", 26),
            "macd_signal": p.get("macd_signal", 9),
            "rsi_period": p.get("rsi_period", 14),
        }

        df_ind = calculate_indicators(df, **indicator_params)

        rsi_pullback = p.get("rsi_pullback", 45)
        adx_threshold = p.get("adx_threshold", 20)
        volume_multiplier = p.get("volume_multiplier", 1.0)
        stop_atr_mult = p.get("stop_atr_mult", 2.0)

        # Entry 조건
        cond_macd_trend = df_ind["macd"] > df_ind["macd_signal"]
        cond_above_sma20 = df_ind["close"] > df_ind["sma20"]
        cond_adx = df_ind["adx"] >= adx_threshold
        cond_rsi_pullback = df_ind["rsi"].shift(1) < rsi_pullback
        cond_rsi_bounce = df_ind["rsi"] > df_ind["rsi"].shift(1)
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * volume_multiplier

        raw_entries = (
            cond_macd_trend & cond_above_sma20 & cond_adx
            & cond_rsi_pullback & cond_rsi_bounce & cond_vol
        )

        # Exit: MACD 데드크로스 OR RSI > 70 OR ATR 손절
        cond_macd_dead = (df_ind["macd"] < df_ind["macd_signal"]) & (
            df_ind["macd"].shift(1) >= df_ind["macd_signal"].shift(1)
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

"""골든크로스 변형 스윙 전략.

매수: SMA5/20 골든크로스 + RSI + ADX + 거래량
매도: SMA5/20 데드크로스, RSI > 70, ATR 손절
"""

import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class GoldenCrossStrategy(BaseStrategy):
    """골든크로스 변형 스윙 전략."""

    name = "golden_cross"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: 최근 N일 내 골든크로스 발생 + 유지 + RSI + ADX + 거래량.

        당일 정확히 크로스가 발생하지 않아도,
        최근 screening_lookback(기본 5)일 내 발생 후 유지 중이면 통과.
        """
        lookback = self.params.get("screening_lookback", 5)
        if len(df) < lookback + 1:
            return False

        latest = df.iloc[-1]
        adx_threshold = self.params.get("adx_threshold", 20)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)

        # 현재 SMA5 > SMA20 (골든크로스 유지 중)
        if latest["sma5"] <= latest["sma20"]:
            return False

        # 최근 N일 내 크로스 발생 확인
        recent = df.iloc[-(lookback + 1):]
        cross_found = False
        for i in range(1, len(recent)):
            if (recent.iloc[i]["sma5"] > recent.iloc[i]["sma20"] and
                    recent.iloc[i - 1]["sma5"] <= recent.iloc[i - 1]["sma20"]):
                cross_found = True
                break
        if not cross_found:
            return False

        cond_rsi = latest["rsi"] >= 50
        cond_adx = latest["adx"] >= adx_threshold
        cond_vol = latest["volume"] >= latest["volume_sma20"] * volume_multiplier

        return all([cond_rsi, cond_adx, cond_vol])

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: SMA5 > SMA20 + RSI 적정 + ADX + 거래량 + 60분봉 확인."""
        if len(df_daily) < 2:
            return False
        latest = df_daily.iloc[-1]
        prev = df_daily.iloc[-2]

        adx_threshold = self.params.get("adx_threshold", 20)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)
        rsi_min = self.params.get("rsi_entry_min", 40)
        rsi_max = self.params.get("rsi_entry_max", 65)

        # 1. 추세: 종가 > SMA20
        if latest["close"] <= latest["sma20"]:
            return False

        # 2. SMA5 > SMA20 (골든크로스 유지 또는 발생)
        if latest["sma5"] <= latest["sma20"]:
            return False

        # 3. RSI 적정 범위
        rsi = latest.get("rsi", 50)
        if rsi < rsi_min or rsi > rsi_max:
            return False

        # 4. ADX 추세 강도
        if latest.get("adx", 0) < adx_threshold:
            return False

        # 5. 거래량 확인
        if latest["volume"] < latest["volume_sma20"] * volume_multiplier:
            return False

        # 6. 60분봉 단기 추세 (선택)
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
        """백테스트: 골든크로스 entry / 데드크로스 exit."""
        p = self.params
        indicator_params = {
            "macd_fast": p.get("macd_fast", 12),
            "macd_slow": p.get("macd_slow", 26),
            "macd_signal": p.get("macd_signal", 9),
            "rsi_period": p.get("rsi_period", 14),
        }

        df_ind = calculate_indicators(df, **indicator_params)

        adx_threshold = p.get("adx_threshold", 20)
        volume_multiplier = p.get("volume_multiplier", 1.0)

        # Entry: SMA5/20 골든크로스 + RSI >= 50 + ADX >= threshold + 거래량
        cond_cross = (df_ind["sma5"] > df_ind["sma20"]) & (
            df_ind["sma5"].shift(1) <= df_ind["sma20"].shift(1)
        )
        cond_rsi = df_ind["rsi"] >= 50
        cond_adx = df_ind["adx"] >= adx_threshold
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * volume_multiplier

        raw_entries = cond_cross & cond_rsi & cond_adx & cond_vol

        # Exit: 데드크로스 OR RSI > 70 OR ATR 손절
        stop_atr_mult = p.get("stop_atr_mult", 2.0)
        cond_dead_cross = (df_ind["sma5"] < df_ind["sma20"]) & (
            df_ind["sma5"].shift(1) >= df_ind["sma20"].shift(1)
        )
        cond_rsi_high = df_ind["rsi"] > 70
        cond_stop = df_ind["close"] < (
            df_ind["close"].shift(1) - df_ind["atr"] * stop_atr_mult
        )

        raw_exits = cond_dead_cross | cond_rsi_high | cond_stop

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).astype("boolean").fillna(False).astype(bool)
        exits = raw_exits.shift(1).astype("boolean").fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

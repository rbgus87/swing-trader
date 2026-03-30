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
    category = "trend"

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
        """장중 진입 — 백테스트(generate_backtest_signals)와 동일한 조건."""
        p = self.params
        adx_threshold = p.get("adx_threshold", 20)
        volume_multiplier = p.get("volume_multiplier", 1.0)
        rsi_entry_min = p.get("rsi_entry_min", 40)
        screening_lookback = p.get("screening_lookback", 3)

        if len(df_daily) < screening_lookback + 1:
            return False

        latest = df_daily.iloc[-1]

        # 1. SMA5 > SMA20 유지 중
        if latest["sma5"] <= latest["sma20"]:
            return False

        # 2. 최근 N일 내 크로스 발생 (백테스트와 동일)
        recent = df_daily.iloc[-(screening_lookback + 1):]
        cross_found = False
        for i in range(1, len(recent)):
            if (recent.iloc[i]["sma5"] > recent.iloc[i]["sma20"] and
                    recent.iloc[i - 1]["sma5"] <= recent.iloc[i - 1]["sma20"]):
                cross_found = True
                break
        if not cross_found:
            return False

        # 3. RSI >= 하한 (상한 없음 — 백테스트와 동일)
        if latest.get("rsi", 50) < rsi_entry_min:
            return False

        # 4. ADX >= 임계값
        if latest.get("adx", 0) < adx_threshold:
            return False

        # 5. 거래량 >= 20일 평균
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True
        # v3: close > SMA20 제거 (크로스 직후 걸림)
        # v3: RSI 상한 65 제거 (강한 모멘텀 차단)
        # v3: 60분봉 제거 (백테스트 미검증)

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
        rsi_entry_min = p.get("rsi_entry_min", 40)
        screening_lookback = p.get("screening_lookback", 3)

        # Entry: 최근 N일 내 SMA5/20 골든크로스 발생 + 유지 + RSI + ADX + 거래량
        # 정확한 크로스 발생일
        cross_day = (df_ind["sma5"] > df_ind["sma20"]) & (
            df_ind["sma5"].shift(1) <= df_ind["sma20"].shift(1)
        )
        # 최근 N일 내 크로스 발생 (당일 포함)
        cond_cross = cross_day.rolling(window=screening_lookback, min_periods=1).max().astype(bool)
        # 현재 SMA5 > SMA20 유지 중
        cond_maintain = df_ind["sma5"] > df_ind["sma20"]
        cond_rsi = df_ind["rsi"] >= rsi_entry_min
        cond_adx = df_ind["adx"] >= adx_threshold
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * volume_multiplier

        raw_entries = cond_cross & cond_maintain & cond_rsi & cond_adx & cond_vol

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

"""Stochastic Reversal 전략 — 모멘텀 과매도 반전.

bb_bounce가 가격 기반(BB 하단) 과매도를 본다면,
이 전략은 모멘텀 기반(%K/%D) 과매도 반전을 포착.

진입: %K < 20 과매도 → %K/%D 골든크로스 + 반등 확인
청산: %K > 80 과매수 OR %K/%D 데드크로스 OR ATR 손절
"""

import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class StochReversalStrategy(BaseStrategy):
    """Stochastic Reversal 전략 — 모멘텀 과매도 반전."""

    name = "stoch_reversal"
    category = "mean_reversion"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: %K 과매도 후 %K/%D 골든크로스 + 반등."""
        if len(df) < 2:
            return False
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        stoch_oversold = self.params.get("stoch_oversold", 20)
        support_pct = self.params.get("support_pct", 0.05)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)

        # 1. 전일 %K < 과매도 기준
        prev_k = prev.get("stoch_k", 50)
        if prev_k >= stoch_oversold:
            return False

        # 2. %K/%D 골든크로스 (전일 %K <= %D → 당일 %K > %D)
        prev_d = prev.get("stoch_d", 50)
        curr_k = latest.get("stoch_k", 50)
        curr_d = latest.get("stoch_d", 50)
        if not (prev_k <= prev_d and curr_k > curr_d):
            return False

        # 3. 반등 확인 (종가 > 전일 종가)
        if latest["close"] <= prev["close"]:
            return False

        # 4. 지지선 근처: 최근 20일 최저가 대비 support_pct 이내
        if len(df) >= 20:
            low_20d = df["low"].tail(20).min()
            if low_20d > 0:
                distance = (latest["close"] - low_20d) / low_20d
                if distance > support_pct:
                    return False

        # 5. 거래량 확인
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: %K/%D 골든크로스 + 반등 + 지지선."""
        if len(df_daily) < 2:
            return False
        latest = df_daily.iloc[-1]
        prev = df_daily.iloc[-2]

        stoch_oversold = self.params.get("stoch_oversold", 20)
        support_pct = self.params.get("support_pct", 0.05)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)

        # 1. 전일 %K 과매도
        prev_k = prev.get("stoch_k", 50)
        if prev_k >= stoch_oversold:
            return False

        # 2. %K/%D 골든크로스
        prev_d = prev.get("stoch_d", 50)
        curr_k = latest.get("stoch_k", 50)
        curr_d = latest.get("stoch_d", 50)
        if not (prev_k <= prev_d and curr_k > curr_d):
            return False

        # 3. 반등 확인
        if latest["close"] <= prev["close"]:
            return False

        # 4. 지지선 근처
        if len(df_daily) >= 20:
            low_20d = df_daily["low"].tail(20).min()
            if low_20d > 0:
                distance = (latest["close"] - low_20d) / low_20d
                if distance > support_pct:
                    return False

        # 5. 거래량
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        # 6. 60분봉 확인 (선택)
        if df_60m is not None and len(df_60m) >= 5:
            df_60m_ind = calculate_indicators(df_60m)
            if not df_60m_ind.empty:
                last_60m = df_60m_ind.iloc[-1]
                if last_60m.get("close", 0) < last_60m.get("sma5", 0):
                    return False

        return True

    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트: %K/%D 골든크로스 entry / 데드크로스·과매수 exit."""
        p = self.params
        indicator_params = {
            "macd_fast": p.get("macd_fast", 12),
            "macd_slow": p.get("macd_slow", 26),
            "macd_signal": p.get("macd_signal", 9),
            "rsi_period": p.get("rsi_period", 14),
        }

        df_ind = calculate_indicators(df, **indicator_params)

        stoch_oversold = p.get("stoch_oversold", 20)
        stoch_overbought = p.get("stoch_overbought", 80)
        support_pct = p.get("support_pct", 0.05)
        stop_atr_mult = p.get("stop_atr_mult", 2.0)

        # Entry 조건
        # 1. 전일 %K < 과매도
        cond_oversold = df_ind["stoch_k"].shift(1) < stoch_oversold
        # 2. %K/%D 골든크로스
        cond_cross = (
            (df_ind["stoch_k"] > df_ind["stoch_d"])
            & (df_ind["stoch_k"].shift(1) <= df_ind["stoch_d"].shift(1))
        )
        # 3. 반등
        cond_bounce = df_ind["close"] > df_ind["close"].shift(1)
        # 4. 지지선 근처 (20일 최저가 대비)
        low_20d = df_ind["low"].rolling(20).min()
        distance_to_support = (df_ind["close"] - low_20d) / low_20d.replace(0, float("nan"))
        cond_support = distance_to_support <= support_pct
        # 5. 거래량
        vol_mult = p.get("volume_multiplier", 1.0)
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * vol_mult

        raw_entries = cond_oversold & cond_cross & cond_bounce & cond_support & cond_vol

        # Exit 조건
        # 1. %K > 과매수
        cond_overbought = df_ind["stoch_k"] > stoch_overbought
        # 2. %K/%D 데드크로스
        cond_dead = (
            (df_ind["stoch_k"] < df_ind["stoch_d"])
            & (df_ind["stoch_k"].shift(1) >= df_ind["stoch_d"].shift(1))
        )
        # 3. RSI > 65
        cond_rsi_high = df_ind["rsi"] > 65
        # 4. ATR 손절
        cond_stop = df_ind["close"] < (
            df_ind["close"].shift(1) - df_ind["atr"] * stop_atr_mult
        )

        raw_exits = cond_overbought | cond_dead | cond_rsi_high | cond_stop

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).astype("boolean").fillna(False).astype(bool)
        exits = raw_exits.shift(1).astype("boolean").fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

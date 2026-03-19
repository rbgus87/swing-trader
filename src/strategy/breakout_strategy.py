"""N일 고가 돌파 전략.

매수: 20일 최고가 돌파 + 거래량 급증 + ADX 추세 확인
매도: 데드크로스 또는 ATR 손절
추세 전환 초기를 포착. 기관 수급 집중 시점과 일치하는 경우 다수.
"""

import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class BreakoutStrategy(BaseStrategy):
    """N일 고가 돌파 전략."""

    name = "breakout"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: N일 신고가 돌파 + 거래량 급증."""
        breakout_period = self.params.get("breakout_period", 20)
        if len(df) < breakout_period + 1:
            return False

        latest = df.iloc[-1]
        volume_multiplier = self.params.get("volume_multiplier", 1.5)
        adx_threshold = self.params.get("adx_threshold", 20)

        # 1. N일 최고가 돌파 (당일 종가 > 직전 N일 고가 최대값)
        prev_high_max = df["high"].iloc[-(breakout_period + 1):-1].max()
        if latest["close"] <= prev_high_max:
            return False

        # 2. 거래량 급증
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        # 3. ADX 추세 강도 (횡보 구간 거짓 돌파 방지)
        if latest.get("adx", 0) < adx_threshold:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: 실시간 가격이 N일 고가를 돌파 확인."""
        breakout_period = self.params.get("breakout_period", 20)
        if len(df_daily) < breakout_period + 1:
            return False

        latest = df_daily.iloc[-1]
        volume_multiplier = self.params.get("volume_multiplier", 1.5)
        adx_threshold = self.params.get("adx_threshold", 20)

        # 1. N일 최고가 돌파
        prev_high_max = df_daily["high"].iloc[-(breakout_period + 1):-1].max()
        if latest["close"] <= prev_high_max:
            return False

        # 2. 거래량 급증
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        # 3. ADX 추세 확인
        if latest.get("adx", 0) < adx_threshold:
            return False

        # 4. RSI 과매수 아닌지 (너무 과열된 상태에서 돌파 추격 방지)
        rsi_max = self.params.get("rsi_entry_max", 75)
        if latest.get("rsi", 50) > rsi_max:
            return False

        # 5. 60분봉 확인 (선택)
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
        """백테스트: N일 돌파 entry / 데드크로스+ATR exit."""
        p = self.params
        indicator_params = {
            "macd_fast": p.get("macd_fast", 12),
            "macd_slow": p.get("macd_slow", 26),
            "macd_signal": p.get("macd_signal", 9),
            "rsi_period": p.get("rsi_period", 14),
        }

        df_ind = calculate_indicators(df, **indicator_params)

        breakout_period = p.get("breakout_period", 20)
        volume_multiplier = p.get("volume_multiplier", 1.5)
        adx_threshold = p.get("adx_threshold", 20)
        stop_atr_mult = p.get("stop_atr_mult", 2.0)

        # Entry: N일 최고가 돌파 + 거래량 + ADX
        rolling_high = df_ind["high"].rolling(breakout_period).max().shift(1)
        cond_breakout = df_ind["close"] > rolling_high
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * volume_multiplier
        cond_adx = df_ind["adx"] >= adx_threshold

        raw_entries = cond_breakout & cond_vol & cond_adx

        # Exit: SMA5/20 데드크로스 OR RSI > 75 OR ATR 손절
        cond_dead_cross = (df_ind["sma5"] < df_ind["sma20"]) & (
            df_ind["sma5"].shift(1) >= df_ind["sma20"].shift(1)
        )
        cond_rsi_high = df_ind["rsi"] > 75
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

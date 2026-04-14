"""볼린저밴드 반등 (평균 회귀) 전략.

매수: BB 하단 터치 + RSI 과매도 + 거래량 확인
매도: BB 중심선(20MA) 도달 또는 상단 밴드 터치
횡보장/박스권에서 강점. 추세장에서는 golden_cross가 유리.
"""

import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class BbBounceStrategy(BaseStrategy):
    """볼린저밴드 반등 전략."""

    name = "bb_bounce"
    category = "mean_reversion"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: BB 하단 근접 + RSI 과매도 + 거래량."""
        if len(df) < 2:
            return False
        latest = df.iloc[-1]

        rsi_oversold = self.params.get("rsi_oversold", 40)
        bb_touch_pct = self.params.get("bb_touch_pct", 0.10)  # 하단 대비 10% 이내

        # 1. BB 하단 근접 (종가가 하단 밴드 + 5% 이내)
        bb_lower = latest.get("bb_lower", 0)
        if bb_lower <= 0:
            return False
        bb_range = latest.get("bb_upper", 0) - bb_lower
        if bb_range <= 0:
            return False
        distance_to_lower = (latest["close"] - bb_lower) / bb_range
        if distance_to_lower > bb_touch_pct:
            return False  # 하단에서 너무 멀리 있음

        # 2. RSI 과매도
        if latest.get("rsi", 50) > rsi_oversold:
            return False

        # 3. 거래량 확인 (평균 이상)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: BB 하단 반등 확인 + RSI 과매도 탈출."""
        if len(df_daily) < 2:
            return False
        latest = df_daily.iloc[-1]
        prev = df_daily.iloc[-2]

        rsi_oversold = self.params.get("rsi_oversold", 40)
        rsi_recovery = self.params.get("rsi_recovery", 45)

        # 1. 전일 BB 하단 터치 (전일 종가 <= 하단 밴드 근처)
        bb_lower = prev.get("bb_lower", 0)
        if bb_lower <= 0:
            return False
        if prev["close"] > bb_lower * 1.02:  # 전일 하단 2% 이내
            return False

        # 2. 당일 반등 시작 (종가 > 전일 종가)
        if latest["close"] <= prev["close"]:
            return False

        # 3. RSI 과매도 탈출 (전일 과매도 → 당일 회복)
        prev_rsi = prev.get("rsi", 50)
        curr_rsi = latest.get("rsi", 50)
        if prev_rsi > rsi_oversold:
            return False  # 전일이 과매도가 아니었음
        if curr_rsi < rsi_recovery:
            return False  # 아직 회복 안 됨

        # 4. 60분봉 확인 (선택)
        if df_60m is not None and len(df_60m) >= 5:
            df_60m_ind = calculate_indicators(df_60m)
            if not df_60m_ind.empty:
                last_60m = df_60m_ind.iloc[-1]
                # 60분봉에서도 반등 확인: 종가 > SMA5
                if last_60m.get("close", 0) < last_60m.get("sma5", 0):
                    return False

        return True

    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트: BB 하단 반등 entry / 중심선 도달 exit."""
        p = self.params
        indicator_params = {
            "macd_fast": p.get("macd_fast", 12),
            "macd_slow": p.get("macd_slow", 26),
            "macd_signal": p.get("macd_signal", 9),
            "rsi_period": p.get("rsi_period", 14),
        }

        df_ind = calculate_indicators(df, **indicator_params)

        rsi_oversold = p.get("rsi_oversold", 40)
        bb_touch_pct = p.get("bb_touch_pct", 0.10)
        stop_atr_mult = p.get("stop_atr_mult", 2.0)

        # Entry: BB 하단 근접 + RSI 과매도 + 반등 시작
        bb_range = df_ind["bb_upper"] - df_ind["bb_lower"]
        distance_to_lower = (df_ind["close"] - df_ind["bb_lower"]) / bb_range.replace(0, float("nan"))
        cond_bb_touch = distance_to_lower.shift(1) <= bb_touch_pct
        cond_bounce = df_ind["close"] > df_ind["close"].shift(1)
        cond_rsi_oversold = df_ind["rsi"].shift(1) <= rsi_oversold
        cond_rsi_recovery = df_ind["rsi"] > rsi_oversold

        raw_entries = cond_bb_touch & cond_bounce & cond_rsi_oversold & cond_rsi_recovery

        # Exit: BB 중심선(20MA) 도달 OR RSI > 65 OR ATR 손절
        cond_mid_reach = df_ind["close"] >= df_ind["bb_mid"]
        cond_rsi_high = df_ind["rsi"] > 65
        cond_stop = df_ind["close"] < (
            df_ind["close"].shift(1) - df_ind["atr"] * stop_atr_mult
        )

        raw_exits = cond_mid_reach | cond_rsi_high | cond_stop

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).astype("boolean").fillna(False).astype(bool)
        exits = raw_exits.shift(1).astype("boolean").fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

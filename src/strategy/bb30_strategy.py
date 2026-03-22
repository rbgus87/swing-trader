"""볼린저30 전략 — BB(30, 1σ) 평균 회귀.

좁은 밴드(1σ, 68% 포함)를 활용한 평균 회귀 전략.
BB 하단 이탈/터치 후 중심선(30MA) 복귀를 타겟.
횡보장(sideways)에서 bb_bounce 대비 빈번한 신호 생성.
"""

import sys
import types

import pandas as pd

if "numba" not in sys.modules:
    _noop_decorator = lambda *a, **kw: (lambda f: f)
    numba_mock = types.ModuleType("numba")
    numba_mock.jit = _noop_decorator
    numba_mock.njit = _noop_decorator
    numba_mock.vectorize = _noop_decorator
    numba_mock.prange = range
    sys.modules["numba"] = numba_mock

import pandas_ta as ta

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class Bb30Strategy(BaseStrategy):
    """볼린저30 전략 — BB(30, 1σ) 평균 회귀."""

    name = "bb30"
    category = "mean_reversion"

    def _calc_bb30(self, close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
        """BB(30, 1σ) 지표 계산."""
        period = self.params.get("bb30_period", 30)
        std = self.params.get("bb30_std", 1.0)
        bb_df = ta.bbands(close, length=period, lower_std=std, upper_std=std)
        col_std = f"{std}_{std}"
        upper = bb_df[f"BBU_{period}_{col_std}"]
        mid = bb_df[f"BBM_{period}_{col_std}"]
        lower = bb_df[f"BBL_{period}_{col_std}"]
        return upper, mid, lower

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: BB(30,1σ) 하단 근접 + RSI 과매도 + 거래량."""
        if len(df) < 2:
            return False
        latest = df.iloc[-1]

        rsi_oversold = self.params.get("rsi_oversold", 40)
        bb_touch_pct = self.params.get("bb_touch_pct", 0.10)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)

        # BB(30, 1σ) 계산
        bb_upper, bb_mid, bb_lower = self._calc_bb30(df["close"])
        if bb_lower.isna().iloc[-1]:
            return False

        bb_lower_val = bb_lower.iloc[-1]
        bb_upper_val = bb_upper.iloc[-1]
        bb_range = bb_upper_val - bb_lower_val
        if bb_range <= 0:
            return False

        # 1. BB 하단 근접 (종가가 하단 밴드 + 10% 이내)
        distance_to_lower = (latest["close"] - bb_lower_val) / bb_range
        if distance_to_lower > bb_touch_pct:
            return False

        # 2. RSI 과매도
        if latest.get("rsi", 50) > rsi_oversold:
            return False

        # 3. 거래량 확인 (평균 이상)
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: BB(30,1σ) 하단 반등 확인 + RSI 과매도 탈출."""
        if len(df_daily) < 2:
            return False
        latest = df_daily.iloc[-1]
        prev = df_daily.iloc[-2]

        rsi_oversold = self.params.get("rsi_oversold", 40)
        rsi_recovery = self.params.get("rsi_recovery", 45)

        # BB(30, 1σ) 계산
        bb_upper, bb_mid, bb_lower = self._calc_bb30(df_daily["close"])
        if bb_lower.isna().iloc[-2]:
            return False

        bb_lower_prev = bb_lower.iloc[-2]
        if bb_lower_prev <= 0:
            return False

        # 1. 전일 BB 하단 터치 (전일 종가 <= 하단 밴드 × 1.02)
        if prev["close"] > bb_lower_prev * 1.02:
            return False

        # 2. 당일 반등 시작 (종가 > 전일 종가)
        if latest["close"] <= prev["close"]:
            return False

        # 3. RSI 과매도 탈출 (전일 과매도 → 당일 회복)
        prev_rsi = prev.get("rsi", 50)
        curr_rsi = latest.get("rsi", 50)
        if prev_rsi > rsi_oversold:
            return False
        if curr_rsi < rsi_recovery:
            return False

        # 4. 60분봉 확인 (선택)
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
        """백테스트: BB(30,1σ) 하단 반등 entry / 중심선 도달 exit."""
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

        # BB(30, 1σ) 계산
        bb_upper, bb_mid, bb_lower = self._calc_bb30(df_ind["close"])

        # Entry: BB 하단 근접 + RSI 과매도 + 반등 시작
        bb_range = bb_upper - bb_lower
        distance_to_lower = (df_ind["close"] - bb_lower) / bb_range.replace(0, float("nan"))
        cond_bb_touch = distance_to_lower.shift(1) <= bb_touch_pct
        cond_bounce = df_ind["close"] > df_ind["close"].shift(1)
        cond_rsi_oversold = df_ind["rsi"].shift(1) <= rsi_oversold
        cond_rsi_recovery = df_ind["rsi"] > rsi_oversold

        raw_entries = cond_bb_touch & cond_bounce & cond_rsi_oversold & cond_rsi_recovery

        # Exit: BB 중심선(30MA) 도달 OR 상단 밴드 도달 OR RSI > 60 OR ATR 손절
        cond_mid_reach = df_ind["close"] >= bb_mid
        cond_upper_reach = df_ind["close"] >= bb_upper
        cond_rsi_high = df_ind["rsi"] > 60
        cond_stop = df_ind["close"] < (
            df_ind["close"].shift(1) - df_ind["atr"] * stop_atr_mult
        )

        raw_exits = cond_mid_reach | cond_upper_reach | cond_rsi_high | cond_stop

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).astype("boolean").fillna(False).astype(bool)
        exits = raw_exits.shift(1).astype("boolean").fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

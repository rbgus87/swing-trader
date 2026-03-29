"""이격도 기반 평균회귀 전략.

엣지: 단기 과매도 반등 — 극단적 이탈은 평균으로 돌아온다.
bb_bounce 대체. 이격도(종가/SMA20)가 BB보다 직관적이고 파라미터 적음.

진입: 이격도 < 93% + RSI 과매도 + 양봉 반등
청산: 이격도 100% 복귀(20일선 터치) / 추가 하락 손절 / 최대보유 7일
"""

import numpy as np
import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class DisparityReversionStrategy(BaseStrategy):
    """이격도 기반 평균회귀 전략."""

    name = "disparity_reversion"
    category = "mean_reversion"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: 이격도 과매도 + RSI 극단 + 양봉 반등."""
        disparity_entry = self.params.get("disparity_entry", 93)
        rsi_oversold = self.params.get("rsi_oversold", 25)

        if len(df) < 60:
            return False
        latest = df.iloc[-1]

        sma20 = latest.get("sma20", 0)
        if sma20 <= 0:
            return False

        # 1. 이격도 < entry_threshold (20일선 대비 7% 이상 이탈)
        disparity = latest["close"] / sma20 * 100
        if disparity >= disparity_entry:
            return False

        # 2. RSI(14) < oversold
        if latest.get("rsi", 50) >= rsi_oversold:
            return False

        # 3. 60일 이동평균 상승 중 (장기 추세 생존 확인)
        sma60 = latest.get("sma60", 0)
        if sma60 <= 0:
            return False
        if len(df) >= 6:
            sma60_past = df.iloc[-6].get("sma60", 0)
            if sma60_past > 0 and sma60 <= sma60_past:
                return False

        # 4. 당일 양봉 (바닥 확인)
        if latest["close"] <= latest["open"]:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: 이격도 과매도 + 양봉 + 거래량 증가."""
        disparity_entry = self.params.get("disparity_entry", 93)

        if len(df_daily) < 30:
            return False
        latest = df_daily.iloc[-1]

        sma20 = latest.get("sma20", 0)
        if sma20 <= 0:
            return False

        # 1. 이격도 < entry_threshold
        disparity = latest["close"] / sma20 * 100
        if disparity >= disparity_entry:
            return False

        # 2. RSI < 30 (약간 완화)
        if latest.get("rsi", 50) >= 30:
            return False

        # 3. 당일 양봉
        if latest["close"] <= latest["open"]:
            return False

        # 4. 거래량 증가 (전일 대비)
        if len(df_daily) >= 2:
            prev_vol = df_daily.iloc[-2]["volume"]
            if prev_vol > 0 and latest["volume"] <= prev_vol:
                return False

        return True

    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트 시그널: 이격도 과매도 entry / 복귀·추가하락 exit."""
        p = self.params
        disparity_entry = p.get("disparity_entry", 93)
        disparity_exit = p.get("disparity_exit", 100)
        disparity_stop = p.get("disparity_stop", 88)
        rsi_oversold = p.get("rsi_oversold", 25)

        df_ind = calculate_indicators(df)
        if df_ind.empty:
            return pd.Series(dtype=bool), pd.Series(dtype=bool)

        # 이격도 계산
        disparity = df_ind["close"] / df_ind["sma20"] * 100

        # Entry 조건
        # 1. 이격도 < entry_threshold
        cond_disparity = disparity < disparity_entry

        # 2. RSI < oversold
        cond_rsi = df_ind["rsi"] < rsi_oversold

        # 3. 당일 양봉
        cond_bullish = df_ind["close"] > df_ind["open"]

        # 4. SMA60 상승 중
        cond_sma60_up = df_ind["sma60"] > df_ind["sma60"].shift(5)

        raw_entries = cond_disparity & cond_rsi & cond_bullish & cond_sma60_up

        # Exit 조건
        # 1. 이격도 100% 복귀 (20일선 터치)
        cond_recovery = disparity >= disparity_exit
        # 2. 이격도 추가 하락 (88% 이하)
        cond_deep_drop = disparity <= disparity_stop

        raw_exits = cond_recovery | cond_deep_drop

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).infer_objects(copy=False).fillna(False).astype(bool)
        exits = raw_exits.shift(1).infer_objects(copy=False).fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

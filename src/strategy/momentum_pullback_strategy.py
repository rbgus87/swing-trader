"""모멘텀 + 눌림목 스윙 전략.

엣지: 모멘텀 프리미엄 — 최근 잘 간 종목이 계속 간다 (학술적 anomaly).
진입: 60일 모멘텀 상위 종목이 3~5일 눌림목 후 반등할 때.
청산: 목표가/손절/최대보유/모멘텀 이탈.

핵심 차이 (기존 macd_pullback 대비):
- MACD 크로스(후행 지표) 대신 가격 자체의 pullback(N일 하락)을 사용
- 모멘텀 필터를 스크리닝이 아닌 전략 진입 조건에 내장
"""

import numpy as np
import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class MomentumPullbackStrategy(BaseStrategy):
    """모멘텀 + 눌림목 스윙 전략."""

    name = "momentum_pullback"
    category = "trend"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: 60일 모멘텀 양수 + 최근 눌림목 + 반등 시작."""
        momentum_period = self.params.get("momentum_period", 60)
        pullback_days = self.params.get("pullback_days", 3)
        rsi_pullback_threshold = self.params.get("rsi_pullback_threshold", 30)

        if len(df) < momentum_period + 5:
            return False
        latest = df.iloc[-1]

        # 1. 60일 모멘텀 양수 (상승 추세 종목)
        momentum = (latest["close"] - df.iloc[-momentum_period]["close"]) / df.iloc[-momentum_period]["close"]
        if momentum <= 0:
            return False

        # 2. 종가 > 20일선 (추세 유지)
        if latest["close"] <= latest.get("sma20", 0):
            return False

        # 3. 최근 N일 중 하락일이 과반 (눌림목 확인)
        recent = df.iloc[-pullback_days:]
        down_days = sum(1 for i in range(len(recent)) if recent.iloc[i]["close"] < recent.iloc[i]["open"])
        if down_days < pullback_days // 2 + 1:
            # 대안: RSI가 pullback 수준이면 통과
            if latest.get("rsi", 50) > rsi_pullback_threshold:
                return False

        # 4. 당일 양봉 (반등 시작)
        if latest["close"] <= latest["open"]:
            return False

        # 5. 거래량 확인
        volume_multiplier = self.params.get("volume_multiplier", 1.0)
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: 눌림목 반등 확인 + 모멘텀 유지."""
        momentum_period = self.params.get("momentum_period", 60)
        pullback_days = self.params.get("pullback_days", 3)

        if len(df_daily) < momentum_period + 5:
            return False
        latest = df_daily.iloc[-1]

        # 1. 60일 모멘텀 양수
        past = df_daily.iloc[-momentum_period]
        momentum = (latest["close"] - past["close"]) / past["close"]
        if momentum <= 0:
            return False

        # 2. 종가 > 20일선
        if latest["close"] <= latest.get("sma20", 0):
            return False

        # 3. 최근 N일 눌림 후 당일 반등
        if len(df_daily) >= pullback_days + 1:
            pullback_start = df_daily.iloc[-(pullback_days + 1)]
            pullback_end = df_daily.iloc[-2]
            pullback_pct = (pullback_end["close"] - pullback_start["close"]) / pullback_start["close"]
            if pullback_pct > -0.02:
                return False  # 최소 2% 이상 눌림 필요

        # 4. 당일 양봉 + 전일 대비 반등
        if latest["close"] <= df_daily.iloc[-2]["close"]:
            return False

        # 5. 거래량 확인
        volume_multiplier = self.params.get("volume_multiplier", 1.0)
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트 시그널: 모멘텀 + 눌림목 반등 entry / 모멘텀 이탈 exit."""
        p = self.params
        momentum_period = p.get("momentum_period", 60)
        pullback_days = p.get("pullback_days", 3)
        rsi_pullback = p.get("rsi_pullback_threshold", 30)
        volume_multiplier = p.get("volume_multiplier", 1.0)

        df_ind = calculate_indicators(df)
        if df_ind.empty:
            return pd.Series(dtype=bool), pd.Series(dtype=bool)

        # Entry 조건
        # 1. 60일 모멘텀 양수
        momentum = df_ind["close"].pct_change(momentum_period)
        cond_momentum = momentum > 0

        # 2. 종가 > SMA20
        cond_above_sma = df_ind["close"] > df_ind["sma20"]

        # 3. 최근 N일 하락 (pullback)
        rolling_return = df_ind["close"].pct_change(pullback_days)
        cond_pullback = rolling_return < -0.02  # 최소 2% 하락

        # 4. 당일 양봉 (반등)
        cond_bullish = df_ind["close"] > df_ind["open"]

        # 5. 5일 RSI 과매도 (대안 pullback 신호)
        delta = df_ind["close"].diff()
        gain = delta.clip(lower=0).rolling(5).mean()
        loss = (-delta.clip(upper=0)).rolling(5).mean()
        rs = gain / (loss + 1e-10)
        rsi5 = 100 - 100 / (1 + rs)
        cond_rsi_oversold = rsi5 < rsi_pullback

        # 6. 거래량
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * volume_multiplier

        # 눌림목 OR RSI 과매도
        cond_pullback_or_rsi = cond_pullback | cond_rsi_oversold

        raw_entries = cond_momentum & cond_above_sma & cond_pullback_or_rsi & cond_bullish & cond_vol

        # Exit 조건
        # 1. 모멘텀 이탈: 60일 모멘텀 음전환
        cond_momentum_exit = momentum < 0
        # 2. 20일선 이탈
        cond_below_sma = df_ind["close"] < df_ind["sma20"]

        raw_exits = cond_momentum_exit | cond_below_sma

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).infer_objects(copy=False).fillna(False).astype(bool)
        exits = raw_exits.shift(1).infer_objects(copy=False).fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

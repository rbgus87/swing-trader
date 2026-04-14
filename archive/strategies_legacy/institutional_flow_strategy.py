"""수급 기반 스윙 전략.

엣지: 외국인/기관의 정보 우위 — 대형 자금이 먼저 움직인다.
한국 시장에서 특히 강력. 정보 비대칭 + 대형 자금 분할 체결로 설명.

진입: 외국인/기관 연속 순매수 종목 + 추세 확인 (기술적 조건)
청산: 외국인 순매도 전환 / 목표가 / 손절 / 최대보유

백테스트 주의: 기관/외국인 순매수 데이터를 일별로 조회하면 API 부하가 큼.
백테스트용 entry에는 기술적 조건만 사용하고, 수급은 종목 선정 단계에서 필터.
"""

import numpy as np
import pandas as pd

from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class InstitutionalFlowStrategy(BaseStrategy):
    """수급 기반 스윙 전략."""

    name = "institutional_flow"
    category = "trend"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: 추세 + 방향성 + 거래량 확인.

        수급 필터는 screener/engine 레벨에서 처리.
        여기서는 기술적 조건만 체크.
        """
        adx_threshold = self.params.get("adx_threshold", 20)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)

        if len(df) < 30:
            return False
        latest = df.iloc[-1]

        # 1. 종가 > 20일선 (추세 유지)
        if latest["close"] <= latest.get("sma20", 0):
            return False

        # 2. ADX > threshold (방향성)
        if latest.get("adx", 0) <= adx_threshold:
            return False

        # 3. 거래량 > 20일 평균
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        # 4. 당일 양봉
        if latest["close"] <= latest["open"]:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: 추세 + 방향성 + 거래량 확인."""
        adx_threshold = self.params.get("adx_threshold", 20)
        volume_multiplier = self.params.get("volume_multiplier", 1.0)

        if len(df_daily) < 30:
            return False
        latest = df_daily.iloc[-1]

        # 1. 종가 > 20일선
        if latest["close"] <= latest.get("sma20", 0):
            return False

        # 2. ADX > threshold
        if latest.get("adx", 0) <= adx_threshold:
            return False

        # 3. 당일 양봉
        if latest["close"] <= latest["open"]:
            return False

        # 4. 거래량 > 20일 평균
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트 시그널: 추세+방향성 entry / 추세 이탈+과열 exit.

        수급 데이터는 백테스트에서 직접 사용하지 않음 (API 부하).
        수급이 좋은 종목을 넣었다는 가정으로 기술적 조건만 적용.
        """
        p = self.params
        adx_threshold = p.get("adx_threshold", 20)
        volume_multiplier = p.get("volume_multiplier", 1.0)

        df_ind = calculate_indicators(df)
        if df_ind.empty:
            return pd.Series(dtype=bool), pd.Series(dtype=bool)

        # Entry 조건
        # 1. 종가 > SMA20
        cond_above_sma = df_ind["close"] > df_ind["sma20"]

        # 2. ADX > threshold
        cond_adx = df_ind["adx"] > adx_threshold

        # 3. 당일 양봉
        cond_bullish = df_ind["close"] > df_ind["open"]

        # 4. 거래량 > 20일 평균
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * volume_multiplier

        raw_entries = cond_above_sma & cond_adx & cond_bullish & cond_vol

        # Exit 조건
        # 1. 추세 이탈: 종가 < SMA20
        cond_below_sma = df_ind["close"] < df_ind["sma20"]
        # 2. 과열: RSI > 70
        cond_overbought = df_ind["rsi"] > 70

        raw_exits = cond_below_sma | cond_overbought

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).infer_objects(copy=False).fillna(False).astype(bool)
        exits = raw_exits.shift(1).infer_objects(copy=False).fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

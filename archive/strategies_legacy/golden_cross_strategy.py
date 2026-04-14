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
        self,
        df_daily: pd.DataFrame,
        df_60m: pd.DataFrame | None = None,
        current_price: int | None = None,
        today_volume: int | None = None,
        df_daily_raw: pd.DataFrame | None = None,
    ) -> bool:
        """장중 진입 — 오늘 현재가를 가상 일봉으로 추가하여 SMA 재계산.

        Args:
            df_daily: 어제까지의 일봉 OHLCV+지표 (calculate_indicators 적용, dropna됨).
            df_60m: 사용 안 함 (호환성 유지).
            current_price: 오늘 현재가 (가상 일봉의 close). None이면 기존 동작.
            today_volume: 오늘 누적 거래량 (없으면 어제 거래량으로 fallback).
            df_daily_raw: dropna 이전의 raw OHLCV. 가상 일봉 재계산에 사용.
        """
        p = self.params
        adx_threshold = p.get("adx_threshold", 20)
        volume_multiplier = p.get("volume_multiplier", 1.0)
        rsi_entry_min = p.get("rsi_entry_min", 40)
        screening_lookback = p.get("screening_lookback", 3)

        if len(df_daily) < screening_lookback + 1:
            self._last_reject = "데이터부족"
            return False

        # current_price가 없으면 기존 동작 (어제 종가 기준)
        if current_price is None:
            df_for_check = df_daily
        else:
            # raw OHLCV 우선 사용, 없으면 fallback (지표 계산된 df_daily)
            base_df = df_daily_raw if df_daily_raw is not None else df_daily
            df_for_check = self._build_with_today_candle(
                base_df, current_price, today_volume
            )
            if df_for_check is None or df_for_check.empty:
                self._last_reject = "가상일봉생성실패"
                return False

        latest = df_for_check.iloc[-1]

        # 1. SMA5 > SMA20 유지 중
        sma5 = latest["sma5"]
        sma20 = latest["sma20"]
        if sma5 <= sma20:
            self._last_reject = f"SMA5({sma5:,.0f})<=SMA20({sma20:,.0f})"
            return False

        # 2. 최근 N일 내 크로스 발생 (백테스트와 동일)
        recent = df_for_check.iloc[-(screening_lookback + 1):]
        cross_found = False
        for i in range(1, len(recent)):
            if (recent.iloc[i]["sma5"] > recent.iloc[i]["sma20"] and
                    recent.iloc[i - 1]["sma5"] <= recent.iloc[i - 1]["sma20"]):
                cross_found = True
                break
        if not cross_found:
            self._last_reject = f"크로스없음(최근{screening_lookback}일)"
            return False

        # 3. RSI >= 하한 (상한 없음 — 백테스트와 동일)
        rsi = latest.get("rsi", 50)
        if rsi < rsi_entry_min:
            self._last_reject = f"RSI({rsi:.1f})<{rsi_entry_min}"
            return False

        # 4. ADX >= 임계값
        adx = latest.get("adx", 0)
        if adx < adx_threshold:
            self._last_reject = f"ADX({adx:.1f})<{adx_threshold}"
            return False

        # 5. 거래량 >= 20일 평균
        vol = latest["volume"]
        vol_sma = latest.get("volume_sma20", 0)
        if vol < vol_sma * volume_multiplier:
            self._last_reject = f"거래량({vol:,.0f})<평균({vol_sma:,.0f})"
            return False

        self._last_reject = ""
        return True

    def _build_with_today_candle(
        self,
        df_daily: pd.DataFrame,
        current_price: int,
        today_volume: int | None,
    ) -> pd.DataFrame | None:
        """raw OHLCV에 오늘 가상 일봉을 추가하고 지표 재계산.

        Args:
            df_daily: dropna 이전의 raw OHLCV DataFrame (ADX 28일 NaN 커버용).
        """
        from datetime import datetime

        from loguru import logger

        from src.strategy.signals import calculate_indicators

        # 방어 1: raw OHLCV 길이 검증 (ADX 28일 NaN + 여유)
        if df_daily is None or df_daily.empty:
            logger.debug("[가상일봉] df_daily is None/empty")
            return None
        if len(df_daily) < 50:
            logger.debug(f"[가상일봉] OHLCV 길이 부족: {len(df_daily)} < 50")
            return None

        # 방어 2: current_price 검증
        if current_price is None or current_price <= 0:
            logger.debug(f"[가상일봉] current_price 무효: {current_price}")
            return None

        yesterday = df_daily.iloc[-1]
        yesterday_close_raw = yesterday.get("close")
        if yesterday_close_raw is None or pd.isna(yesterday_close_raw):
            logger.debug(f"[가상일봉] 어제 종가 무효: {yesterday_close_raw}")
            return None
        yesterday_close = float(yesterday_close_raw)

        # 거래량 fallback
        if today_volume and today_volume > 0:
            vol = int(today_volume)
        else:
            yesterday_vol = yesterday.get("volume", 0)
            if yesterday_vol is None or pd.isna(yesterday_vol):
                vol = 0
            else:
                vol = int(yesterday_vol)

        today_row = {
            "open": yesterday_close,
            "high": max(yesterday_close, float(current_price)),
            "low": min(yesterday_close, float(current_price)),
            "close": float(current_price),
            "volume": vol,
        }

        if "date" in df_daily.columns:
            today_row["date"] = datetime.now().strftime("%Y-%m-%d")

        base_cols = [
            c for c in ["date", "open", "high", "low", "close", "volume"]
            if c in df_daily.columns
        ]
        df_ohlcv = df_daily[base_cols].copy()
        new_row_df = pd.DataFrame([{c: today_row.get(c) for c in base_cols}])
        df_combined = pd.concat([df_ohlcv, new_row_df], ignore_index=True)

        # 방어 3: 지표 재계산
        try:
            df_with_indicators = calculate_indicators(df_combined)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"[가상일봉] calculate_indicators 예외: {e}"
            )
            return None

        if df_with_indicators is None or df_with_indicators.empty:
            length = len(df_with_indicators) if df_with_indicators is not None else "None"
            logger.warning(f"[가상일봉] 지표 재계산 후 empty (len={length})")
            return None

        return df_with_indicators

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

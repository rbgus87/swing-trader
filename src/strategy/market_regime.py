"""시장 국면 판단 — 모든 전략에 공통 적용되는 게이트.

KOSPI 지수의 200일 이동평균을 기준으로 추세장/방어 모드를 판단.
추세장이 아니면 매수 신호를 차단하여 하락장 진입을 방지.
"""

from datetime import datetime, timedelta

import pandas as pd
from loguru import logger
from pykrx import stock


class MarketRegime:
    """시장 국면 판단기.

    KOSPI 200일선 기준:
    - 지수 > 200일선 → 추세장 (매수 허용)
    - 지수 < 200일선 → 방어 모드 (매수 차단)
    """

    def __init__(self, sma_period: int = 200):
        self._sma_period = sma_period
        self._is_bullish: bool | None = None
        self._kospi_close: int = 0
        self._kospi_sma200: float = 0.0
        self._last_check_date: str = ""

    def check(self, date: str | None = None) -> bool:
        """시장 국면 판단 (하루 1회 캐싱).

        Args:
            date: 기준일 (YYYYMMDD). None이면 오늘.

        Returns:
            True = 추세장 (매수 허용), False = 방어 모드 (매수 차단).
        """
        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        # 같은 날 이미 체크했으면 캐시 반환
        if date == self._last_check_date and self._is_bullish is not None:
            return self._is_bullish

        try:
            # KOSPI 지수 일봉 조회 (200일 + 여유분)
            start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=400)).strftime("%Y%m%d")
            df = stock.get_index_ohlcv_by_date(start, date, "1001")  # 1001 = KOSPI

            if df.empty or len(df) < self._sma_period:
                logger.warning(f"KOSPI 데이터 부족 ({len(df)}일) — 추세장으로 간주")
                self._is_bullish = True
                return True

            # 종가 컬럼명 처리 (pykrx 버전에 따라 다를 수 있음)
            close_col = "종가" if "종가" in df.columns else "close"
            closes = df[close_col]

            sma200 = closes.rolling(self._sma_period).mean().iloc[-1]
            latest_close = closes.iloc[-1]

            self._kospi_close = int(latest_close)
            self._kospi_sma200 = float(sma200)
            self._is_bullish = latest_close > sma200
            self._last_check_date = date

            regime = "추세장" if self._is_bullish else "방어모드"
            logger.info(
                f"시장 국면: {regime} | KOSPI {self._kospi_close:,} "
                f"(200일선 {self._kospi_sma200:,.0f})"
            )
            return self._is_bullish

        except Exception as e:
            logger.error(f"시장 국면 판단 실패: {e} — 추세장으로 간주")
            self._is_bullish = True
            return True

    @property
    def is_bullish(self) -> bool:
        """현재 추세장 여부 (마지막 체크 결과)."""
        if self._is_bullish is None:
            return self.check()
        return self._is_bullish

    @property
    def kospi_close(self) -> int:
        return self._kospi_close

    @property
    def kospi_sma200(self) -> float:
        return self._kospi_sma200

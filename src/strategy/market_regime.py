"""시장 국면 판단 — 글로벌 약세장 게이트 (매수 차단 전용).

KOSPI < 200일선 AND VKOSPI > 30 → bearish (전 종목 매수 차단).
그 외 → allow (개별 종목 국면은 Screener에서 판단).

개별 종목의 추세/횡보 판단은 Screener._judge_stock_regime()에서
투표 기반으로 수행합니다.
"""

from datetime import datetime, timedelta

import pandas as pd
from loguru import logger


class MarketRegime:
    """시장 국면 판단기 — 글로벌 약세장 게이트.

    판단 기준:
    - KOSPI < 200일선 AND VKOSPI > 30 → 약세장 (매수 차단)
    - 그 외 → 허용 (개별 종목 국면에 위임)
    """

    def __init__(
        self,
        sma_period: int = 200,
        vkospi_fear_threshold: float = 30.0,
    ):
        self._sma_period = sma_period
        self._vkospi_fear_threshold = vkospi_fear_threshold

        self._is_bullish: bool | None = None
        self._regime_type: str = "unknown"  # allow / bearish
        self._kospi_close: int = 0
        self._kospi_sma200: float = 0.0
        self._kospi_adx: float = 0.0
        self._vkospi: float = 0.0
        self._block_reason: str = ""
        self._last_check_date: str = ""

    def check(self, date: str | None = None) -> bool:
        """시장 국면 판단 (하루 1회 캐싱).

        Args:
            date: 기준일 (YYYYMMDD). None이면 오늘.

        Returns:
            True = 매수 허용, False = 약세장 (매수 차단).
        """
        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        # 같은 날 이미 체크했으면 캐시 반환
        if date == self._last_check_date and self._is_bullish is not None:
            return self._is_bullish

        try:
            # KOSPI 지수 일봉 조회 — DataProvider 경유 (KRX API → pykrx → KODEX200 폴백)
            from data.provider import get_provider
            provider = get_provider()
            start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=400)).strftime("%Y%m%d")
            df = provider.get_kospi_ohlcv(start, date)

            if df.empty or len(df) < self._sma_period:
                logger.warning(f"KOSPI 데이터 부족 ({len(df) if not df.empty else 0}일) — 매수 허용으로 간주")
                self._is_bullish = True
                return True

            # KODEX200 ETF 프록시 여부 감지
            is_proxy = getattr(df, "attrs", {}).get("source") == "kodex200_proxy"

            closes = df["close"]

            # 1. KOSPI 200일선 체크
            sma200 = closes.rolling(self._sma_period).mean().iloc[-1]
            latest_close = closes.iloc[-1]
            self._kospi_close = int(latest_close)
            self._kospi_sma200 = float(sma200)

            above_sma = latest_close > sma200

            # 2. VKOSPI 공포 차단
            self._vkospi = self._get_vkospi(date)
            is_calm = self._vkospi <= self._vkospi_fear_threshold

            # 종합 판단: 극단적 약세장만 차단
            # KOSPI < SMA200 AND VKOSPI > 30 → bearish
            # 그 외 → allow (개별 종목 국면에 위임)
            if not above_sma and not is_calm:
                self._is_bullish = False
                self._regime_type = "bearish"
            else:
                self._is_bullish = True
                self._regime_type = "allow"

            self._last_check_date = date

            # 차단 사유 기록
            reasons = []
            if not above_sma:
                reasons.append(f"KOSPI {self._kospi_close:,} < 200일선 {self._kospi_sma200:,.0f}")
            if not is_calm:
                reasons.append(f"VKOSPI {self._vkospi:.1f} > {self._vkospi_fear_threshold} (공포)")
            self._block_reason = " | ".join(reasons) if reasons else ""

            regime_label = "약세장(차단)" if not self._is_bullish else "허용"
            source_label = "KODEX200(프록시)" if is_proxy else "KOSPI"
            detail = (
                f"{source_label} {self._kospi_close:,} "
                f"(200일선 {self._kospi_sma200:,.0f}), "
                f"VKOSPI {self._vkospi:.1f}"
            )
            logger.info(f"시장 국면: {regime_label} | {detail}")
            if self._block_reason:
                logger.info(f"차단 사유: {self._block_reason}")

            return self._is_bullish

        except Exception as e:
            logger.error(f"시장 국면 판단 실패: {e} — 매수 허용으로 간주")
            self._is_bullish = True
            return True

    def _get_vkospi(self, date: str) -> float:
        """VKOSPI(변동성지수) 조회 — DataProvider 경유."""
        try:
            from data.provider import get_provider
            return get_provider().get_vkospi(date)
        except Exception as e:
            logger.debug(f"VKOSPI 조회 실패: {e}")
            return 0.0

    @property
    def is_bullish(self) -> bool:
        """현재 매수 허용 여부 (마지막 체크 결과)."""
        if self._is_bullish is None:
            return self.check()
        return self._is_bullish

    @property
    def kospi_close(self) -> int:
        return self._kospi_close

    @property
    def kospi_sma200(self) -> float:
        return self._kospi_sma200

    @property
    def kospi_adx(self) -> float:
        return self._kospi_adx

    @property
    def vkospi(self) -> float:
        return self._vkospi

    @property
    def regime_type(self) -> str:
        """현재 국면 유형: allow / bearish."""
        return self._regime_type

    @property
    def block_reason(self) -> str:
        return self._block_reason

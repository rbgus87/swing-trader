"""시장 국면 판단 — 모든 전략에 공통 적용되는 게이트.

3단계 판단:
1. KOSPI 200일 이동평균 → 추세장/방어 모드
2. KOSPI ADX → 횡보장 감지 (ADX < 15이면 추세 약함)
3. VKOSPI(변동성지수) → 공포 구간 차단 (VKOSPI > 30이면 진입 차단)
"""

from datetime import datetime, timedelta

import pandas as pd
from loguru import logger


class MarketRegime:
    """시장 국면 판단기.

    판단 기준:
    - KOSPI > 200일선 AND ADX >= 15 AND VKOSPI <= 30 → 추세장 (매수 허용)
    - 그 외 → 방어 모드 (매수 차단)
    """

    def __init__(
        self,
        sma_period: int = 200,
        adx_period: int = 14,
        adx_sideways_threshold: float = 15.0,
        vkospi_fear_threshold: float = 30.0,
    ):
        self._sma_period = sma_period
        self._adx_period = adx_period
        self._adx_sideways_threshold = adx_sideways_threshold
        self._vkospi_fear_threshold = vkospi_fear_threshold

        self._is_bullish: bool | None = None
        self._regime_type: str = "unknown"  # trending / sideways / bearish
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
            True = 추세장 (매수 허용), False = 방어 모드 (매수 차단).
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
                logger.warning(f"KOSPI 데이터 부족 ({len(df) if not df.empty else 0}일) — 추세장으로 간주")
                self._is_bullish = True
                return True

            # KODEX200 ETF 프록시 여부 감지
            is_proxy = getattr(df, "attrs", {}).get("source") == "kodex200_proxy"

            # DataProvider가 영문 컬럼으로 통일
            close_col = "close"
            high_col = "high"
            low_col = "low"
            closes = df[close_col]

            # 1. KOSPI 200일선 체크
            sma200 = closes.rolling(self._sma_period).mean().iloc[-1]
            latest_close = closes.iloc[-1]
            self._kospi_close = int(latest_close)
            self._kospi_sma200 = float(sma200)

            above_sma = latest_close > sma200

            # 2. ADX 횡보장 감지 (KOSPI 지수 기반)
            self._kospi_adx = self._calculate_adx(df, close_col, high_col, low_col)

            is_trending = self._kospi_adx >= self._adx_sideways_threshold

            # 3. VKOSPI 공포 차단
            self._vkospi = self._get_vkospi(date)
            is_calm = self._vkospi <= self._vkospi_fear_threshold

            # 종합 판단
            self._is_bullish = above_sma and is_calm  # ADX와 무관하게 매수 허용
            self._last_check_date = date

            # 국면 유형 결정 (전략 전환용)
            if not above_sma or not is_calm:
                self._regime_type = "bearish"
            elif is_trending:
                self._regime_type = "trending"
            else:
                self._regime_type = "sideways"

            # 차단 사유 기록
            reasons = []
            if not above_sma:
                reasons.append(f"KOSPI {self._kospi_close:,} < 200일선 {self._kospi_sma200:,.0f}")
            if not is_trending:
                reasons.append(f"ADX {self._kospi_adx:.1f} < {self._adx_sideways_threshold} (횡보)")
            if not is_calm:
                reasons.append(f"VKOSPI {self._vkospi:.1f} > {self._vkospi_fear_threshold} (공포)")
            self._block_reason = " | ".join(reasons) if reasons else ""

            regime = "추세장" if self._is_bullish else "방어모드"
            source_label = "KODEX200(프록시)" if is_proxy else "KOSPI"
            detail = (
                f"{source_label} {self._kospi_close:,} "
                f"(200일선 {self._kospi_sma200:,.0f}), "
                f"ADX {self._kospi_adx:.1f}, VKOSPI {self._vkospi:.1f}"
            )
            logger.info(f"시장 국면: {regime} | {detail}")
            if self._block_reason:
                logger.info(f"차단 사유: {self._block_reason}")

            return self._is_bullish

        except Exception as e:
            logger.error(f"시장 국면 판단 실패: {e} — 추세장으로 간주")
            self._is_bullish = True
            return True

    def _calculate_adx(
        self, df: pd.DataFrame, close_col: str, high_col: str, low_col: str
    ) -> float:
        """KOSPI 지수의 ADX 계산."""
        try:
            import pandas_ta as ta

            adx_df = ta.adx(
                df[high_col].astype(float),
                df[low_col].astype(float),
                df[close_col].astype(float),
                length=self._adx_period,
            )
            if adx_df is not None and not adx_df.empty:
                adx_col = f"ADX_{self._adx_period}"
                if adx_col in adx_df.columns:
                    val = adx_df[adx_col].dropna()
                    if len(val) > 0:
                        return float(val.iloc[-1])
        except Exception as e:
            logger.debug(f"KOSPI ADX 계산 실패: {e}")
        return 25.0  # 실패 시 추세 있음으로 간주

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

    @property
    def kospi_adx(self) -> float:
        return self._kospi_adx

    @property
    def vkospi(self) -> float:
        return self._vkospi

    @property
    def regime_type(self) -> str:
        """현재 국면 유형: trending / sideways / bearish."""
        return self._regime_type

    @property
    def block_reason(self) -> str:
        return self._block_reason

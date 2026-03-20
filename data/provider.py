"""데이터 소스 추상 레이어.

KRX API / pykrx / DART API를 통합하는 DataProvider.
용도별로 최적 소스를 자동 선택:
    - 일일 운용/스크리닝: KRX API (전종목 1콜, 공식)
    - 백테스트 대량 데이터: pykrx (날짜 범위 한번에 조회)
    - 인덱스 데이터: KRX API (pykrx 인코딩 이슈 해결) → pykrx 폴백
    - 재무제표: DART API (OpenDartReader)
"""

import os
from datetime import datetime

import pandas as pd
from loguru import logger

from data.column_mapper import OHLCV_MAP, map_columns
from data.krx_api import KrxOpenAPI
from src.utils.market_calendar import get_prev_trading_day


class DataProvider:
    """데이터 소스 통합 제공자."""

    def __init__(self):
        # .env 로드 보장 (Config 없이 단독 사용 시에도 환경변수 적용)
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        self._krx = KrxOpenAPI()
        self._dart = None  # lazy init
        self._stock_name_cache: dict[str, str] = {}

    @property
    def krx_available(self) -> bool:
        return self._krx.available

    # ── OHLCV 데이터 ──

    def get_ohlcv_by_date_range(
        self, code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """종목의 날짜 범위 OHLCV 조회.

        백테스트용: pykrx 사용 (날짜 범위 한번에 조회, 효율적).
        KRX API는 일별 1콜이므로 대량 조회에 비효율.

        Args:
            code: 종목코드 (6자리).
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).

        Returns:
            DatetimeIndex OHLCV DataFrame.
        """
        try:
            from pykrx import stock
            df = stock.get_market_ohlcv_by_date(start_date, end_date, code)
            if not df.empty:
                df = map_columns(df, OHLCV_MAP)
                df.index.name = "date"
                return df
        except Exception as e:
            logger.warning(f"pykrx OHLCV 실패 ({code}): {e}")

        # 폴백: KRX API (느리지만 확실)
        if self._krx.available:
            try:
                return self._krx.get_stock_ohlcv_by_date_range(
                    code, start_date, end_date
                )
            except Exception as e:
                logger.error(f"KRX API OHLCV 폴백 실패 ({code}): {e}")

        return pd.DataFrame()

    def get_all_stocks_today(
        self, date: str | None = None, market: str = "kospi"
    ) -> pd.DataFrame:
        """특정 날짜의 전종목 OHLCV + 시총 조회.

        일일 운용/스크리닝용: KRX API 우선 (1콜로 전종목).

        Args:
            date: 기준일 (YYYYMMDD). None이면 어제 (당일 데이터 미제공).
            market: "kospi" 또는 "kosdaq".

        Returns:
            전종목 DataFrame.
        """
        # KRX API/pykrx는 당일 장마감 전 데이터 미제공 → 전 거래일로 보정
        if date is None:
            date = get_prev_trading_day().strftime("%Y%m%d")
        else:
            from datetime import date as date_type
            target = datetime.strptime(date, "%Y%m%d").date()
            today = datetime.now().date()
            if target >= today:
                date = get_prev_trading_day(today).strftime("%Y%m%d")

        if self._krx.available:
            try:
                df = self._krx.get_stocks_by_date(date, market)
                if not df.empty:
                    return df
            except Exception as e:
                logger.warning(f"KRX API 전종목 조회 실패: {e}")

        # 폴백: pykrx
        try:
            from pykrx import stock
            df = stock.get_market_ohlcv_by_date(date, date, market.upper())
            if not df.empty:
                df = map_columns(df, OHLCV_MAP)
                df.index.name = "code"
                df = df.reset_index()
                return df
        except Exception as e:
            logger.error(f"pykrx 전종목 조회 실패: {e}")

        return pd.DataFrame()

    # ── 인덱스 데이터 ──

    def get_kospi_ohlcv(
        self, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """KOSPI 지수 OHLCV 조회.

        KRX API 우선 (pykrx 인코딩 이슈 해결).

        Args:
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).

        Returns:
            DatetimeIndex OHLCV DataFrame.
        """
        # 방법 1: KRX API (공식)
        if self._krx.available:
            try:
                df = self._krx.get_index_ohlcv_range(
                    start_date, end_date, "kospi", "코스피"
                )
                if not df.empty:
                    logger.info(f"KOSPI 지수 로드 (KRX API): {len(df)}행")
                    return df
            except Exception as e:
                logger.warning(f"KRX API KOSPI 지수 실패: {e}")

        # 방법 2: pykrx index API
        try:
            from pykrx import stock
            df = stock.get_index_ohlcv_by_date(start_date, end_date, "1001")
            if not df.empty:
                df = map_columns(df, OHLCV_MAP)
                df.index.name = "date"
                logger.info(f"KOSPI 지수 로드 (pykrx): {len(df)}행")
                return df
        except Exception as e:
            logger.warning(f"pykrx KOSPI 지수 실패: {e}")

        # 방법 3: KODEX 200 ETF 프록시 (가격 스케일이 다름에 주의)
        try:
            df = self.get_ohlcv_by_date_range("069500", start_date, end_date)
            if not df.empty:
                df.attrs["source"] = "kodex200_proxy"
                logger.warning(
                    f"KOSPI 지수 대신 KODEX200 ETF 프록시 사용: {len(df)}행 "
                    "(가격 스케일이 지수와 다름)"
                )
                return df
        except Exception as e:
            logger.error(f"KODEX200 프록시 실패: {e}")

        return pd.DataFrame()

    def get_vkospi(self, date: str) -> float:
        """VKOSPI(변동성지수) 최신값 조회.

        Args:
            date: 기준일 (YYYYMMDD).

        Returns:
            VKOSPI 종가 (실패 시 0.0).
        """
        # KRX API 시도 (인코딩 이슈 없음)
        if self._krx.available:
            try:
                df = self._krx.get_index_by_date(date, "kospi")
                if not df.empty and "name" in df.columns:
                    match = df[df["name"].str.contains("VKOSPI", case=False, na=False)]
                    if not match.empty and "close" in match.columns:
                        return float(match.iloc[0]["close"])
            except Exception:
                pass

        # pykrx 폴백
        try:
            from pykrx import stock
            from datetime import timedelta
            start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
            df = stock.get_index_ohlcv_by_date(start, date, "1004")
            if not df.empty:
                close_col = "종가" if "종가" in df.columns else "close"
                return float(df[close_col].iloc[-1])
        except Exception:
            pass

        return 0.0

    # ── 종목 리스트 / 종목명 ──

    def get_ticker_list(
        self, market: str = "kospi_kosdaq", date: str | None = None
    ) -> list[str]:
        """전종목 코드 리스트."""
        if self._krx.available:
            try:
                codes = self._krx.get_ticker_list(date, market)
                if codes:
                    return codes
            except Exception as e:
                logger.warning(f"KRX API 종목 리스트 실패: {e}")

        # pykrx 폴백
        try:
            from pykrx import stock
            codes = []
            if market in ("kospi", "kospi_kosdaq"):
                codes.extend(stock.get_market_ticker_list(market="KOSPI"))
            if market in ("kosdaq", "kospi_kosdaq"):
                codes.extend(stock.get_market_ticker_list(market="KOSDAQ"))
            return codes
        except Exception as e:
            logger.error(f"pykrx 종목 리스트 실패: {e}")
            return []

    def get_stock_name(self, code: str) -> str:
        """종목명 조회 (캐시 활용)."""
        if code in self._stock_name_cache:
            return self._stock_name_cache[code]

        try:
            from pykrx import stock
            name = stock.get_market_ticker_name(code)
            if name:
                self._stock_name_cache[code] = name
                return name
        except Exception:
            pass

        return code

    # ── 시가총액 ──

    def get_market_caps(
        self, date: str, market: str = "kospi"
    ) -> dict[str, int]:
        """전종목 시가총액 조회."""
        if self._krx.available:
            try:
                caps = self._krx.get_market_caps(date, market)
                if caps:
                    return caps
            except Exception as e:
                logger.warning(f"KRX API 시총 실패: {e}")

        # pykrx 폴백
        try:
            from pykrx import stock
            df = stock.get_market_cap_by_ticker(date, market=market.upper())
            if not df.empty:
                cap_col = "시가총액" if "시가총액" in df.columns else "market_cap"
                if cap_col in df.columns:
                    return df[cap_col].to_dict()
        except Exception as e:
            logger.error(f"pykrx 시총 실패: {e}")

        return {}

    # ── 수급 데이터 ──

    def get_institutional_net_buying(
        self, code: str, days: int = 5
    ) -> tuple[int, int]:
        """기관/외국인 순매수 합계 (최근 N일).

        pykrx 전용 (KRX API에 해당 엔드포인트 없음).

        Returns:
            (기관 순매수 합, 외국인 순매수 합). 실패 시 (0, 0).
        """
        try:
            from pykrx import stock
            from datetime import timedelta
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
            df = stock.get_market_trading_value_by_date(
                start, end, code, on="순매수"
            )
            if df.empty:
                return 0, 0
            recent = df.tail(days)
            inst_col = "기관합계" if "기관합계" in recent.columns else None
            foreign_col = "외국인합계" if "외국인합계" in recent.columns else None
            inst = int(recent[inst_col].sum()) if inst_col else 0
            foreign = int(recent[foreign_col].sum()) if foreign_col else 0
            return inst, foreign
        except Exception:
            return 0, 0

    # ── DART 재무제표 ──

    def get_financials(
        self, code: str, year: int | None = None
    ) -> pd.DataFrame | None:
        """DART 재무제표 조회.

        Args:
            code: 종목코드 (6자리).
            year: 사업연도. None이면 직전 연도.

        Returns:
            재무제표 DataFrame 또는 None.
        """
        if self._dart is None:
            dart_key = os.getenv("DART_API_KEY", "")
            if not dart_key:
                logger.warning("DART_API_KEY 미설정 — 재무제표 조회 불가")
                return None
            try:
                import OpenDartReader
                self._dart = OpenDartReader.OpenDartReader(dart_key)
            except ImportError:
                logger.warning("OpenDartReader 미설치 — pip install opendartreader")
                return None

        if year is None:
            year = datetime.now().year - 1

        try:
            df = self._dart.finstate(code, year)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"DART 재무제표 조회 실패 ({code}, {year}): {e}")

        return None


# 싱글턴 인스턴스
_provider: DataProvider | None = None


def get_provider() -> DataProvider:
    """DataProvider 싱글턴 반환."""
    global _provider
    if _provider is None:
        _provider = DataProvider()
    return _provider

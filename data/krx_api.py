"""KRX Open API 클라이언트.

한국거래소 정보데이터시스템 공식 API를 통한 데이터 조회.
pykrx의 인코딩 이슈를 근본 해결하고, 공식 데이터 소스로서 안정성 확보.

엔드포인트:
    - /sto/stk_bydd_trd: KOSPI 일별매매정보 (전종목 OHLCV + 시총)
    - /sto/ksq_bydd_trd: KOSDAQ 일별매매정보
    - /sto/stk_isu_base_info: KOSPI 종목기본정보
    - /sto/ksq_isu_base_info: KOSDAQ 종목기본정보
    - /idx/kospi_dd_trd: KOSPI 지수 일별시세
    - /idx/kosdaq_dd_trd: KOSDAQ 지수 일별시세

인증: AUTH_KEY 헤더
제한: 일 10,000콜
"""

import os
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
from loguru import logger

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"

# 엔드포인트 매핑
ENDPOINTS = {
    "kospi_stocks": "/sto/stk_bydd_trd",
    "kosdaq_stocks": "/sto/ksq_bydd_trd",
    "kospi_info": "/sto/stk_isu_base_info",
    "kosdaq_info": "/sto/ksq_isu_base_info",
    "kospi_index": "/idx/kospi_dd_trd",
    "kosdaq_index": "/idx/kosdaq_dd_trd",
}

# KRX 응답 → 내부 영문 컬럼 매핑
KRX_STOCK_COLUMNS = {
    "ISU_SRT_CD": "code",       # 종목코드 (단축)
    "ISU_ABBRV": "name",        # 종목명 (약어)
    "TDD_OPNPRC": "open",
    "TDD_HGPRC": "high",
    "TDD_LWPRC": "low",
    "TDD_CLSPRC": "close",
    "ACC_TRDVOL": "volume",
    "ACC_TRDVAL": "amount",
    "MKTCAP": "market_cap",
    "FLUC_RT": "change_rate",
}

KRX_INDEX_COLUMNS = {
    "IDX_NM": "name",
    "TDD_OPNPRC": "open",
    "TDD_HGPRC": "high",
    "TDD_LWPRC": "low",
    "TDD_CLSPRC": "close",
    "ACC_TRDVOL": "volume",
    "ACC_TRDVAL": "amount",
}


class KrxOpenAPI:
    """KRX Open API 클라이언트."""

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.getenv("KRX_API_KEY", "")
        if not self._api_key:
            logger.warning("KRX_API_KEY 미설정 — KRX API 사용 불가, pykrx 폴백")
        self._session = requests.Session()
        self._session.headers.update({
            "AUTH_KEY": self._api_key.strip(),
            "Content-Type": "application/json",
        })
        self._last_call_time = 0.0
        self._min_interval = 0.5  # 최소 호출 간격 (초)

    def _request(self, endpoint: str, params: dict) -> list[dict]:
        """API 요청 실행."""
        if not self._api_key:
            raise RuntimeError("KRX_API_KEY 미설정")

        # Rate limiting
        elapsed = time.monotonic() - self._last_call_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        url = BASE_URL + endpoint
        try:
            resp = self._session.get(url, params=params, timeout=30)
            self._last_call_time = time.monotonic()

            if resp.status_code == 401:
                raise PermissionError(
                    f"KRX API 인증 실패 (401) — 해당 서비스 이용 신청 확인 필요: {endpoint}"
                )
            resp.raise_for_status()

            data = resp.json()
            return data.get("OutBlock_1", [])
        except requests.RequestException as e:
            logger.error(f"KRX API 요청 실패 ({endpoint}): {e}")
            raise

    @property
    def available(self) -> bool:
        """API 키 설정 여부."""
        return bool(self._api_key)

    # ── 주식 데이터 ──

    def get_stocks_by_date(
        self, date: str, market: str = "kospi"
    ) -> pd.DataFrame:
        """특정 날짜의 전종목 OHLCV + 시총 조회.

        Args:
            date: 기준일 (YYYYMMDD).
            market: "kospi" 또는 "kosdaq".

        Returns:
            전종목 DataFrame (code, name, open, high, low, close, volume, market_cap).
        """
        endpoint_key = f"{market}_stocks"
        if endpoint_key not in ENDPOINTS:
            raise ValueError(f"지원하지 않는 시장: {market}")

        records = self._request(ENDPOINTS[endpoint_key], {"basDd": date})
        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df = df.rename(columns={
            k: v for k, v in KRX_STOCK_COLUMNS.items() if k in df.columns
        })

        # code 컬럼 보장 (ISU_SRT_CD 우선, 없으면 ISU_CD 사용)
        if "code" not in df.columns:
            if "ISU_CD" in df.columns:
                df["code"] = df["ISU_CD"]

        # 숫자 컬럼 변환 (int64: market_cap/amount 오버플로 방지)
        numeric_cols = ["open", "high", "low", "close", "volume", "amount", "market_cap"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(",", ""), errors="coerce"
                ).fillna(0).astype("int64")

        return df

    def get_stock_ohlcv_by_date_range(
        self, code: str, start_date: str, end_date: str, market: str = "kospi"
    ) -> pd.DataFrame:
        """특정 종목의 날짜 범위 OHLCV 조회 (날짜별 루프).

        주의: 일별 1콜씩 필요하므로 백테스트 대량 호출에는 비효율적.
        일일 운용/스크리닝에 적합.

        Args:
            code: 종목코드 (6자리).
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).
            market: "kospi" 또는 "kosdaq".

        Returns:
            DatetimeIndex를 가진 OHLCV DataFrame.
        """
        from src.utils.market_calendar import is_trading_day

        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")

        rows = []
        current = start
        while current <= end:
            if not is_trading_day(current.date()):
                current += timedelta(days=1)
                continue
            date_str = current.strftime("%Y%m%d")
            try:
                df = self.get_stocks_by_date(date_str, market)
                if not df.empty and "code" in df.columns:
                    match = df[df["code"] == code]
                    if not match.empty:
                        row = match.iloc[0].to_dict()
                        row["date"] = current
                        rows.append(row)
            except Exception:
                pass
            current += timedelta(days=1)

        if not rows:
            return pd.DataFrame()

        result = pd.DataFrame(rows)
        result = result.set_index("date")
        result.index = pd.DatetimeIndex(result.index)
        result.index.name = "date"
        return result[["open", "high", "low", "close", "volume"]]

    def get_stock_info(
        self, date: str, market: str = "kospi"
    ) -> pd.DataFrame:
        """종목 기본정보 조회 (코드, 이름, 상장주식수 등).

        Args:
            date: 기준일 (YYYYMMDD).
            market: "kospi" 또는 "kosdaq".

        Returns:
            종목 정보 DataFrame.
        """
        endpoint_key = f"{market}_info"
        if endpoint_key not in ENDPOINTS:
            raise ValueError(f"지원하지 않는 시장: {market}")

        records = self._request(ENDPOINTS[endpoint_key], {"basDd": date})
        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        # 종목코드 컬럼 정규화
        if "ISU_SRT_CD" in df.columns:
            df = df.rename(columns={"ISU_SRT_CD": "code", "ISU_ABBRV": "name"})
        return df

    def get_ticker_list(
        self, date: str | None = None, market: str = "kospi_kosdaq"
    ) -> list[str]:
        """전종목 코드 리스트 조회.

        Args:
            date: 기준일 (YYYYMMDD). None이면 오늘.
            market: "kospi", "kosdaq", "kospi_kosdaq".

        Returns:
            종목코드 리스트.
        """
        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        codes = []
        if market in ("kospi", "kospi_kosdaq"):
            try:
                df = self.get_stocks_by_date(date, "kospi")
                if not df.empty and "code" in df.columns:
                    codes.extend(df["code"].tolist())
            except Exception as e:
                logger.warning(f"KRX API KOSPI 종목 리스트 실패: {e}")

        if market in ("kosdaq", "kospi_kosdaq"):
            try:
                df = self.get_stocks_by_date(date, "kosdaq")
                if not df.empty and "code" in df.columns:
                    codes.extend(df["code"].tolist())
            except Exception as e:
                logger.warning(f"KRX API KOSDAQ 종목 리스트 실패: {e}")

        return codes

    # ── 인덱스 데이터 ──

    def get_index_by_date(
        self, date: str, index_type: str = "kospi"
    ) -> pd.DataFrame:
        """특정 날짜의 인덱스 시세 조회.

        Args:
            date: 기준일 (YYYYMMDD).
            index_type: "kospi" 또는 "kosdaq".

        Returns:
            인덱스 시세 DataFrame.
        """
        endpoint_key = f"{index_type}_index"
        if endpoint_key not in ENDPOINTS:
            raise ValueError(f"지원하지 않는 인덱스: {index_type}")

        records = self._request(ENDPOINTS[endpoint_key], {"basDd": date})
        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df = df.rename(columns={
            k: v for k, v in KRX_INDEX_COLUMNS.items() if k in df.columns
        })

        numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(",", ""), errors="coerce"
                ).fillna(0)

        return df

    def get_index_ohlcv_range(
        self, start_date: str, end_date: str, index_type: str = "kospi",
        index_name: str = "코스피",
    ) -> pd.DataFrame:
        """인덱스 OHLCV 날짜 범위 조회.

        Args:
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).
            index_type: "kospi" 또는 "kosdaq".
            index_name: 필터할 인덱스 이름 (기본: "코스피").

        Returns:
            DatetimeIndex를 가진 OHLCV DataFrame.
        """
        from src.utils.market_calendar import is_trading_day

        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")

        rows = []
        current = start
        while current <= end:
            # 비거래일(주말/공휴일) 스킵 → API 호출 절감
            if not is_trading_day(current.date()):
                current += timedelta(days=1)
                continue
            date_str = current.strftime("%Y%m%d")
            try:
                df = self.get_index_by_date(date_str, index_type)
                if not df.empty and "name" in df.columns:
                    match = df[df["name"].str.contains(index_name, na=False)]
                    if not match.empty:
                        row = match.iloc[0].to_dict()
                        row["date"] = current
                        rows.append(row)
            except Exception:
                pass
            current += timedelta(days=1)

        if not rows:
            return pd.DataFrame()

        result = pd.DataFrame(rows)
        result = result.set_index("date")
        result.index = pd.DatetimeIndex(result.index)
        result.index.name = "date"
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in result.columns]
        return result[cols]

    # ── 시가총액 ──

    def get_market_caps(
        self, date: str, market: str = "kospi"
    ) -> dict[str, int]:
        """전종목 시가총액 조회.

        Args:
            date: 기준일 (YYYYMMDD).
            market: "kospi" 또는 "kosdaq".

        Returns:
            {종목코드: 시가총액(원)} 딕셔너리.
        """
        df = self.get_stocks_by_date(date, market)
        if df.empty or "code" not in df.columns or "market_cap" not in df.columns:
            return {}
        return dict(zip(df["code"], df["market_cap"]))

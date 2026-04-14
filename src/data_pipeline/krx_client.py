"""KRX OpenAPI wrapper.

Responsibilities:
- Session-level call counting against KRX daily 10,000-call limit.
- Retry with exponential backoff on network errors.
- Raw-field preservation via pydantic model with extra="allow".
- Ticker normalization to 6-digit string.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.data_pipeline.config import (
    KRX_API_KEY,
    KRX_CALL_WARN_THRESHOLD,
    KRX_DAILY_CALL_LIMIT,
)

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
ENDPOINT_KOSPI_DAILY = "/sto/stk_bydd_trd"
ENDPOINT_KOSDAQ_DAILY = "/sto/ksq_bydd_trd"
ENDPOINT_KOSPI_BASE_INFO = "/sto/stk_isu_base_info"
ENDPOINT_KOSDAQ_BASE_INFO = "/sto/ksq_isu_base_info"

MARKET_TO_ENDPOINT = {
    "KOSPI": ENDPOINT_KOSPI_DAILY,
    "KOSDAQ": ENDPOINT_KOSDAQ_DAILY,
}

MARKET_TO_BASE_INFO_ENDPOINT = {
    "KOSPI": ENDPOINT_KOSPI_BASE_INFO,
    "KOSDAQ": ENDPOINT_KOSDAQ_BASE_INFO,
}


def _normalize_ticker(raw: str) -> str:
    """Pad to 6 digits, stripping spaces. Non-numeric inputs pass through stripped."""
    s = (raw or "").strip()
    if s.isdigit():
        return s.zfill(6)
    return s


class KrxStockMeta(BaseModel):
    """One row from a KRX daily-trading response.

    Only isu_cd and isu_nm are promoted; all other fields are preserved via
    extra="allow" so schema drift does not break ingestion.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    isu_cd: str = Field(alias="ISU_CD")
    isu_nm: str = Field(alias="ISU_NM")

    @field_validator("isu_cd", mode="before")
    @classmethod
    def _normalize_isu_cd(cls, v: Any) -> str:
        return _normalize_ticker(str(v))

    @field_validator("isu_nm", mode="before")
    @classmethod
    def _strip_isu_nm(cls, v: Any) -> str:
        return str(v).strip()


class KrxIsuBaseInfo(BaseModel):
    """One row from a KRX `*_isu_base_info` response.

    Field names follow KRX raw key casing per spec. extra="allow" preserves
    any newly-added KRX fields without breaking ingestion.
    """

    model_config = ConfigDict(extra="allow")

    ISU_CD: str           # ISIN 12자리 (KR7XXXXXXXXX)
    ISU_SRT_CD: str       # 단축코드 6자리
    ISU_NM: str           # 정식명 ("삼성전자보통주")
    ISU_ABBRV: str        # 약식명 ("삼성전자")
    ISU_ENG_NM: Optional[str] = None
    LIST_DD: str          # 상장일 YYYYMMDD
    MKT_TP_NM: str        # 시장 ("KOSPI" / "KOSDAQ")
    SECUGRP_NM: Optional[str] = None
    SECT_TP_NM: Optional[str] = None       # KOSDAQ 소속부 (산업 분류 아님)
    KIND_STKCERT_TP_NM: Optional[str] = None  # 종목 유형 ("보통주" / "우선주" 등)
    PARVAL: Optional[str] = None
    LIST_SHRS: Optional[str] = None

    @field_validator("ISU_SRT_CD", mode="before")
    @classmethod
    def _normalize_short_code(cls, v: Any) -> str:
        return _normalize_ticker(str(v))


class KrxClient:
    """Thin wrapper around KRX OpenAPI with quota + retry + logging."""

    def __init__(self) -> None:
        self._api_key = KRX_API_KEY.strip()
        self._call_count = 0
        self._session = requests.Session()

    @property
    def call_count(self) -> int:
        return self._call_count

    def _bump_and_check_quota(self) -> None:
        self._call_count += 1
        if self._call_count >= KRX_DAILY_CALL_LIMIT:
            raise RuntimeError(
                f"KRX daily call limit reached: {self._call_count} >= "
                f"{KRX_DAILY_CALL_LIMIT}. Aborting to avoid API ban."
            )
        if self._call_count >= KRX_CALL_WARN_THRESHOLD:
            logger.warning(
                f"KRX call count {self._call_count} approaching daily "
                f"limit {KRX_DAILY_CALL_LIMIT}"
            )

    def _request(self, endpoint: str, params: dict) -> dict:
        """GET with retry. Returns decoded JSON body."""
        url = BASE_URL + endpoint
        headers = {"AUTH_KEY": self._api_key}

        backoff = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            self._bump_and_check_quota()
            started = time.monotonic()
            try:
                resp = self._session.get(
                    url, headers=headers, params=params, timeout=30
                )
                elapsed = time.monotonic() - started
                logger.info(
                    f"KRX {endpoint} params={params} "
                    f"status={resp.status_code} elapsed={elapsed:.2f}s "
                    f"calls={self._call_count}"
                )
                resp.raise_for_status()
                body = resp.json()
                logger.debug(
                    f"KRX {endpoint} body[:1500]={str(body)[:1500]}"
                )
                return body
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(
                    f"KRX {endpoint} attempt {attempt}/3 failed: {exc}"
                )
                if attempt < 3:
                    time.sleep(backoff)
                    backoff *= 2

        logger.error(f"KRX {endpoint} failed after 3 attempts: {last_exc}")
        assert last_exc is not None
        raise last_exc

    def get_listed_stocks(
        self,
        market: str,
        base_date: Optional[str] = None,
    ) -> List[KrxStockMeta]:
        """Return listed stocks for KOSPI or KOSDAQ as of base_date (YYYYMMDD).

        If base_date is None, caller should pass the latest trading day
        externally; this wrapper does not auto-detect holidays. The KRX API
        returns an empty list for non-trading days.
        """
        if market not in MARKET_TO_ENDPOINT:
            raise ValueError(
                f"Unsupported market: {market!r}. Expected one of "
                f"{list(MARKET_TO_ENDPOINT)}"
            )
        if base_date is None:
            raise ValueError(
                "base_date (YYYYMMDD) is required — pass a known trading day."
            )

        body = self._request(
            MARKET_TO_ENDPOINT[market], params={"basDd": base_date}
        )
        rows = body.get("OutBlock_1", [])
        return [KrxStockMeta.model_validate(r) for r in rows]

    def get_stock_base_info(
        self,
        market: str,
        base_date: Optional[str] = None,
    ) -> List[KrxIsuBaseInfo]:
        """Return stock base info (LIST_DD, ISIN, KIND_STKCERT_TP_NM, ...) for market.

        Endpoints:
            KOSPI:  /sto/stk_isu_base_info
            KOSDAQ: /sto/ksq_isu_base_info
        """
        if market not in MARKET_TO_BASE_INFO_ENDPOINT:
            raise ValueError(
                f"Unsupported market: {market!r}. Expected one of "
                f"{list(MARKET_TO_BASE_INFO_ENDPOINT)}"
            )
        if base_date is None:
            raise ValueError(
                "base_date (YYYYMMDD) is required — pass a known trading day."
            )

        body = self._request(
            MARKET_TO_BASE_INFO_ENDPOINT[market], params={"basDd": base_date}
        )
        rows = body.get("OutBlock_1", [])
        return [KrxIsuBaseInfo.model_validate(r) for r in rows]

"""Collect current KOSPI + KOSDAQ stock meta from KRX OpenAPI into stocks table.

Flow:
    1. Resolve latest trading day (simple lookback from today).
    2. Call KRX for KOSPI and KOSDAQ listings.
    3. Classify stock_type, infer parent_ticker for preferreds.
    4. INSERT OR REPLACE into stocks via shared connection.
    5. Print summary report.
"""
from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Windows cp949 환경에서 UTF-8 출력 강제
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.db import get_connection
from src.data_pipeline.krx_client import KrxClient, KrxStockMeta
from src.data_pipeline.preferred_parent_map import MANUAL_PARENT_MAP


PREFERRED_NAME_PATTERNS = [
    re.compile(r"우$"),
    re.compile(r"우B$"),
    re.compile(r"우\(전환\)$"),
    re.compile(r"\d우$"),
    re.compile(r"\d우B$"),
    re.compile(r"\d우\(전환\)$"),
]

_PREFERRED_SUFFIX_STRIP_PATTERNS = [
    re.compile(r"\d*우\(전환\)$"),
    re.compile(r"\d*우B$"),
    re.compile(r"\d*우$"),
]


def is_preferred(name: str, ticker: str) -> bool:
    """우선주 판별.

    한국 우선주 종목코드 규칙: 마지막 자리가 0이면 보통주.
    (예: 005930 보통주 / 005935 우선주 / 00104K 신형 우선주)

    종목코드 규칙 + 종목명 규칙 AND 조건.
    """
    if ticker[-1] == "0":
        return False
    return any(pat.search(name) for pat in PREFERRED_NAME_PATTERNS)


def classify_stock_type(name: str, ticker: str) -> str:
    """Determine stock_type from name/ticker.

    Priority:
        1. SPAC     — name contains '스팩'
        2. REIT     — name contains '리츠' or '위탁관리부동산투자회사'
        3. PREFERRED — ticker + name AND rule (see is_preferred)
        4. COMMON   — default
    """
    n = name.strip()
    if "스팩" in n:
        return "SPAC"
    if "리츠" in n or "위탁관리부동산투자회사" in n:
        return "REIT"
    if is_preferred(n, ticker):
        return "PREFERRED"
    return "COMMON"


def _strip_preferred_suffix(name: str) -> str:
    """우선주 접미사 제거해서 기본명 추출.

    삼성전자우 → 삼성전자
    CJ4우(전환) → CJ
    BYC우 → BYC
    """
    for pat in _PREFERRED_SUFFIX_STRIP_PATTERNS:
        name = pat.sub("", name)
    return name.strip()


def guess_parent_ticker(
    ticker: str,
    name: str,
    all_stocks: dict[str, dict],
) -> Optional[str]:
    """우선주의 보통주 ticker 추정. 3단계 fallback.

    1. 종목명 기반: 우선주 접미사 제거한 basename과 정확히 일치하는 COMMON 검색 (같은 시장)
    2. 종목코드 규칙: 마지막 자리를 0으로 바꾼 ticker가 COMMON으로 존재하는지 확인
    3. 수동 매핑: MANUAL_PARENT_MAP 참조
    """
    current_market = all_stocks.get(ticker, {}).get("market")

    basename = _strip_preferred_suffix(name)
    if basename:
        for cand_ticker, cand_info in all_stocks.items():
            if (
                cand_info["stock_type"] == "COMMON"
                and cand_info["market"] == current_market
                and cand_info["name"] == basename
            ):
                return cand_ticker

    candidate_ticker = ticker[:-1] + "0"
    if candidate_ticker in all_stocks:
        cand_info = all_stocks[candidate_ticker]
        if cand_info["stock_type"] == "COMMON":
            return candidate_ticker

    if ticker in MANUAL_PARENT_MAP:
        mapped = MANUAL_PARENT_MAP[ticker]
        if mapped in all_stocks:
            return mapped
        logger.warning(
            f"Manual mapping {ticker}→{mapped} but {mapped} not in stocks"
        )
        return None

    return None


def _latest_krx_trading_day(client: KrxClient) -> str:
    """Return YYYYMMDD for most recent KRX trading day by lookback probe.

    Uses KOSPI endpoint; KRX returns empty OutBlock_1 on non-trading days.
    """
    today = date.today()
    for delta in range(0, 10):
        probe = today - timedelta(days=delta)
        ymd = probe.strftime("%Y%m%d")
        rows = client.get_listed_stocks("KOSPI", base_date=ymd)
        if rows:
            logger.info(f"Latest trading day resolved: {ymd} ({len(rows)} KOSPI rows)")
            return ymd
        logger.info(f"{ymd} returned empty; trying previous day")
    raise RuntimeError("Could not find a trading day in last 10 days")


def _extract_sector(meta: KrxStockMeta) -> Optional[str]:
    """Best-effort sector extraction from KRX raw fields (preserved via extra)."""
    raw = meta.model_dump()
    for key in ("IDX_IND_NM", "SECT_TP_NM", "KSQ_SECT_TP_NM"):
        v = raw.get(key)
        if v and str(v).strip():
            return str(v).strip()
    return None


def _sample_by_type(rows: list[dict], stype: str, n: int = 3) -> list[dict]:
    return [r for r in rows if r["stock_type"] == stype][:n]


def main() -> int:
    started = time.monotonic()
    client = KrxClient()

    base_date = _latest_krx_trading_day(client)

    kospi = client.get_listed_stocks("KOSPI", base_date=base_date)
    kosdaq = client.get_listed_stocks("KOSDAQ", base_date=base_date)
    logger.info(f"KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

    records: list[dict] = []
    for meta, market in [(m, "KOSPI") for m in kospi] + [
        (m, "KOSDAQ") for m in kosdaq
    ]:
        stype = classify_stock_type(meta.isu_nm, meta.isu_cd)
        records.append(
            {
                "ticker": meta.isu_cd,
                "name": meta.isu_nm,
                "market": market,
                "sector": _extract_sector(meta),
                "stock_type": stype,
                "parent_ticker": None,
            }
        )

    all_stocks = {r["ticker"]: r for r in records}

    for r in records:
        if r["stock_type"] == "PREFERRED":
            r["parent_ticker"] = guess_parent_ticker(
                r["ticker"], r["name"], all_stocks
            )

    now_iso = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO stocks (
                ticker, name, market, sector, stock_type, parent_ticker,
                listed_date, delisted_date, delisting_reason,
                first_candle_date, last_candle_date, last_updated
            ) VALUES (
                :ticker, :name, :market, :sector, :stock_type, :parent_ticker,
                NULL, NULL, NULL, NULL, NULL, :last_updated
            )
            """,
            [{**r, "last_updated": now_iso} for r in records],
        )

    elapsed = time.monotonic() - started

    by_market = {"KOSPI": 0, "KOSDAQ": 0}
    by_type: dict[str, int] = {}
    for r in records:
        by_market[r["market"]] += 1
        by_type[r["stock_type"]] = by_type.get(r["stock_type"], 0) + 1

    print()
    print("====================================")
    print("Phase 1 Step 1a — Stocks Meta Report")
    print("====================================")
    print()
    print("Market distribution:")
    print(f"  KOSPI:  {by_market['KOSPI']} stocks")
    print(f"  KOSDAQ: {by_market['KOSDAQ']} stocks")
    print(f"  Total:  {len(records)} stocks")
    print()
    print("Stock type distribution:")
    for t in ("COMMON", "PREFERRED", "SPAC", "REIT", "OTHER"):
        print(f"  {t+':':<10} {by_type.get(t, 0)}")
    print()
    print("Samples by type:")
    for t in ("COMMON", "PREFERRED", "SPAC", "REIT"):
        samples = _sample_by_type(records, t, n=3)
        if not samples:
            continue
        print(f"  [{t}]")
        for s in samples:
            pt = f"  parent={s['parent_ticker']}" if s["parent_ticker"] else ""
            print(f"    {s['ticker']}  {s['name']:<18} {s['market']}{pt}")
    print()
    print("Collection stats:")
    print(f"  KRX API calls:   {client.call_count}")
    print(f"  Elapsed:         {elapsed:.1f}s")
    print(f"  Saved to stocks: {len(records)} rows")
    print()

    unresolved = [
        r for r in records if r["stock_type"] == "PREFERRED" and not r["parent_ticker"]
    ]
    if unresolved:
        print(f"Preferreds without resolved parent: {len(unresolved)}")
        for s in unresolved[:10]:
            print(f"  {s['ticker']}  {s['name']}  ({s['market']})")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())

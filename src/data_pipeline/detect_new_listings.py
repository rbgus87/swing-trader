"""신규 상장 종목 감지 + stocks 테이블 추가.

KRX OpenAPI로 현재 상장 종목 조회 → stocks에 없는 ticker만 INSERT.
일일 갱신 파이프라인의 첫 단계.
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.collect_stocks_meta import (
    _latest_krx_trading_day,
    classify_stock_type,
    guess_parent_ticker,
)
from src.data_pipeline.db import get_connection
from src.data_pipeline.krx_client import KrxClient


def fetch_current_listings(client: KrxClient, base_date: str) -> list[dict]:
    """KRX bydd_trd로 현재 상장 종목 조회."""
    kospi = client.get_listed_stocks("KOSPI", base_date=base_date)
    kosdaq = client.get_listed_stocks("KOSDAQ", base_date=base_date)
    logger.info(f"KRX listings: KOSPI={len(kospi)}, KOSDAQ={len(kosdaq)}")

    rows = []
    for meta, market in [(m, "KOSPI") for m in kospi] + [(m, "KOSDAQ") for m in kosdaq]:
        stype = classify_stock_type(meta.isu_nm, meta.isu_cd)
        rows.append({
            "ticker": meta.isu_cd,
            "name": meta.isu_nm,
            "market": market,
            "stock_type": stype,
        })
    return rows


def detect_new(rows: list[dict]) -> list[dict]:
    """stocks에 없는 ticker만 필터링."""
    with get_connection() as conn:
        cursor = conn.execute("SELECT ticker FROM stocks")
        existing = {r[0] for r in cursor.fetchall()}
    return [r for r in rows if r["ticker"] not in existing]


def insert_new_listings(new_rows: list[dict], all_current: list[dict]) -> int:
    """신규 종목을 stocks에 INSERT. parent_ticker 추정 포함."""
    if not new_rows:
        return 0

    # guess_parent_ticker에는 전체 현재 listings가 필요 (우선주 부모 찾기)
    all_stocks_map = {
        r["ticker"]: {
            "name": r["name"],
            "market": r["market"],
            "stock_type": r["stock_type"],
        }
        for r in all_current
    }

    now_iso = datetime.now().isoformat(timespec="seconds")
    today_iso = date.today().isoformat()

    inserted = 0
    with get_connection() as conn:
        for r in new_rows:
            parent = None
            if r["stock_type"] == "PREFERRED":
                parent = guess_parent_ticker(r["ticker"], r["name"], all_stocks_map)

            conn.execute(
                """
                INSERT OR IGNORE INTO stocks (
                    ticker, name, market, sector, stock_type, parent_ticker,
                    listed_date, delisted_date, delisting_reason,
                    first_candle_date, last_candle_date, last_updated
                ) VALUES (
                    ?, ?, ?, NULL, ?, ?,
                    ?, NULL, NULL, NULL, NULL, ?
                )
                """,
                (r["ticker"], r["name"], r["market"], r["stock_type"], parent,
                 today_iso, now_iso),
            )
            if conn.total_changes > inserted:
                inserted = conn.total_changes
    return inserted


def main() -> int:
    started = time.monotonic()
    logger.info("=" * 50)
    logger.info("Daily Update — Step 1: Detect New Listings")
    logger.info("=" * 50)

    client = KrxClient()
    base_date = _latest_krx_trading_day(client)

    current = fetch_current_listings(client, base_date)
    new_rows = detect_new(current)

    logger.info(f"Current KRX listings: {len(current)}")
    logger.info(f"New (not in stocks):  {len(new_rows)}")

    if new_rows:
        for r in new_rows[:10]:
            logger.info(f"  NEW: {r['ticker']} {r['name']:<20} {r['market']} type={r['stock_type']}")
        if len(new_rows) > 10:
            logger.info(f"  ... and {len(new_rows) - 10} more")

    inserted = insert_new_listings(new_rows, current)

    elapsed = time.monotonic() - started
    logger.info(f"Inserted: {inserted} rows")
    logger.info(f"KRX API calls: {client.call_count}")
    logger.info(f"Elapsed: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

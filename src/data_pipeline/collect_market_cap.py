"""Phase 1 Step 3b — 시가총액 이력 전종목 백필.

KRX bydd_trd 엔드포인트에서 2014-01-01 ~ 오늘 범위의 거래일별 전종목
시가총액(MKTCAP)과 상장주식수(LIST_SHRS)를 수집하여 market_cap_history에 저장.

- 거래일 소스: daily_candles의 distinct date (이미 검증됨)
- 재시작: market_cap_collection_log 기반 체크포인트
- 미지 종목: stocks에 자동 INSERT (우선주 등)
- 누락 종목: data_anomaly_log에 MISSING_IN_HISTORICAL_SNAPSHOT 기록

Usage:
    python src/data_pipeline/collect_market_cap.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger  # noqa: E402

from src.data_pipeline.db import get_connection  # noqa: E402
from src.data_pipeline.import_delisted_list import classify_stock_type  # noqa: E402
from src.data_pipeline.krx_client import KrxClient  # noqa: E402

MARKETS = ["KOSPI", "KOSDAQ"]
CUTOFF_DATE = "2014-01-01"


# ============================================================
# 거래일 리스트 추출
# ============================================================

def get_trading_dates(cutoff_date: str = CUTOFF_DATE) -> list[str]:
    """daily_candles에서 고유 거래일 추출. sorted YYYY-MM-DD."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_candles WHERE date >= ? ORDER BY date",
            (cutoff_date,),
        ).fetchall()
        return [row["date"] for row in rows]


def get_pending_jobs(trading_dates: list[str]) -> list[tuple[str, str]]:
    """수집 대기 (market, date) 조합 반환. SUCCESS인 것 제외."""
    all_jobs = [(m, d) for d in trading_dates for m in MARKETS]

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT market, date FROM market_cap_collection_log WHERE status = 'SUCCESS'"
        ).fetchall()
        completed = {(row["market"], row["date"]) for row in rows}

    return [job for job in all_jobs if job not in completed]


# ============================================================
# 미지 종목 자동 INSERT
# ============================================================

def ensure_ticker_in_stocks(conn, ticker: str, name: str, market: str) -> None:
    """stocks에 없는 ticker면 자동 INSERT (우선주 등)."""
    existing = conn.execute(
        "SELECT ticker FROM stocks WHERE ticker = ?", (ticker,)
    ).fetchone()
    if existing is not None:
        return

    stock_type = classify_stock_type(name, ticker)
    conn.execute(
        """INSERT INTO stocks
           (ticker, name, market, stock_type, parent_ticker,
            listed_date, delisted_date, delisting_reason, last_updated)
           VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)""",
        (ticker, name, market, stock_type, datetime.now().isoformat()),
    )
    logger.debug(f"Auto-inserted {ticker} {name} ({market}, {stock_type})")


# ============================================================
# 일자별 수집
# ============================================================

def collect_one_day(
    client: KrxClient, conn, market: str, date_str: str
) -> dict:
    """특정 (market, date) 수집. Returns status dict."""
    bas_dd = date_str.replace("-", "")

    try:
        records = client.get_listed_stocks(market, bas_dd)
    except Exception as e:
        return {
            "status": "FAILED",
            "rows_saved": 0,
            "error_message": str(e)[:500],
            "missing_count": 0,
        }

    if not records:
        return {
            "status": "SKIPPED",
            "rows_saved": 0,
            "error_message": "Empty response (holiday?)",
            "missing_count": 0,
        }

    # 응답 ticker 집합 (isu_cd는 bydd_trd에서 6자리 단축코드)
    response_tickers: set[str] = set()
    rows_saved = 0

    for r in records:
        ticker = r.isu_cd
        if not ticker or len(ticker) != 6:
            continue

        extras = r.model_extra or {}
        mktcap_raw = extras.get("MKTCAP", "")
        list_shrs_raw = extras.get("LIST_SHRS", "")

        try:
            mktcap = int(mktcap_raw) if mktcap_raw and mktcap_raw != "0" else None
            list_shrs = int(list_shrs_raw) if list_shrs_raw and list_shrs_raw != "0" else None
        except (ValueError, TypeError):
            continue

        if mktcap is None or list_shrs is None:
            continue

        response_tickers.add(ticker)
        ensure_ticker_in_stocks(conn, ticker, r.isu_nm, market)

        conn.execute(
            """INSERT OR REPLACE INTO market_cap_history
               (ticker, date, market_cap, shares_outstanding)
               VALUES (?, ?, ?, ?)""",
            (ticker, date_str, mktcap, list_shrs),
        )
        rows_saved += 1

    # 누락 종목 탐지 (stocks에는 있는데 응답에 없음)
    expected_rows = conn.execute(
        """SELECT ticker, name FROM stocks
           WHERE market = ?
             AND (listed_date IS NULL OR listed_date <= ?)
             AND (delisted_date IS NULL OR delisted_date > ?)""",
        (market, date_str, date_str),
    ).fetchall()
    expected_tickers = {row["ticker"] for row in expected_rows}
    expected_map = {row["ticker"]: row["name"] for row in expected_rows}
    missing = expected_tickers - response_tickers

    # 누락 기록 (샘플 10건만 — 과도한 anomaly 방지)
    for ticker in list(missing)[:10]:
        conn.execute(
            """INSERT OR IGNORE INTO data_anomaly_log
               (ticker, date, anomaly_type, details, severity, detected_at)
               VALUES (?, ?, 'MISSING_IN_HISTORICAL_SNAPSHOT', ?, 'INFO', ?)""",
            (
                ticker,
                date_str,
                json.dumps({"market": market, "name": expected_map.get(ticker, "?")}),
                datetime.now().isoformat(),
            ),
        )

    return {
        "status": "SUCCESS",
        "rows_saved": rows_saved,
        "error_message": None,
        "missing_count": len(missing),
    }


def update_collection_log(conn, market: str, date_str: str, result: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO market_cap_collection_log
           (market, date, attempted_at, status, rows_saved, error_message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            market,
            date_str,
            datetime.now().isoformat(),
            result["status"],
            result["rows_saved"],
            result["error_message"],
        ),
    )


# ============================================================
# 메인
# ============================================================

def main() -> int:
    logger.info("=" * 50)
    logger.info("Phase 1 Step 3b — Market Cap Backfill")
    logger.info("=" * 50)

    client = KrxClient()

    trading_dates = get_trading_dates()
    logger.info(f"Trading dates in range: {len(trading_dates)}")
    logger.info(f"Range: {trading_dates[0]} ~ {trading_dates[-1]}")

    pending_jobs = get_pending_jobs(trading_dates)
    total_jobs = len(trading_dates) * len(MARKETS)
    logger.info(f"Pending jobs: {len(pending_jobs)} / {total_jobs}")

    if not pending_jobs:
        logger.info("All jobs already completed. Nothing to do.")
        return 0

    stats = {
        "start_time": time.time(),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "total_rows": 0,
        "total_missing": 0,
        "auto_inserted": 0,
    }

    with get_connection() as conn:
        # 자동 INSERT 전 stocks 수 기록
        before_count = conn.execute("SELECT COUNT(*) as c FROM stocks").fetchone()["c"]

        for idx, (market, date_str) in enumerate(pending_jobs, 1):
            result = collect_one_day(client, conn, market, date_str)
            update_collection_log(conn, market, date_str, result)

            if result["status"] == "SUCCESS":
                stats["success"] += 1
                stats["total_rows"] += result["rows_saved"]
                stats["total_missing"] += result["missing_count"]
            elif result["status"] == "FAILED":
                stats["failed"] += 1
                logger.warning(
                    f"FAILED {market} {date_str}: {result['error_message']}"
                )
            else:
                stats["skipped"] += 1

            # 진행률 로그 (100개마다)
            if idx % 100 == 0:
                elapsed = time.time() - stats["start_time"]
                rate = idx / elapsed if elapsed > 0 else 0
                remaining = (len(pending_jobs) - idx) / rate if rate > 0 else 0
                logger.info(
                    f"Progress: {idx}/{len(pending_jobs)} "
                    f"({idx / len(pending_jobs) * 100:.1f}%) "
                    f"| S={stats['success']} F={stats['failed']} SK={stats['skipped']} "
                    f"| rows={stats['total_rows']:,} "
                    f"| {rate:.1f} jobs/s "
                    f"| ETA: {remaining / 60:.1f} min"
                )

            # 주기적 commit (50 jobs마다)
            if idx % 50 == 0:
                conn.commit()

        # 최종 commit은 get_connection context manager가 처리

        after_count = conn.execute("SELECT COUNT(*) as c FROM stocks").fetchone()["c"]
        stats["auto_inserted"] = after_count - before_count

    elapsed = time.time() - stats["start_time"]
    _print_report(stats, elapsed, len(pending_jobs))
    return 0


def _print_report(stats: dict, elapsed: float, total: int) -> None:
    logger.info("=" * 50)
    logger.info("Market Cap Backfill Report")
    logger.info("=" * 50)
    logger.info(f"Total jobs:          {total}")
    logger.info(f"  SUCCESS:           {stats['success']}")
    logger.info(f"  FAILED:            {stats['failed']}")
    logger.info(f"  SKIPPED (holiday): {stats['skipped']}")
    logger.info(f"Rows saved:          {stats['total_rows']:,}")
    logger.info(f"Missing tickers:     {stats['total_missing']} (anomaly log)")
    logger.info(f"Auto-inserted stocks: {stats['auto_inserted']}")
    logger.info(f"Elapsed:             {elapsed / 60:.1f} min ({elapsed:.0f}s)")


if __name__ == "__main__":
    sys.exit(main())

"""Phase 1 Step 2 — 전종목 일봉 백필 (병렬 3 동시).

Producer: ThreadPoolExecutor(max_workers=3) 로 FDR 호출
Consumer: 단일 writer 스레드가 Queue에서 꺼내 DB 저장
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.anomaly_detector import detect_anomalies, filter_invalid_rows
from src.data_pipeline.db import get_connection
from src.data_pipeline.fdr_client import FdrClient
from src.data_pipeline.rate_limiter import RateLimiter


EARLIEST_COLLECT_DATE = date(2014, 1, 1)
MAX_WORKERS = 3
RATE_LIMIT_PER_SEC = 5
CONSECUTIVE_FAILURE_THRESHOLD = 10
FAILURE_COOLDOWN_SECONDS = 60

result_queue: queue.Queue = queue.Queue(maxsize=50)
SENTINEL = None


def fetch_ticker_data(
    client: FdrClient,
    rate_limiter: RateLimiter,
    ticker: str,
    listed_date_str: Optional[str],
    delisted_date_str: Optional[str],
) -> dict:
    """단일 종목 FDR 호출 (Producer). DB 접근 없음.

    폐지 종목이면 delisted_date까지 수집, 아니면 오늘까지.
    """
    rate_limiter.wait()

    if listed_date_str:
        listed = date.fromisoformat(listed_date_str)
        start_date = max(listed, EARLIEST_COLLECT_DATE)
    else:
        start_date = EARLIEST_COLLECT_DATE

    if delisted_date_str:
        end_date = date.fromisoformat(delisted_date_str)
    else:
        end_date = date.today()

    df = client.get_daily_ohlcv(ticker, start_date, end_date)

    return {
        "ticker": ticker,
        "listed_date_str": listed_date_str,
        "delisted_date_str": delisted_date_str,
        "df": df,
        "start_date": start_date,
        "end_date": end_date,
    }


def db_writer_thread(stats: dict, total: int) -> None:
    """Consumer: 단일 스레드로 DB 쓰기 전담."""
    processed = 0

    with get_connection() as conn:
        while True:
            item = result_queue.get()

            if item is SENTINEL:
                result_queue.task_done()
                break

            ticker = item["ticker"]
            df = item["df"]

            try:
                anomalies = detect_anomalies(ticker, df)
                for anom in anomalies:
                    conn.execute(
                        """
                        INSERT INTO data_anomaly_log
                        (ticker, date, anomaly_type, details, severity, detected_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            anom["ticker"],
                            anom["date"],
                            anom["anomaly_type"],
                            anom["details"],
                            anom["severity"],
                            datetime.now().isoformat(),
                        ),
                    )

                df_clean = filter_invalid_rows(df)

                rows_saved = 0
                if not df_clean.empty:
                    for idx, row in df_clean.iterrows():
                        d = (
                            idx.date().isoformat()
                            if hasattr(idx, "date")
                            else str(idx)[:10]
                        )
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO daily_candles
                            (ticker, date, open, high, low, close, volume, change_rate)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                ticker,
                                d,
                                float(row["Open"]),
                                float(row["High"]),
                                float(row["Low"]),
                                float(row["Close"]),
                                int(row["Volume"]),
                                float(row.get("Change", 0))
                                if pd.notna(row.get("Change", 0))
                                else None,
                            ),
                        )
                        rows_saved += 1

                start_date = item["start_date"]
                end_date = item["end_date"]
                expected_min = (end_date - start_date).days * 245 / 365 * 0.5

                if df.empty:
                    status = "FAILED"
                    error_msg = "FDR returned empty"
                elif rows_saved == 0:
                    status = "FAILED"
                    error_msg = "All rows filtered as invalid"
                elif rows_saved < expected_min:
                    status = "PARTIAL"
                    error_msg = None
                else:
                    status = "SUCCESS"
                    error_msg = None

                last_date = None
                if rows_saved > 0:
                    cursor = conn.execute(
                        "SELECT MAX(date) FROM daily_candles WHERE ticker = ?",
                        (ticker,),
                    )
                    last_date = cursor.fetchone()[0]

                    # listed_date 역산: NULL인 경우 첫 거래일로 채움
                    existing = conn.execute(
                        "SELECT listed_date FROM stocks WHERE ticker = ?",
                        (ticker,),
                    ).fetchone()
                    if existing and existing["listed_date"] is None:
                        first_date = conn.execute(
                            "SELECT MIN(date) FROM daily_candles WHERE ticker = ?",
                            (ticker,),
                        ).fetchone()[0]
                        if first_date:
                            conn.execute(
                                """
                                UPDATE stocks
                                SET listed_date = ?,
                                    last_updated = datetime('now')
                                WHERE ticker = ?
                                """,
                                (first_date, ticker),
                            )

                conn.execute(
                    """
                    INSERT OR REPLACE INTO collection_log
                    (ticker, data_type, last_collected_date, last_attempt_at,
                     status, error_message, retry_count)
                    VALUES (?, 'daily_candle', ?, ?, ?, ?,
                            COALESCE((SELECT retry_count FROM collection_log
                                      WHERE ticker = ? AND data_type = 'daily_candle'), 0) + ?)
                    """,
                    (
                        ticker,
                        last_date,
                        datetime.now().isoformat(),
                        status,
                        error_msg,
                        ticker,
                        1 if status in ("FAILED", "PARTIAL") else 0,
                    ),
                )

                stats["rows_saved"] += rows_saved
                stats["anomalies"] += len(anomalies)
                if status == "SUCCESS":
                    stats["success"] += 1
                    stats["consecutive_failures"] = 0
                elif status == "PARTIAL":
                    stats["partial"] += 1
                else:
                    stats["failed"] += 1
                    stats["consecutive_failures"] += 1

                conn.commit()

            except Exception as e:
                logger.error(f"DB write error for {ticker}: {e}")
                stats["failed"] += 1
                stats["consecutive_failures"] += 1

            processed += 1

            if processed % 100 == 0:
                elapsed = time.time() - stats["start_time"]
                remaining = (
                    (total - processed) * (elapsed / processed)
                    if processed > 0
                    else 0
                )
                logger.info(
                    f"Progress: {processed}/{total} "
                    f"({processed/total*100:.1f}%) "
                    f"| success={stats['success']}, partial={stats['partial']}, "
                    f"failed={stats['failed']} "
                    f"| rows={stats['rows_saved']:,} "
                    f"| ETA: {remaining/60:.1f} min"
                )

            result_queue.task_done()


def get_tickers_to_collect(force_resume: bool = False) -> list[tuple]:
    with get_connection() as conn:
        if force_resume:
            cursor = conn.execute(
                "SELECT ticker, listed_date, delisted_date FROM stocks ORDER BY ticker"
            )
        else:
            cursor = conn.execute(
                """
                SELECT s.ticker, s.listed_date, s.delisted_date
                FROM stocks s
                LEFT JOIN collection_log c
                    ON s.ticker = c.ticker AND c.data_type = 'daily_candle'
                WHERE c.status IS NULL OR c.status != 'SUCCESS'
                ORDER BY s.ticker
                """
            )
        return [
            (row["ticker"], row["listed_date"], row["delisted_date"])
            for row in cursor.fetchall()
        ]


def get_tickers_for_incremental() -> list[tuple]:
    """증분 모드: 모든 active 종목 + start_date = last_candle_date + 1일.

    candle이 없는 active 종목은 listed_date부터 수집 (신규 상장).
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT s.ticker, s.listed_date, s.delisted_date,
                   MAX(c.date) as last_candle_date
            FROM stocks s
            LEFT JOIN daily_candles c ON s.ticker = c.ticker
            WHERE s.delisted_date IS NULL
            GROUP BY s.ticker
            ORDER BY s.ticker
            """
        )
        result = []
        for row in cursor.fetchall():
            last = row["last_candle_date"]
            if last:
                next_start = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
                result.append((row["ticker"], next_start, row["delisted_date"]))
            else:
                result.append((row["ticker"], row["listed_date"], row["delisted_date"]))
        return result


def main(force_resume: bool = False, incremental: bool = False) -> None:
    logger.info("=" * 50)
    mode = "INCREMENTAL" if incremental else ("FORCE_RESUME" if force_resume else "BACKFILL")
    logger.info(f"Daily Candles — {mode}")
    logger.info("=" * 50)
    logger.info(f"Workers: {MAX_WORKERS}, Rate limit: {RATE_LIMIT_PER_SEC}/sec")
    logger.info(
        f"Failure threshold: {CONSECUTIVE_FAILURE_THRESHOLD}, "
        f"Cooldown: {FAILURE_COOLDOWN_SECONDS}s"
    )

    client = FdrClient()
    rate_limiter = RateLimiter(RATE_LIMIT_PER_SEC)

    if incremental:
        tickers = get_tickers_for_incremental()
        # DB의 최대 거래일 기준으로 skip (장 열린 최신 날짜)
        with get_connection() as conn:
            max_db_date = conn.execute(
                "SELECT MAX(date) FROM daily_candles"
            ).fetchone()[0]
        before = len(tickers)
        tickers = [t for t in tickers
                   if (t[1] is None) or (t[1] <= max_db_date)]
        skipped_up_to_date = before - len(tickers)
        logger.info(
            f"Incremental: {len(tickers)} to fetch "
            f"(max_db_date={max_db_date}, skipped up-to-date: {skipped_up_to_date})"
        )
    else:
        tickers = get_tickers_to_collect(force_resume)
        with get_connection() as conn:
            skipped = conn.execute(
                """
                SELECT COUNT(*) FROM collection_log
                WHERE data_type = 'daily_candle' AND status = 'SUCCESS'
                """
            ).fetchone()[0]
        logger.info(f"Total to collect: {len(tickers)} (already success: {skipped})")

    total = len(tickers)

    if total == 0:
        logger.info("Nothing to collect.")
        return

    stats = {
        "start_time": time.time(),
        "success": 0,
        "partial": 0,
        "failed": 0,
        "rows_saved": 0,
        "anomalies": 0,
        "consecutive_failures": 0,
        "cooldown_events": 0,
    }

    writer = threading.Thread(
        target=db_writer_thread,
        args=(stats, total),
        daemon=False,
    )
    writer.start()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_ticker_data, client, rate_limiter, t, ld, dd): t
            for t, ld, dd in tickers
        }

        for future in as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result()
                result_queue.put(result)

                if stats["consecutive_failures"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    stats["cooldown_events"] += 1
                    logger.warning(
                        f"Consecutive failures: {stats['consecutive_failures']}. "
                        f"Entering cooldown ({FAILURE_COOLDOWN_SECONDS}s). "
                        f"Event #{stats['cooldown_events']}"
                    )
                    time.sleep(FAILURE_COOLDOWN_SECONDS)
                    stats["consecutive_failures"] = 0
                    logger.info("Cooldown finished, resuming")

            except Exception as e:
                logger.error(f"Fetch error for {ticker}: {e}")
                result_queue.put(
                    {
                        "ticker": ticker,
                        "listed_date_str": None,
                        "df": pd.DataFrame(),
                        "start_date": EARLIEST_COLLECT_DATE,
                        "end_date": date.today(),
                    }
                )

    result_queue.put(SENTINEL)
    writer.join()

    elapsed = time.time() - stats["start_time"]
    print_report(stats, elapsed, total)


def print_report(stats: dict, elapsed: float, total: int) -> None:
    logger.info("=" * 50)
    logger.info("Daily Candles Backfill Report")
    logger.info("=" * 50)
    logger.info(f"Total processed:   {total}")
    logger.info(f"  SUCCESS:         {stats['success']}")
    logger.info(f"  PARTIAL:         {stats['partial']}")
    logger.info(f"  FAILED:          {stats['failed']}")
    logger.info(f"Rows saved:        {stats['rows_saved']:,}")
    logger.info(f"Anomalies logged:  {stats['anomalies']}")
    logger.info(f"Cooldown events:   {stats['cooldown_events']}")
    logger.info(f"Elapsed:           {elapsed/60:.1f} min ({elapsed:.0f}s)")
    logger.info(f"Avg per ticker:    {elapsed/max(total,1):.2f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-resume", action="store_true")
    parser.add_argument("--incremental", action="store_true",
                        help="증분 모드: active 종목의 last_candle_date+1부터 오늘까지")
    args = parser.parse_args()
    main(force_resume=args.force_resume, incremental=args.incremental)

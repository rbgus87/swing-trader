"""Phase 1 Step 2 부가작업 — 최종 거래일 기반 폐지 추정.

조건: last trading date < today - 30d AND listed_date < today - 30d
→ delisted_date = last_trading_date + 1, delisting_reason = 'inferred_from_last_trading_day'
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.db import get_connection


DELIST_THRESHOLD_DAYS = 30


def infer_delisted() -> int:
    today = date.today()
    threshold_date = today - timedelta(days=DELIST_THRESHOLD_DAYS)

    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT s.ticker, s.listed_date, MAX(c.date) AS last_trading_date
            FROM stocks s
            LEFT JOIN daily_candles c ON s.ticker = c.ticker
            WHERE s.delisted_date IS NULL
            GROUP BY s.ticker
            HAVING last_trading_date IS NOT NULL
               AND last_trading_date < ?
               AND s.listed_date < ?
            """,
            (threshold_date.isoformat(), threshold_date.isoformat()),
        )
        candidates = cursor.fetchall()
        logger.info(f"Delisting candidates: {len(candidates)}")

        for ticker, _listed, last_date_str in candidates:
            last_date = date.fromisoformat(last_date_str)
            inferred = last_date + timedelta(days=1)
            conn.execute(
                """
                UPDATE stocks
                SET delisted_date = ?,
                    delisting_reason = 'inferred_from_last_trading_day',
                    last_updated = datetime('now')
                WHERE ticker = ?
                """,
                (inferred.isoformat(), ticker),
            )

        total_inferred = conn.execute(
            "SELECT COUNT(*) FROM stocks "
            "WHERE delisting_reason = 'inferred_from_last_trading_day'"
        ).fetchone()[0]
        total_delisted = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE delisted_date IS NOT NULL"
        ).fetchone()[0]
        total_active = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE delisted_date IS NULL"
        ).fetchone()[0]

    logger.info("=" * 50)
    logger.info("Delisting Inference Report")
    logger.info("=" * 50)
    logger.info(f"Updated this run:      {len(candidates)}")
    logger.info(f"Total inferred:        {total_inferred}")
    logger.info(f"Total delisted:        {total_delisted}")
    logger.info(f"Active (still listed): {total_active}")
    logger.info(f"Threshold: last trade < {threshold_date}")
    return len(candidates)


if __name__ == "__main__":
    infer_delisted()

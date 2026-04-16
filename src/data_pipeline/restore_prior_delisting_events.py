"""이전상장 이력 복원: 5건의 DELISTED_PRIOR 이벤트를 stock_status_events에 기록.

KRX 상폐 XLS 파일에 중복 기재된 종목의 첫 번째 폐지 이벤트(이전상장)를
stock_status_events 테이블에 복원한다.

멱등성: INSERT OR IGNORE 사용. 반복 실행 안전.

Usage:
    python src/data_pipeline/restore_prior_delisting_events.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.db import get_connection  # noqa: E402

PRIOR_DELISTING_EVENTS = [
    {"ticker": "127160", "name": "매직마이크로", "prior_delist_date": "2015-11-19", "reason": "코스닥시장 이전상장"},
    {"ticker": "148140", "name": "비디아이",     "prior_delist_date": "2017-11-09", "reason": "코스닥시장 이전상장"},
    {"ticker": "182690", "name": "테라셈",       "prior_delist_date": "2014-10-30", "reason": "코스닥시장 이전상장"},
    {"ticker": "197210", "name": "리드",         "prior_delist_date": "2015-11-20", "reason": "코스닥시장 이전상장"},
    {"ticker": "204990", "name": "코썬바이오",   "prior_delist_date": "2016-12-09", "reason": "코스닥시장 이전상장"},
]


def main() -> int:
    now = datetime.now().isoformat()
    inserted = 0
    skipped = 0

    with get_connection() as conn:
        for event in PRIOR_DELISTING_EVENTS:
            # 중복 체크
            existing = conn.execute(
                """SELECT id FROM stock_status_events
                   WHERE ticker = ? AND event_type = 'DELISTED_PRIOR'
                     AND start_date = ?""",
                (event["ticker"], event["prior_delist_date"]),
            ).fetchone()

            if existing:
                print(f"  SKIP {event['ticker']} {event['name']} — 이미 등록됨 (id={existing['id']})")
                skipped += 1
                continue

            conn.execute(
                """INSERT INTO stock_status_events
                   (ticker, event_type, start_date, end_date, reason, source, collected_at)
                   VALUES (?, 'DELISTED_PRIOR', ?, NULL, ?, 'manual_from_xls', ?)""",
                (event["ticker"], event["prior_delist_date"], event["reason"], now),
            )
            print(f"  INSERT {event['ticker']} {event['name']} — {event['prior_delist_date']} {event['reason']}")
            inserted += 1

    print(f"\n완료: {inserted}건 INSERT, {skipped}건 SKIP (기존 등록)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One-shot cleanup: market_division의 상태 라벨을 분리.

- 관리종목         → stock_status_events (ADMIN_DESIGNATED) + market_division NULL
- 투자주의환기종목 → stock_status_events (INVESTMENT_WARNING) + market_division NULL
- SPAC             → market_division NULL (stock_type='SPAC'와 교차 검증, mismatch만 로그)
- 외국기업         → stock_type='FOREIGN' + market_division NULL

재실행 안전: 동일 source의 ADMIN_DESIGNATED / INVESTMENT_WARNING 기존 이벤트 DELETE 후 재INSERT.

Usage:
    python src/data_pipeline/migrate_market_division_cleanup.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.db import get_connection

REASON = "지정일 불명 (수집 시점)"
SOURCE = "KRX_base_info"


def main() -> int:
    today_iso = date.today().isoformat()
    now_iso = datetime.now().isoformat(timespec="seconds")

    with get_connection() as conn:
        admin_tickers = [
            r[0] for r in conn.execute(
                "SELECT ticker FROM stocks WHERE market_division LIKE '관리종목%'"
            )
        ]
        warn_tickers = [
            r[0] for r in conn.execute(
                "SELECT ticker FROM stocks WHERE market_division LIKE '투자주의환기종목%'"
            )
        ]
        spac_rows = list(
            conn.execute(
                "SELECT ticker, name, stock_type FROM stocks "
                "WHERE market_division LIKE 'SPAC%'"
            )
        )
        foreign_tickers = [
            r[0] for r in conn.execute(
                "SELECT ticker FROM stocks WHERE market_division LIKE '외국기업%'"
            )
        ]

        spac_mismatches = [
            {"ticker": t, "name": n, "stock_type": st}
            for (t, n, st) in spac_rows
            if st != "SPAC"
        ]

        # Reset events of these types from this source (idempotent re-run)
        conn.execute(
            "DELETE FROM stock_status_events "
            "WHERE event_type IN ('ADMIN_DESIGNATED','INVESTMENT_WARNING') "
            "AND source = ?",
            (SOURCE,),
        )

        if admin_tickers:
            conn.executemany(
                """
                INSERT INTO stock_status_events
                (ticker, event_type, start_date, end_date, reason, source, collected_at)
                VALUES (?, 'ADMIN_DESIGNATED', ?, NULL, ?, ?, ?)
                """,
                [
                    (t, today_iso, REASON, SOURCE, now_iso)
                    for t in admin_tickers
                ],
            )
        if warn_tickers:
            conn.executemany(
                """
                INSERT INTO stock_status_events
                (ticker, event_type, start_date, end_date, reason, source, collected_at)
                VALUES (?, 'INVESTMENT_WARNING', ?, NULL, ?, ?, ?)
                """,
                [
                    (t, today_iso, REASON, SOURCE, now_iso)
                    for t in warn_tickers
                ],
            )

        if foreign_tickers:
            conn.executemany(
                "UPDATE stocks SET stock_type='FOREIGN' WHERE ticker=?",
                [(t,) for t in foreign_tickers],
            )

        cur = conn.execute(
            """
            UPDATE stocks SET market_division = NULL
            WHERE market_division LIKE '관리종목%'
               OR market_division LIKE '투자주의환기종목%'
               OR market_division LIKE 'SPAC%'
               OR market_division LIKE '외국기업%'
            """
        )
        md_nulled = cur.rowcount

        # Final state
        final_md = list(
            conn.execute(
                "SELECT market_division, COUNT(*) FROM stocks "
                "WHERE market_division IS NOT NULL "
                "GROUP BY market_division ORDER BY COUNT(*) DESC"
            )
        )
        final_st = list(
            conn.execute(
                "SELECT stock_type, COUNT(*) FROM stocks "
                "GROUP BY stock_type ORDER BY COUNT(*) DESC"
            )
        )

    print()
    print("====================================")
    print("market_division cleanup")
    print("====================================")
    print(f"  관리종목 → stock_status_events (ADMIN_DESIGNATED): {len(admin_tickers)} events")
    print(f"  투자주의환기종목 → stock_status_events (INVESTMENT_WARNING): {len(warn_tickers)} events")
    print(f"  SPAC market_division cleared:                       {len(spac_rows)} rows")
    print(f"  외국기업 → stock_type=FOREIGN:                       {len(foreign_tickers)} rows")
    print(f"  market_division NULL set (total cleared):            {md_nulled} rows")
    print()
    print("SPAC cross-check (market_division=SPAC vs stock_type):")
    if spac_mismatches:
        print(f"  Mismatches: {len(spac_mismatches)} (logged only — NOT auto-corrected)")
        for m in spac_mismatches[:10]:
            print(f"    {m['ticker']}  {m['name']:<18} stock_type={m['stock_type']}")
    else:
        print("  All consistent — every market_division=SPAC has stock_type=SPAC ✓")
    print()
    print("Final market_division distribution:")
    if final_md:
        for md, c in final_md:
            print(f"  {md:<20} {c}")
    else:
        print("  (all NULL)")
    status_residual = [(md, c) for md, c in final_md if md not in (
        "중견기업부", "우량기업부", "벤처기업부", "기술성장기업부"
    )]
    if status_residual:
        logger.warning(f"Status labels still present after cleanup: {status_residual}")
    else:
        print("  (status labels: 0 — cleanup successful ✓)")
    print()
    print("stock_type distribution after migration:")
    for st, c in final_st:
        print(f"  {st:<10} {c}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

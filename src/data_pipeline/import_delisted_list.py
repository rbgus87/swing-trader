"""Phase 1 Step 2b — 상장폐지 종목 xls 파일 import.

KRX 정보데이터시스템에서 받은 상장폐지현황.xls(HTML 형식, CP949)을 파싱해서
stocks 테이블에 폐지 종목을 추가. 폐지일 >= 2014-01-01만 대상.

3-case 분기 (멱등):
  1. ticker 없음                       → INSERT 신규 폐지 종목
  2. ticker 있음 + 같은 name           → UPDATE delisting info만
  3. ticker 있음 + 다른 name (재사용)  → stocks 유지, ticker_reuse_events +
                                          TICKER_REUSE_POLLUTED anomaly 기록

listed_date는 NULL로 두고 collect_daily_candles.py가 첫 거래일로 보강.
market은 NULL 유지 (파일에 정보 없음).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

import pandas as pd
from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline import PROJECT_ROOT
from src.data_pipeline.db import get_connection

DELIST_FILE_PATH = PROJECT_ROOT / "data" / "raw" / "delisting" / "상장폐지현황.xls"
CUTOFF_DATE = "2014-01-01"


PREFERRED_NAME_PATTERNS = [
    re.compile(r"우$"),
    re.compile(r"우B$"),
    re.compile(r"우C$"),
    re.compile(r"우\(전환\)$"),
    re.compile(r"\d우$"),
    re.compile(r"\d우B$"),
    re.compile(r"\d우C$"),
    re.compile(r"\d우\(전환\)$"),
]


def is_preferred(name: str, ticker: str) -> bool:
    if ticker[-1] == "0":
        return False
    return any(pat.search(name) for pat in PREFERRED_NAME_PATTERNS)


def classify_stock_type(name: str, ticker: str) -> str:
    """stock_type 판별. 우선순위: SPAC → REIT → PREFERRED → FOREIGN → COMMON.

    외국기업 판별은 파일에 시장구분이 없어 ticker 앞자리 9로 대체.
    """
    if "스팩" in name:
        return "SPAC"
    if "리츠" in name or "위탁관리부동산투자회사" in name:
        return "REIT"
    if is_preferred(name, ticker):
        return "PREFERRED"
    if ticker.startswith("9") and len(ticker) == 6:
        return "FOREIGN"
    return "COMMON"


def _normalize_delist_date(v) -> str:
    """폐지일 문자열을 YYYY-MM-DD ISO 형식으로 정규화.

    입력 예시: '2023/01/02', '2023-01-02', '20230102', datetime 등.
    """
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return ""
    # YYYY/MM/DD
    if re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", s):
        y, m, d = s.split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", s):
        y, m, d = s.split("-")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    # YYYYMMDD
    if re.match(r"^\d{8}$", s):
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    # Fallback: attempt pandas parse
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.notna(ts):
            return ts.date().isoformat()
    except Exception:
        pass
    return s  # Return as-is; downstream will filter


def parse_delisting_xls(path: Path) -> pd.DataFrame:
    """KRX 폐지 xls(HTML, CP949) 파싱.

    Returns: DataFrame[ticker, name, delist_date, reason]
    """
    if not path.exists():
        raise FileNotFoundError(f"Delisting file not found: {path}")

    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("cp949", errors="replace")
    dfs = pd.read_html(StringIO(text))

    if not dfs:
        raise ValueError("No tables found in xls file")

    df = dfs[0]
    if len(df.columns) != 6:
        raise ValueError(
            f"Expected 6 columns (번호/회사명/종목코드/폐지일/폐지사유/비고), "
            f"got {len(df.columns)}: {df.columns.tolist()}"
        )
    df.columns = ["no", "name", "ticker", "delist_date", "reason", "note"]

    df["ticker"] = df["ticker"].astype(str).str.strip().str.zfill(6)
    df["delist_date"] = df["delist_date"].apply(_normalize_delist_date)
    df["reason"] = df["reason"].fillna("").astype(str).str.strip()
    df["name"] = df["name"].astype(str).str.strip()

    return df[["ticker", "name", "delist_date", "reason"]]


def import_to_stocks(df: pd.DataFrame, cutoff_date: str = CUTOFF_DATE) -> dict:
    """stocks 테이블에 삽입/갱신. 3-case 분기."""
    filtered = df[df["delist_date"] >= cutoff_date].copy()
    logger.info(f"Filtered by cutoff {cutoff_date}: {len(filtered)} / {len(df)}")

    now = datetime.now().isoformat(timespec="seconds")
    stats = {
        "total_filtered": len(filtered),
        "new_inserts": 0,
        "updates": 0,
        "reuse_detected": 0,
        "unchanged": 0,
    }
    reuse_samples: list[dict] = []
    reuse_all: list[dict] = []

    with get_connection() as conn:
        for _, row in filtered.iterrows():
            ticker = row["ticker"]
            name = row["name"]

            existing = conn.execute(
                "SELECT name, delisted_date FROM stocks WHERE ticker = ?",
                (ticker,),
            ).fetchone()

            stock_type = classify_stock_type(name, ticker)

            if existing is None:
                # market은 KRX 폐지 파일에 시장구분 없어 'UNKNOWN' 플레이스홀더.
                # (stocks.market NOT NULL 제약. 나중에 pykrx 이력 등으로 보강 가능.)
                conn.execute(
                    """
                    INSERT INTO stocks
                    (ticker, name, market, stock_type, parent_ticker,
                     listed_date, delisted_date, delisting_reason, last_updated)
                    VALUES (?, ?, 'UNKNOWN', ?, NULL, NULL, ?, ?, ?)
                    """,
                    (ticker, name, stock_type, row["delist_date"], row["reason"], now),
                )
                stats["new_inserts"] += 1
                continue

            existing_name = existing["name"]
            existing_delisted = existing["delisted_date"]

            if existing_name == name:
                if existing_delisted != row["delist_date"] or not existing_delisted:
                    conn.execute(
                        """
                        UPDATE stocks
                        SET delisted_date = ?,
                            delisting_reason = ?,
                            last_updated = ?
                        WHERE ticker = ?
                        """,
                        (row["delist_date"], row["reason"], now, ticker),
                    )
                    stats["updates"] += 1
                else:
                    stats["unchanged"] += 1
                continue

            # Case 3: ticker reuse
            logger.warning(
                f"Ticker reuse: {ticker} "
                f"(current stocks='{existing_name}', delisted file='{name}')"
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO ticker_reuse_events
                (original_ticker, reuse_start_date, old_company_name,
                 new_company_name, old_delisted_date, notes, verified)
                VALUES (?, ?, ?, ?, ?, 'Auto-detected via Step 2b import', 0)
                """,
                (
                    ticker,
                    row["delist_date"],
                    name,
                    existing_name,
                    row["delist_date"],
                ),
            )
            conn.execute(
                """
                INSERT INTO data_anomaly_log
                (ticker, date, anomaly_type, details, severity, detected_at)
                VALUES (?, NULL, 'TICKER_REUSE_POLLUTED', ?, 'ERROR', ?)
                """,
                (
                    ticker,
                    json.dumps(
                        {
                            "old_company": name,
                            "new_company": existing_name,
                            "note": "daily_candles contain data from multiple companies",
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            stats["reuse_detected"] += 1
            reuse_all.append(
                {
                    "ticker": ticker,
                    "old": name,
                    "new": existing_name,
                    "delist_date": row["delist_date"],
                }
            )
            if len(reuse_samples) < 5:
                reuse_samples.append(reuse_all[-1])

    stats["reuse_samples"] = reuse_samples
    stats["reuse_all"] = reuse_all
    return stats


def print_report(parse_stats: dict, import_stats: dict) -> None:
    logger.info("=" * 50)
    logger.info("Phase 1 Step 2b — Delisting Import Report")
    logger.info("=" * 50)
    logger.info(f"Source file:      {DELIST_FILE_PATH.name}")
    logger.info(f"Total in file:    {parse_stats['total_in_file']}")
    logger.info(f"After {CUTOFF_DATE}: {import_stats['total_filtered']}")
    logger.info(f"  New inserts:    {import_stats['new_inserts']}")
    logger.info(f"  Updates:        {import_stats['updates']}")
    logger.info(f"  Unchanged:      {import_stats['unchanged']}")
    logger.info(f"  Reuse detected: {import_stats['reuse_detected']}")

    if import_stats.get("reuse_samples"):
        logger.info("")
        logger.info("Reuse samples (top 5):")
        for s in import_stats["reuse_samples"]:
            logger.info(
                f"  {s['ticker']}: '{s['old']}' → '{s['new']}' (delist {s['delist_date']})"
            )

    with get_connection() as conn:
        total_stocks = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        total_delisted = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE delisted_date IS NOT NULL"
        ).fetchone()[0]
        type_dist = conn.execute(
            """
            SELECT stock_type, COUNT(*) FROM stocks
            WHERE delisted_date IS NOT NULL
            GROUP BY stock_type ORDER BY COUNT(*) DESC
            """
        ).fetchall()

    logger.info("")
    logger.info("stocks 테이블 최종 상태:")
    logger.info(f"  Total:    {total_stocks}")
    logger.info(f"  Delisted: {total_delisted}")
    logger.info("  Delisted by type:")
    for row in type_dist:
        logger.info(f"    {row['stock_type']}: {row[1]}")


def main() -> int:
    logger.info("Parsing delisting xls file...")
    df = parse_delisting_xls(DELIST_FILE_PATH)
    parse_stats = {"total_in_file": len(df)}
    logger.info(f"Parsed {len(df)} entries")

    logger.info("Importing to stocks table...")
    import_stats = import_to_stocks(df, CUTOFF_DATE)

    print_report(parse_stats, import_stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())

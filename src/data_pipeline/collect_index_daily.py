"""KOSPI/KOSDAQ 지수 일봉 수집.

index_daily 테이블에 OHLCV 적재. 전체 수집(기본) 또는 증분(--update-only).
FDR이 지수에 대해 LOGOUT 차단하므로 Yahoo Finance(yfinance)로 수집.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger

from src.data_pipeline.db import get_connection


INDEX_MAP = {
    'KOSPI': '^KS11',
    'KOSDAQ': '^KQ11',
}
DEFAULT_START = date(2014, 1, 1)


def ensure_schema() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS index_daily (
                index_code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL NOT NULL,
                volume INTEGER,
                PRIMARY KEY (index_code, date)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(date)")


def last_date(index_code: str) -> date | None:
    with get_connection() as conn:
        r = conn.execute(
            "SELECT MAX(date) as d FROM index_daily WHERE index_code = ?",
            (index_code,),
        ).fetchone()
    if r and r['d']:
        return datetime.strptime(r['d'], '%Y-%m-%d').date()
    return None


def upsert(index_code: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = []
    for idx, row in df.iterrows():
        d = idx.strftime('%Y-%m-%d') if hasattr(idx, 'strftime') else str(idx)[:10]
        close = row.get('Close')
        if pd.isna(close):
            continue
        rows.append((
            index_code, d,
            float(row['Open']) if pd.notna(row.get('Open')) else None,
            float(row['High']) if pd.notna(row.get('High')) else None,
            float(row['Low']) if pd.notna(row.get('Low')) else None,
            float(close),
            int(row['Volume']) if pd.notna(row.get('Volume')) else None,
        ))
    if not rows:
        return 0
    with get_connection() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO index_daily
                (index_code, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)
    return len(rows)


def collect(index_code: str, start_date: date, end_date: date | None = None) -> int:
    ticker = INDEX_MAP[index_code]
    end_str = (end_date + timedelta(days=1)).isoformat() if end_date else (date.today() + timedelta(days=1)).isoformat()
    logger.info(f"{index_code} ({ticker}) {start_date} ~ {end_date or 'today'}")
    try:
        df = yf.download(
            ticker, start=start_date.isoformat(), end=end_str,
            progress=False, auto_adjust=False, threads=False,
        )
    except Exception as e:
        logger.warning(f"{index_code}: yfinance error {e}")
        return 0
    if df is None or df.empty:
        logger.warning(f"{index_code}: yfinance returned empty")
        return 0
    # MultiIndex 컬럼 flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    n = upsert(index_code, df)
    logger.info(f"{index_code}: {n} rows upserted")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--update-only', action='store_true',
                    help='DB 최종 날짜 이후만 수집')
    ap.add_argument('--start', type=str, default=DEFAULT_START.isoformat(),
                    help='전체 수집 시작일 (YYYY-MM-DD)')
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    ensure_schema()

    total = 0
    for code in INDEX_MAP:
        if args.update_only:
            ld = last_date(code)
            start = ld + timedelta(days=1) if ld else DEFAULT_START
            if start > date.today():
                logger.info(f"{code}: already up-to-date ({ld})")
                continue
        else:
            start = datetime.strptime(args.start, '%Y-%m-%d').date()
        total += collect(code, start)

    with get_connection() as conn:
        for code in INDEX_MAP:
            r = conn.execute(
                "SELECT COUNT(*) as c, MIN(date) as mn, MAX(date) as mx FROM index_daily WHERE index_code = ?",
                (code,),
            ).fetchone()
            logger.info(f"{code}: total={r['c']}, {r['mn']} ~ {r['mx']}")

    logger.info(f"전체 upsert: {total} rows")
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""FDR `StockListing('KRX-DELISTING')` 구조 탐색.

Phase 1 Step 1b-3 후속. KRX/DART 단독 실패 후 FDR KRX-DELISTING 경로의
폐지 이력 커버리지·필드 구조 확인.

Usage:
    python scripts/explore_fdr_delisting.py
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import FinanceDataReader as fdr  # noqa: E402
import pandas as pd  # noqa: E402


TEST_CASES = [
    ("003620", "쌍용자동차", 2022),
    ("097230", "한진중공업", 2021),
    ("048260", "오스템임플란트", 2023),
]


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    section("STEP 1 — Basic call")
    t0 = time.monotonic()
    try:
        df = fdr.StockListing("KRX-DELISTING")
    except Exception:
        print("fdr.StockListing('KRX-DELISTING') raised:")
        traceback.print_exc()
        return 1
    elapsed = time.monotonic() - t0
    print(f"Returned in {elapsed:.2f}s")
    print(f"Type: {type(df).__name__}")
    print(f"Shape: {df.shape}")
    print(f"Columns: {df.columns.tolist()}")
    print("Dtypes:")
    print(df.dtypes.to_string())

    if df.empty:
        print("\n(empty DataFrame — FDR returned nothing)")
        return 1

    section("STEP 2 — Structure dump")
    print(f"Total rows: {len(df)}")
    print("\n--- head(10) ---")
    print(df.head(10).to_string())
    print("\n--- tail(10) ---")
    print(df.tail(10).to_string())
    print("\n--- Null counts per column ---")
    print(df.isna().sum().to_string())

    # Date-like columns: min/max coverage
    date_like = [c for c in df.columns if str(df[c].dtype).startswith("datetime") or "date" in c.lower() or "일자" in c or "일" in c]
    if date_like:
        print("\n--- Date-like columns coverage ---")
        for c in date_like:
            col = df[c]
            try:
                parsed = pd.to_datetime(col, errors="coerce")
                non_null = parsed.dropna()
                if len(non_null):
                    print(f"  {c}: min={non_null.min()} max={non_null.max()} non_null={len(non_null)}/{len(df)}")
                else:
                    print(f"  {c}: all null after coercion")
            except Exception as e:
                print(f"  {c}: failed to parse — {e}")

    section("STEP 3 — Delisting date/reason fields")
    keyword_cols = [
        c for c in df.columns
        if any(k in c.lower() for k in ("delist", "date", "reason"))
        or any(k in c for k in ("일자", "일", "사유"))
    ]
    print(f"Keyword-hit columns: {keyword_cols}")
    for c in keyword_cols:
        sample = df[c].dropna().head(5).tolist()
        print(f"\n  {c}  (dtype={df[c].dtype})")
        print(f"    samples: {sample}")

    section("STEP 4 — Test cases")
    # Find symbol-like column
    symbol_col_candidates = [
        c for c in df.columns
        if c.lower() in ("symbol", "code", "ticker", "종목코드")
    ]
    symbol_col = symbol_col_candidates[0] if symbol_col_candidates else df.columns[0]
    print(f"Using symbol column: {symbol_col}")

    for ticker, expected_name, expected_year in TEST_CASES:
        series = df[symbol_col].astype(str).str.strip()
        matches = df[series == ticker]
        print(f"\n=== {ticker} {expected_name} (expected delisting year: {expected_year}) ===")
        print(f"  Matches: {len(matches)}")
        if len(matches):
            print(matches.to_string())

    section("STEP 5 — Aggregate stats")
    # Detect delisting-date column heuristically
    delist_col = None
    for cand in ["DelistingDate", "Delisting Date", "delistingDate", "폐지일자", "폐지일", "상폐일", "상장폐지일"]:
        if cand in df.columns:
            delist_col = cand
            break
    if delist_col is None and date_like:
        delist_col = date_like[0]
    print(f"Assumed delisting-date column: {delist_col}")

    if delist_col:
        parsed = pd.to_datetime(df[delist_col], errors="coerce")
        non_null = parsed.dropna()
        after_2014 = non_null[non_null >= pd.Timestamp("2014-01-01")]
        print(f"Total with parseable date: {len(non_null)}")
        print(f"After 2014-01-01:          {len(after_2014)}")
        if len(non_null):
            year_counts = non_null.dt.year.value_counts().sort_index()
            print("\nBy year:")
            for y, n in year_counts.items():
                print(f"  {y}: {n}")

    # Reason column detection
    reason_col = None
    for cand in ["Reason", "DelistingReason", "폐지사유", "상장폐지사유", "사유", "상장폐지원인"]:
        if cand in df.columns:
            reason_col = cand
            break
    if reason_col:
        print(f"\nReason column: {reason_col}")
        vc = df[reason_col].value_counts().head(15)
        print("Top 15 reasons:")
        print(vc.to_string())
    else:
        print("\n(no reason column identified by common names)")

    section("STEP 6 — FDR DataReader on delisted tickers")
    for ticker in ["048260", "003620", "097230"]:
        t0 = time.monotonic()
        try:
            ohlcv = fdr.DataReader(ticker, "2020-01-01", "2024-12-31")
            dt = time.monotonic() - t0
            last = ohlcv.index[-1] if len(ohlcv) else "(empty)"
            first = ohlcv.index[0] if len(ohlcv) else "(empty)"
            print(f"  {ticker}: {len(ohlcv)} rows  first={first}  last={last}  elapsed={dt:.2f}s")
        except Exception as e:
            print(f"  {ticker}: FAILED — {type(e).__name__}: {e}")

    section("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())

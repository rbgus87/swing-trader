"""DART corp_code 구조 탐색.

Phase 1 Step 1b-3 탐색 단계. 상장폐지 종목 수집 소스로 DART corp_code.xml이
적합한지 확인.

Cache:
    data/cache/CORPCODE.xml          — 다운로드된 원본 XML
    data/cache/dart_corp_code_sample.json — 분석용 샘플

Usage:
    python scripts/explore_dart_corp_code.py
    python scripts/explore_dart_corp_code.py --force   # 캐시 무시하고 재다운로드
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline import DB_PATH
from src.data_pipeline.dart_client import DartClient

CACHE_DIR = _PROJECT_ROOT / "data" / "cache"
XML_PATH = CACHE_DIR / "CORPCODE.xml"
SAMPLE_JSON_PATH = CACHE_DIR / "dart_corp_code_sample.json"


TEST_DELISTED_CASES = [
    ("003620", "쌍용자동차", 2022),
    ("097230", "한진중공업", 2021),
    ("048260", "오스템임플란트", 2023),
    ("020560", "아시아나항공", None),
]


def _ensure_xml(force: bool) -> Path:
    if XML_PATH.exists() and not force:
        sz = XML_PATH.stat().st_size / (1024 * 1024)
        print(f"Using cached XML: {XML_PATH} ({sz:.2f} MB)")
        return XML_PATH
    print(f"Downloading corp_code.xml → {XML_PATH}")
    DartClient().download_corp_code(XML_PATH)
    return XML_PATH


def _parse_records(xml_path: Path) -> tuple[str, list[dict[str, str]]]:
    """Return (root_tag_name, [record_dict, ...])."""
    started = time.monotonic()
    tree = ET.parse(xml_path)
    root = tree.getroot()
    root_tag = root.tag
    records: list[dict[str, str]] = []
    for child in root:
        rec = {sub.tag: (sub.text or "").strip() for sub in child}
        records.append(rec)
    elapsed = time.monotonic() - started
    print(f"Parsed {len(records)} records in {elapsed:.2f}s. root tag = '{root_tag}'")
    return root_tag, records


def _swing_db_tickers() -> set[str]:
    if not DB_PATH.exists():
        return set()
    conn = sqlite3.connect(DB_PATH)
    try:
        return {r[0] for r in conn.execute("SELECT ticker FROM stocks")}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="재다운로드")
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    xml_path = _ensure_xml(args.force)
    file_size_mb = xml_path.stat().st_size / (1024 * 1024)

    root_tag, records = _parse_records(xml_path)
    if not records:
        print("(empty XML — abort)")
        return 1

    # === Phase A: first 3 records full dump ===
    print()
    print("=" * 78)
    print("PHASE A — first 3 records full dump")
    print("=" * 78)
    print(json.dumps(records[:3], ensure_ascii=False, indent=2))

    # === Phase B: field analysis ===
    print()
    print("=" * 78)
    print("PHASE B — field analysis")
    print("=" * 78)
    field_counter = Counter()
    field_nonempty_counter = Counter()
    for r in records:
        for k, v in r.items():
            field_counter[k] += 1
            if v:
                field_nonempty_counter[k] += 1
    all_fields = sorted(field_counter.keys())
    print(f"Distinct fields across all records: {all_fields}")
    print()
    print(f"{'field':<20} {'present':>10} {'non-empty':>12} {'fill%':>8}")
    print("-" * 60)
    total = len(records)
    for f in all_fields:
        present = field_counter[f]
        nonempty = field_nonempty_counter[f]
        pct = nonempty / total * 100 if total else 0
        print(f"{f:<20} {present:>10} {nonempty:>12} {pct:>7.2f}%")

    # Look for delisting-related fields by name heuristic
    delisting_keywords = ["delist", "ceased", "end", "close", "폐지", "말소"]
    candidates = [
        f for f in all_fields
        if any(k.lower() in f.lower() for k in delisting_keywords)
    ]
    print()
    print(f"Delisting-keyword field candidates: {candidates if candidates else '(none)'}")

    # === Phase C: coverage ===
    print()
    print("=" * 78)
    print("PHASE C — coverage")
    print("=" * 78)
    has_stock_code = [r for r in records if r.get("stock_code")]
    no_stock_code = [r for r in records if not r.get("stock_code")]
    six_digit = [
        r for r in has_stock_code
        if r["stock_code"].isdigit() and len(r["stock_code"]) == 6
    ]
    print(f"Total DART corps:              {len(records)}")
    print(f"with stock_code (non-empty):   {len(has_stock_code)}")
    print(f"without stock_code:            {len(no_stock_code)}")
    print(f"with 6-digit numeric code:     {len(six_digit)}")

    swing_tickers = _swing_db_tickers()
    print(f"\nswing.db stocks count:         {len(swing_tickers)}")
    if swing_tickers:
        dart_codes = {r["stock_code"] for r in has_stock_code}
        in_dart = swing_tickers & dart_codes
        not_in_dart = swing_tickers - dart_codes
        print(f"swing.db ∩ DART (현재 상장 중 DART 매칭): {len(in_dart)}")
        print(f"swing.db − DART (DART 미등록):              {len(not_in_dart)}")
        if not_in_dart:
            sample_missing = sorted(not_in_dart)[:5]
            print(f"  sample: {sample_missing}")

    # === Phase D: delisted test cases ===
    print()
    print("=" * 78)
    print("PHASE D — delisted test cases")
    print("=" * 78)
    case_results: dict[str, list[dict]] = {}
    for stock_code, name_hint, year in TEST_DELISTED_CASES:
        print(f"\n=== {stock_code} {name_hint} (delisting={year}) ===")
        # find by stock_code
        by_code = [r for r in records if r.get("stock_code") == stock_code]
        # find by name substring
        by_name = [
            r for r in records
            if name_hint in (r.get("corp_name") or "")
        ]
        # union, dedupe by corp_code
        union_map = {}
        for r in by_code + by_name:
            cc = r.get("corp_code")
            if cc:
                union_map[cc] = r
        rows = list(union_map.values())

        if not rows:
            print("  (no match)")
            case_results[stock_code] = []
            continue

        for r in rows:
            sc = r.get("stock_code") or "(empty)"
            md = r.get("modify_date") or "(empty)"
            print(
                f"  - corp_code={r.get('corp_code')}  "
                f"corp_name={r.get('corp_name')}  "
                f"stock_code={sc}  modify_date={md}"
            )
            extras = {
                k: v for k, v in r.items()
                if k not in ("corp_code", "corp_name", "stock_code", "modify_date")
                and v
            }
            if extras:
                print(f"      extras: {extras}")

        same_code = [r for r in rows if r.get("stock_code") == stock_code]
        print(
            f"  → 같은 stock_code({stock_code})를 가진 corp_code 항목 수: {len(same_code)}"
        )
        case_results[stock_code] = rows

    # === Phase E: persist sample ===
    sample = {
        "first_20": records[:20],
        "delisted_cases": case_results,
        "fields": all_fields,
        "fill_rates": {f: field_nonempty_counter[f] / total for f in all_fields},
        "total_records": total,
        "file_size_mb": file_size_mb,
    }
    SAMPLE_JSON_PATH.write_text(
        json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nSaved sample → {SAMPLE_JSON_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

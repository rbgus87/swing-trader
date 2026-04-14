"""KRX OpenAPI exploration for Phase 1 Step 1b.

Probes 3 endpoint categories (read-only, no DB writes):
    1. Delisted stocks
    2. Listed-date (per-stock)
    3. Sector classification (KOSPI gap)

Usage:
    python scripts/explore_krx_endpoints.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.config import KRX_API_KEY

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
HEADERS = {"AUTH_KEY": KRX_API_KEY.strip()}

PROBE_DATE = "20260413"
SAMSUNG = "005930"
SAMSUNG_LISTED = "1975-06-11"
SAMSUNG_SECTOR_EXPECTED = "전기전자"

call_counter = {"n": 0}


def call(endpoint: str, params: dict, *, label: str) -> tuple[int, dict | None, float]:
    """Single GET. Returns (status_code, body_or_none, elapsed_s)."""
    call_counter["n"] += 1
    url = BASE_URL + endpoint
    t0 = time.monotonic()
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        elapsed = time.monotonic() - t0
        body: dict | None
        try:
            body = resp.json()
        except Exception:
            body = None
        print(
            f"  → [{label}] {endpoint}  params={params}  "
            f"status={resp.status_code}  elapsed={elapsed:.2f}s  call#{call_counter['n']}"
        )
        if resp.status_code != 200:
            print(f"     body[:300]={resp.text[:300]}")
        return resp.status_code, body, elapsed
    except requests.RequestException as e:
        elapsed = time.monotonic() - t0
        print(f"  → [{label}] {endpoint}  ERROR  {e}  elapsed={elapsed:.2f}s")
        return -1, None, elapsed


def show_rows(body: dict | None, n: int = 3) -> list[dict]:
    if not body:
        print("     (no body)")
        return []
    rows = body.get("OutBlock_1", [])
    print(f"     OutBlock_1 length: {len(rows)}")
    if rows:
        print(f"     fields: {list(rows[0].keys())}")
        sample = rows[:n]
        print("     sample:")
        print(json.dumps(sample, ensure_ascii=False, indent=2)[:2000])
    return rows


def find_ticker(rows: list[dict], ticker: str) -> dict | None:
    for r in rows:
        if r.get("ISU_CD") == ticker or r.get("ISU_SRT_CD") == ticker:
            return r
    return None


# ─── Probe 2 + 3: stk_isu_base_info (likely covers listed_date AND sector) ───
def probe_base_info() -> None:
    print("\n" + "=" * 78)
    print("PROBE A — /sto/stk_isu_base_info (KOSPI base info)")
    print("=" * 78)
    status, body, _ = call(
        "/sto/stk_isu_base_info",
        {"basDd": PROBE_DATE},
        label="stk_isu_base_info",
    )
    if status == 200 and body:
        rows = show_rows(body, n=2)
        samsung = find_ticker(rows, SAMSUNG)
        if samsung:
            print("\n     Samsung (005930) full record:")
            print(json.dumps(samsung, ensure_ascii=False, indent=2))
        else:
            print(f"\n     Samsung {SAMSUNG} NOT found in {len(rows)} rows")

    print("\n" + "=" * 78)
    print("PROBE B — /sto/ksq_isu_base_info (KOSDAQ base info, parity check)")
    print("=" * 78)
    status, body, _ = call(
        "/sto/ksq_isu_base_info",
        {"basDd": PROBE_DATE},
        label="ksq_isu_base_info",
    )
    if status == 200 and body:
        rows = show_rows(body, n=2)


# ─── Probe 1: delisted candidates ───
DELIST_CANDIDATES = [
    # Common KRX OpenAPI naming patterns; only one is expected to exist (if any)
    ("/sto/dlst_stk", {"basDd": PROBE_DATE}),
    ("/sto/dlst_isu_stk", {"basDd": PROBE_DATE}),
    ("/sto/dlst_isu_stk_list", {"basDd": PROBE_DATE}),
    ("/sto/dlst_stk_isu", {"basDd": PROBE_DATE}),
    ("/sto/sto_dlst_stk", {"basDd": PROBE_DATE}),
]


def probe_delisted() -> None:
    print("\n" + "=" * 78)
    print("PROBE C — Delisted-stock endpoint candidates")
    print("=" * 78)
    for path, params in DELIST_CANDIDATES:
        status, body, _ = call(path, params, label="delist?")
        if status == 200 and body:
            rows = body.get("OutBlock_1") or []
            if rows:
                print(f"     ✓ candidate appears alive: {path} (rows={len(rows)})")
                show_rows(body, n=3)
                return
            else:
                print(f"     (empty OutBlock_1 — endpoint exists but no data for {PROBE_DATE}?)")
                # Try a known-delisting period
                status2, body2, _ = call(
                    path, {"basDd": "20200401"}, label="delist?-2020"
                )
                if status2 == 200 and body2:
                    rows2 = body2.get("OutBlock_1") or []
                    if rows2:
                        print(f"     ✓ data found at 20200401 for {path} (rows={len(rows2)})")
                        show_rows(body2, n=3)
                        return
        time.sleep(0.3)


def main() -> int:
    started = time.monotonic()
    probe_base_info()
    probe_delisted()
    elapsed = time.monotonic() - started

    print("\n" + "=" * 78)
    print(f"Total KRX calls: {call_counter['n']}   Elapsed: {elapsed:.1f}s")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

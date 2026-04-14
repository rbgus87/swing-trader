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


# ─── Probe 1: delisted candidates (Step 1b-탐색 initial) ───
DELIST_CANDIDATES = [
    ("/sto/dlst_stk", {"basDd": PROBE_DATE}),
    ("/sto/dlst_isu_stk", {"basDd": PROBE_DATE}),
    ("/sto/dlst_isu_stk_list", {"basDd": PROBE_DATE}),
    ("/sto/dlst_stk_isu", {"basDd": PROBE_DATE}),
    ("/sto/sto_dlst_stk", {"basDd": PROBE_DATE}),
]

# ─── Probe F: Step 1b-3 후속 — extended delisting / status candidates ───
DELIST_CANDIDATES_EXT = [
    # Tier 2: sto namespace extended naming
    ("/sto/stk_delist", {"basDd": PROBE_DATE}),
    ("/sto/stk_delisting", {"basDd": PROBE_DATE}),
    ("/sto/stk_dlst_lst", {"basDd": PROBE_DATE}),
    ("/sto/stk_delst_lst", {"basDd": PROBE_DATE}),
    ("/sto/ksq_delist", {"basDd": PROBE_DATE}),
    ("/sto/ksq_delisting", {"basDd": PROBE_DATE}),
    ("/sto/stk_susp", {"basDd": PROBE_DATE}),
    ("/sto/stk_admin", {"basDd": PROBE_DATE}),
    ("/sto/stk_admin_stk", {"basDd": PROBE_DATE}),
    ("/sto/stk_isu_list_chg", {"basDd": PROBE_DATE}),
    # dis (공시) namespace
    ("/dis/dlst_corp", {"basDd": PROBE_DATE}),
    ("/dis/delisting", {"basDd": PROBE_DATE}),
    # gen (general)
    ("/gen/delist", {"basDd": PROBE_DATE}),
]


def probe_delisted() -> None:
    print("\n" + "=" * 78)
    print("PROBE C — Delisted-stock endpoint candidates (Step 1b-탐색)")
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


def probe_delisted_extended() -> list[dict]:
    """Step 1b-3 후속 탐색. Tier 2 확장 후보.

    Returns list of dicts summarizing each probe outcome.
    """
    print("\n" + "=" * 78)
    print("PROBE F — Extended delisting/status candidates (Step 1b-3 후속)")
    print("=" * 78)
    results: list[dict] = []
    for path, params in DELIST_CANDIDATES_EXT:
        status, body, _ = call(path, params, label="ext?")
        summary = "(no body)"
        if status == 200 and body:
            rows = body.get("OutBlock_1") or []
            summary = f"OutBlock_1 len={len(rows)}"
            if rows:
                print(f"     ✓ ALIVE with data: {path}")
                show_rows(body, n=3)
                results.append(
                    {"path": path, "status": status, "summary": summary, "alive": True}
                )
                time.sleep(0.3)
                continue
        elif status == 200 and not body:
            summary = "200 but empty body"
        results.append(
            {"path": path, "status": status, "summary": summary, "alive": False}
        )
        time.sleep(0.2)
    return results


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["all", "initial", "extended"],
        default="extended",
        help="initial=Step 1b-탐색만, extended=Step 1b-3 후속만, all=전체",
    )
    args = parser.parse_args()

    started = time.monotonic()
    ext_results: list[dict] = []
    if args.mode in ("all", "initial"):
        probe_base_info()
        probe_delisted()
    if args.mode in ("all", "extended"):
        ext_results = probe_delisted_extended()
    elapsed = time.monotonic() - started

    if ext_results:
        print("\n" + "=" * 78)
        print("SUMMARY — Extended candidates")
        print("=" * 78)
        alive = [r for r in ext_results if r["alive"]]
        dead = [r for r in ext_results if not r["alive"]]
        print(f"  ALIVE: {len(alive)} / {len(ext_results)}")
        for r in alive:
            print(f"    ✓ {r['path']}  status={r['status']}  {r['summary']}")
        print(f"  DEAD:  {len(dead)}")
        status_counts: dict[int, int] = {}
        for r in dead:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        for st, cnt in sorted(status_counts.items()):
            print(f"    status {st}: {cnt} paths")

    print("\n" + "=" * 78)
    print(f"Total KRX calls: {call_counter['n']}   Elapsed: {elapsed:.1f}s")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

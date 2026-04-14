"""Collect current KOSPI + KOSDAQ stock meta from KRX OpenAPI into stocks table.

Flow:
    1. Resolve latest trading day.
    2. 1차: bydd_trd로 종목 리스트 + stock_type 분류 + INSERT OR REPLACE
    3. Legacy 정정: 잘못된 sector(KOSDAQ 소속부) → market_division 이동 + sector NULL
    4. 2차 enrich: base_info로 isin / listed_date / market_division / name 갱신
       + KIND_STKCERT_TP_NM과 정규식 stock_type 교차 검증 (anomaly_log)
    5. 리포트 출력
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Windows cp949 환경에서 UTF-8 출력 강제
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.db import get_connection
from src.data_pipeline.krx_client import KrxClient, KrxIsuBaseInfo, KrxStockMeta
from src.data_pipeline.preferred_parent_map import MANUAL_PARENT_MAP


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

_PREFERRED_SUFFIX_STRIP_PATTERNS = [
    re.compile(r"\d*우\(전환\)$"),
    re.compile(r"\d*우B$"),
    re.compile(r"\d*우C$"),
    re.compile(r"\d*우$"),
]

# KRX SECT_TP_NM 분류
REAL_KOSDAQ_DIVISIONS = {"중견기업부", "우량기업부", "벤처기업부", "기술성장기업부"}
SECT_STATUS_RULES: dict[str, dict] = {
    "관리종목": {"action": "event", "event_type": "ADMIN_DESIGNATED"},
    "투자주의환기종목": {"action": "event", "event_type": "INVESTMENT_WARNING"},
    "SPAC": {"action": "clear"},
    "외국기업": {"action": "foreign"},
}


def _strip_paren_suffix(s: str) -> str:
    """관리종목(소속부없음) → 관리종목"""
    idx = s.find("(")
    return s[:idx].strip() if idx >= 0 else s.strip()


def classify_sect_tp_nm(
    sect_tp_nm: Optional[str],
) -> tuple[Optional[str], Optional[dict]]:
    """KRX base_info의 SECT_TP_NM → (market_division, status_action) 분리.

    Returns:
        - 실제 KOSDAQ 소속부 → (값, None)
        - 상태 라벨(관리/투자주의/SPAC/외국기업) → (None, action_dict)
        - 알 수 없는 값 → (값, None) + WARN
        - 빈 값 → (None, None)
    """
    s = (sect_tp_nm or "").strip()
    if not s:
        return None, None
    if s in REAL_KOSDAQ_DIVISIONS:
        return s, None
    base = _strip_paren_suffix(s)
    if base in SECT_STATUS_RULES:
        return None, SECT_STATUS_RULES[base]
    logger.warning(f"Unknown SECT_TP_NM value: {s!r}")
    return s, None

# KRX KIND_STKCERT_TP_NM 정규화: 우선주 family는 다양한 표기를 갖음
# - 보통주
# - 우선주 / 구형우선주 / 신형우선주 / 종류주권
PREFERRED_KIND_VALUES = {"우선주", "구형우선주", "신형우선주", "종류주권"}
COMMON_KIND_VALUES = {"보통주"}


def _normalize_kind(kind: str) -> Optional[str]:
    """KRX KIND_STKCERT_TP_NM → COMMON / PREFERRED / None(unknown)."""
    if kind in COMMON_KIND_VALUES:
        return "COMMON"
    if kind in PREFERRED_KIND_VALUES:
        return "PREFERRED"
    return None


def is_preferred(name: str, ticker: str) -> bool:
    """우선주 판별. 종목코드 끝자리 + 종목명 패턴 AND 조건."""
    if ticker[-1] == "0":
        return False
    return any(pat.search(name) for pat in PREFERRED_NAME_PATTERNS)


def classify_stock_type(name: str, ticker: str) -> str:
    n = name.strip()
    if "스팩" in n:
        return "SPAC"
    if "리츠" in n or "위탁관리부동산투자회사" in n:
        return "REIT"
    if is_preferred(n, ticker):
        return "PREFERRED"
    return "COMMON"


def _strip_preferred_suffix(name: str) -> str:
    for pat in _PREFERRED_SUFFIX_STRIP_PATTERNS:
        name = pat.sub("", name)
    return name.strip()


def guess_parent_ticker(
    ticker: str,
    name: str,
    all_stocks: dict[str, dict],
) -> Optional[str]:
    current_market = all_stocks.get(ticker, {}).get("market")

    basename = _strip_preferred_suffix(name)
    if basename:
        for cand_ticker, cand_info in all_stocks.items():
            if (
                cand_info["stock_type"] == "COMMON"
                and cand_info["market"] == current_market
                and cand_info["name"] == basename
            ):
                return cand_ticker

    candidate_ticker = ticker[:-1] + "0"
    if candidate_ticker in all_stocks:
        cand_info = all_stocks[candidate_ticker]
        if cand_info["stock_type"] == "COMMON":
            return candidate_ticker

    if ticker in MANUAL_PARENT_MAP:
        mapped = MANUAL_PARENT_MAP[ticker]
        if mapped in all_stocks:
            return mapped
        logger.warning(
            f"Manual mapping {ticker}→{mapped} but {mapped} not in stocks"
        )
        return None

    return None


def _latest_krx_trading_day(client: KrxClient) -> str:
    today = date.today()
    for delta in range(0, 10):
        probe = today - timedelta(days=delta)
        ymd = probe.strftime("%Y%m%d")
        rows = client.get_listed_stocks("KOSPI", base_date=ymd)
        if rows:
            logger.info(f"Latest trading day resolved: {ymd} ({len(rows)} KOSPI rows)")
            return ymd
        logger.info(f"{ymd} returned empty; trying previous day")
    raise RuntimeError("Could not find a trading day in last 10 days")


def _extract_sector(meta: KrxStockMeta) -> Optional[str]:
    """KRX bydd_trd 응답의 SECT_TP_NM (KOSDAQ 소속부) — 정정 로직에서 옮겨짐."""
    raw = meta.model_dump()
    for key in ("IDX_IND_NM", "SECT_TP_NM", "KSQ_SECT_TP_NM"):
        v = raw.get(key)
        if v and str(v).strip():
            return str(v).strip()
    return None


def _sample_by_type(rows: list[dict], stype: str, n: int = 3) -> list[dict]:
    return [r for r in rows if r["stock_type"] == stype][:n]


def _to_iso_date(yyyymmdd: Optional[str]) -> Optional[str]:
    if not yyyymmdd or len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def collect_from_bydd_trd(client: KrxClient, base_date: str) -> list[dict]:
    """1차 수집: bydd_trd로 ticker/name/market/stock_type/parent_ticker INSERT OR REPLACE."""
    kospi = client.get_listed_stocks("KOSPI", base_date=base_date)
    kosdaq = client.get_listed_stocks("KOSDAQ", base_date=base_date)
    logger.info(f"bydd_trd: KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

    records: list[dict] = []
    for meta, market in [(m, "KOSPI") for m in kospi] + [
        (m, "KOSDAQ") for m in kosdaq
    ]:
        stype = classify_stock_type(meta.isu_nm, meta.isu_cd)
        records.append(
            {
                "ticker": meta.isu_cd,
                "name": meta.isu_nm,
                "market": market,
                "sector": _extract_sector(meta),
                "stock_type": stype,
                "parent_ticker": None,
            }
        )

    all_stocks = {r["ticker"]: r for r in records}
    for r in records:
        if r["stock_type"] == "PREFERRED":
            r["parent_ticker"] = guess_parent_ticker(
                r["ticker"], r["name"], all_stocks
            )

    now_iso = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO stocks (
                ticker, name, market, sector, stock_type, parent_ticker,
                listed_date, delisted_date, delisting_reason,
                first_candle_date, last_candle_date, last_updated
            ) VALUES (
                :ticker, :name, :market, :sector, :stock_type, :parent_ticker,
                NULL, NULL, NULL, NULL, NULL, :last_updated
            )
            """,
            [{**r, "last_updated": now_iso} for r in records],
        )
    return records


def migrate_sector_to_market_division() -> tuple[int, int]:
    """기존 sector(KOSDAQ 소속부)를 market_division으로 이동, sector는 NULL.

    Returns: (moved_count, nullified_count)
    """
    with get_connection() as conn:
        moved = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE sector IS NOT NULL AND sector != ''"
        ).fetchone()[0]
        conn.execute(
            "UPDATE stocks SET market_division = sector "
            "WHERE sector IS NOT NULL AND sector != ''"
        )
        total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        conn.execute("UPDATE stocks SET sector = NULL")
    logger.info(f"sector→market_division: moved={moved}, sector→NULL={total}")
    return moved, total


def enrich_from_base_info(
    client: KrxClient,
    base_date: str,
    bydd_records: list[dict],
) -> dict:
    """2차 보강: base_info로 isin / listed_date / market_division / name 갱신
    + KIND_STKCERT_TP_NM과 정규식 stock_type 교차 검증
    + SECT_TP_NM 상태 라벨 라우팅 (관리종목/투자주의환기종목/SPAC/외국기업)."""
    kospi = client.get_stock_base_info("KOSPI", base_date=base_date)
    kosdaq = client.get_stock_base_info("KOSDAQ", base_date=base_date)
    logger.info(f"base_info: KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

    bydd_by_ticker = {r["ticker"]: r for r in bydd_records}

    update_rows: list[dict] = []
    mismatches: list[dict] = []
    name_changes: list[dict] = []
    foreign_tickers: list[str] = []
    spac_mismatches: list[dict] = []
    pending_admin: list[str] = []
    pending_warning: list[str] = []

    for bi in kospi + kosdaq:
        ticker = bi.ISU_SRT_CD
        listed_iso = _to_iso_date(bi.LIST_DD)
        name = (bi.ISU_ABBRV or "").strip()
        md_value, status_action = classify_sect_tp_nm(bi.SECT_TP_NM)

        if status_action:
            md_value = None
            action = status_action["action"]
            if action == "event":
                event_type = status_action["event_type"]
                if event_type == "ADMIN_DESIGNATED":
                    pending_admin.append(ticker)
                elif event_type == "INVESTMENT_WARNING":
                    pending_warning.append(ticker)
            elif action == "foreign":
                foreign_tickers.append(ticker)
            elif action == "clear":
                bydd = bydd_by_ticker.get(ticker)
                if bydd and bydd["stock_type"] != "SPAC":
                    spac_mismatches.append(
                        {
                            "ticker": ticker,
                            "name": name,
                            "stock_type": bydd["stock_type"],
                        }
                    )

        update_rows.append(
            {
                "ticker": ticker,
                "isin": bi.ISU_CD,
                "name": name,
                "listed_date": listed_iso,
                "market_division": md_value,
            }
        )

        bydd = bydd_by_ticker.get(ticker)
        if bydd and name and bydd["name"] != name:
            name_changes.append(
                {"ticker": ticker, "old": bydd["name"], "new": name}
            )

        if not bydd:
            continue
        regex_type = bydd["stock_type"]
        kind = (bi.KIND_STKCERT_TP_NM or "").strip()
        if regex_type in ("SPAC", "REIT") or not kind:
            continue
        norm_kind = _normalize_kind(kind)
        if norm_kind is None:
            continue
        if regex_type != norm_kind:
            mismatches.append(
                {"ticker": ticker, "regex_type": regex_type, "kind": kind, "name": name}
            )

    now_iso = datetime.now().isoformat(timespec="seconds")
    today_iso = date.today().isoformat()
    new_admin_inserts = 0
    new_warning_inserts = 0
    with get_connection() as conn:
        conn.executemany(
            """
            UPDATE stocks
            SET isin = :isin,
                name = :name,
                listed_date = :listed_date,
                market_division = :market_division
            WHERE ticker = :ticker
            """,
            update_rows,
        )
        if foreign_tickers:
            conn.executemany(
                "UPDATE stocks SET stock_type='FOREIGN' WHERE ticker=?",
                [(t,) for t in foreign_tickers],
            )
        conn.execute(
            "DELETE FROM data_anomaly_log WHERE anomaly_type='STOCK_TYPE_MISMATCH'"
        )
        if mismatches:
            conn.executemany(
                """
                INSERT INTO data_anomaly_log
                (ticker, anomaly_type, details, severity, detected_at)
                VALUES (?, 'STOCK_TYPE_MISMATCH', ?, 'WARN', ?)
                """,
                [
                    (
                        m["ticker"],
                        json.dumps(
                            {
                                "regex_type": m["regex_type"],
                                "krx_kind_type": m["kind"],
                                "name": m["name"],
                            },
                            ensure_ascii=False,
                        ),
                        now_iso,
                    )
                    for m in mismatches
                ],
            )

        # Status events: insert only if no active event already exists for (ticker, type).
        for tickers, event_type in [
            (pending_admin, "ADMIN_DESIGNATED"),
            (pending_warning, "INVESTMENT_WARNING"),
        ]:
            existing = {
                r[0]
                for r in conn.execute(
                    "SELECT ticker FROM stock_status_events "
                    "WHERE event_type=? AND end_date IS NULL",
                    (event_type,),
                )
            }
            new_tickers = [t for t in tickers if t not in existing]
            if new_tickers:
                conn.executemany(
                    """
                    INSERT INTO stock_status_events
                    (ticker, event_type, start_date, end_date, reason, source, collected_at)
                    VALUES (?, ?, ?, NULL, ?, 'KRX_base_info', ?)
                    """,
                    [
                        (t, event_type, today_iso, "지정일 불명 (수집 시점)", now_iso)
                        for t in new_tickers
                    ],
                )
                if event_type == "ADMIN_DESIGNATED":
                    new_admin_inserts = len(new_tickers)
                else:
                    new_warning_inserts = len(new_tickers)

    return {
        "base_info_total": len(update_rows),
        "kospi_count": len(kospi),
        "kosdaq_count": len(kosdaq),
        "mismatches": mismatches,
        "name_changes": name_changes,
        "foreign_count": len(foreign_tickers),
        "spac_mismatches": spac_mismatches,
        "pending_admin": len(pending_admin),
        "pending_warning": len(pending_warning),
        "new_admin_inserts": new_admin_inserts,
        "new_warning_inserts": new_warning_inserts,
    }


def _print_report(
    bydd_records: list[dict],
    migration_stats: tuple[int, int],
    enrich_stats: dict,
    api_calls: int,
    elapsed: float,
) -> None:
    by_market = {"KOSPI": 0, "KOSDAQ": 0}
    by_type: dict[str, int] = {}
    for r in bydd_records:
        by_market[r["market"]] += 1
        by_type[r["stock_type"]] = by_type.get(r["stock_type"], 0) + 1

    moved, nullified = migration_stats
    mismatches = enrich_stats["mismatches"]

    with get_connection() as conn:
        listed_filled = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE listed_date IS NOT NULL"
        ).fetchone()[0]
        isin_filled = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE isin IS NOT NULL"
        ).fetchone()[0]
        md_kosdaq = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE market='KOSDAQ' AND market_division IS NOT NULL"
        ).fetchone()[0]
        md_kospi = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE market='KOSPI' AND market_division IS NOT NULL"
        ).fetchone()[0]
        sector_nonnull = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE sector IS NOT NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        anomaly_count = conn.execute(
            "SELECT COUNT(*) FROM data_anomaly_log WHERE anomaly_type='STOCK_TYPE_MISMATCH'"
        ).fetchone()[0]

    print()
    print("====================================")
    print("Phase 1 Step 1b — Stocks Meta Report")
    print("====================================")
    print()
    print("Market distribution:")
    print(f"  KOSPI:  {by_market['KOSPI']} stocks")
    print(f"  KOSDAQ: {by_market['KOSDAQ']} stocks")
    print(f"  Total:  {total} stocks")
    print()
    print("Stock type distribution:")
    for t in ("COMMON", "PREFERRED", "SPAC", "REIT", "OTHER"):
        print(f"  {t+':':<10} {by_type.get(t, 0)}")
    print()
    print("Samples by type:")
    for t in ("COMMON", "PREFERRED", "SPAC", "REIT"):
        samples = _sample_by_type(bydd_records, t, n=3)
        if not samples:
            continue
        print(f"  [{t}]")
        for s in samples:
            pt = f"  parent={s['parent_ticker']}" if s["parent_ticker"] else ""
            print(f"    {s['ticker']}  {s['name']:<18} {s['market']}{pt}")
    print()
    print("Sector migration:")
    print(f"  sector → market_division moved: {moved} rows")
    print(f"  sector set to NULL:             {nullified} rows")
    print(f"  sector currently NOT NULL:      {sector_nonnull} (expect 0)")
    print()
    print("Enrichment stats (base_info):")
    print(f"  listed_date 채움률:      {listed_filled} / {total} ({listed_filled/total*100:.1f}%)")
    print(f"  isin 채움률:             {isin_filled} / {total} ({isin_filled/total*100:.1f}%)")
    print(
        f"  market_division 채움률:  KOSDAQ {md_kosdaq}/{by_market['KOSDAQ']}, "
        f"KOSPI {md_kospi}/{by_market['KOSPI']}"
    )
    print(f"  name updates from ISU_ABBRV: {len(enrich_stats['name_changes'])} rows")
    print()
    print("Status routing (SECT_TP_NM):")
    print(f"  FOREIGN stock_type 적용:        {enrich_stats['foreign_count']} rows")
    print(
        f"  ADMIN_DESIGNATED events seen:    {enrich_stats['pending_admin']} "
        f"(new inserts: {enrich_stats['new_admin_inserts']})"
    )
    print(
        f"  INVESTMENT_WARNING events seen:  {enrich_stats['pending_warning']} "
        f"(new inserts: {enrich_stats['new_warning_inserts']})"
    )
    spac_mm = enrich_stats["spac_mismatches"]
    print(f"  SPAC market_division mismatch:   {len(spac_mm)} (logged only)")
    if spac_mm:
        for m in spac_mm[:5]:
            print(f"    {m['ticker']}  {m['name']:<18} stock_type={m['stock_type']}")
    print()
    print("Type mismatch (KIND_STKCERT_TP_NM vs regex):")
    print(f"  Total mismatches: {len(mismatches)}")
    pattern_counter = Counter(
        (m["regex_type"], m["kind"]) for m in mismatches
    )
    if pattern_counter:
        print("  By pattern:")
        for (rt, kind), n in pattern_counter.most_common():
            print(f"    regex={rt}, krx={kind}: {n}")
    print(f"  Anomaly log entries added: {anomaly_count}")
    if mismatches:
        print("  Top samples:")
        for m in mismatches[:5]:
            print(
                f"    {m['ticker']}  {m['name']:<18} "
                f"regex={m['regex_type']} krx={m['kind']}"
            )
    print()
    print("Collection stats:")
    print(f"  KRX API calls:   {api_calls}")
    print(f"  Elapsed:         {elapsed:.1f}s")
    print(f"  Saved to stocks: {total} rows")
    print()

    unresolved = [
        r for r in bydd_records if r["stock_type"] == "PREFERRED" and not r["parent_ticker"]
    ]
    if unresolved:
        print(f"Preferreds without resolved parent: {len(unresolved)}")
        for s in unresolved[:10]:
            print(f"  {s['ticker']}  {s['name']}  ({s['market']})")
        print()


def main() -> int:
    started = time.monotonic()
    client = KrxClient()

    base_date = _latest_krx_trading_day(client)
    bydd_records = collect_from_bydd_trd(client, base_date)
    migration_stats = migrate_sector_to_market_division()
    enrich_stats = enrich_from_base_info(client, base_date, bydd_records)

    elapsed = time.monotonic() - started
    _print_report(bydd_records, migration_stats, enrich_stats, client.call_count, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""KRX bydd_trd 과거 일자 시가총액 응답 검증.

Phase 1 Step 3a 탐색. 3개 과거 일자에 대해 KRX bydd_trd 응답을
stocks 테이블 기대값과 교차 비교하여, 폐지 종목 시총 포함 여부를 판정한다.

DB 수정 없음. 총 6회 API 호출 (3 날짜 × 2 시장).

Usage:
    python scripts/explore_krx_historical_bydd.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.db import get_connection  # noqa: E402
from src.data_pipeline.krx_client import KrxClient  # noqa: E402

# ── 테스트 일자 ──────────────────────────────────────────────
TEST_DATES = [
    ("2026-04-14", "최근 거래일 (기준선)"),
    ("2020-01-02", "중간 과거"),
    ("2014-01-02", "먼 과거 (범위 경계)"),
]

SAMSUNG_TICKER = "005930"


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def to_yyyymmdd(date_str: str) -> str:
    """'2026-04-14' → '20260414'"""
    return date_str.replace("-", "")


def get_expected_tickers(conn, date_str: str) -> dict[str, dict]:
    """stocks 테이블에서 해당 일자에 상장 중이었어야 할 종목 조회."""
    rows = conn.execute(
        """SELECT ticker, name, listed_date, delisted_date
           FROM stocks
           WHERE (listed_date IS NULL OR listed_date <= ?)
             AND (delisted_date IS NULL OR delisted_date > ?)""",
        (date_str, date_str),
    ).fetchall()
    return {r["ticker"]: dict(r) for r in rows}


def run_test(client: KrxClient, conn, date_str: str, label: str) -> dict:
    """하나의 테스트 일자에 대해 KOSPI+KOSDAQ 조회 및 교차 비교."""
    section(f"Test: {date_str} ({label})")
    yyyymmdd = to_yyyymmdd(date_str)

    # KRX API 호출
    kospi_rows = client.get_listed_stocks("KOSPI", yyyymmdd)
    kosdaq_rows = client.get_listed_stocks("KOSDAQ", yyyymmdd)
    all_rows = kospi_rows + kosdaq_rows

    print(f"KOSPI 응답 건수:  {len(kospi_rows)}")
    print(f"KOSDAQ 응답 건수: {len(kosdaq_rows)}")
    print(f"총 종목 수:       {len(all_rows)}")

    # 응답 ticker 추출 (ISU_CD는 ISIN이므로, 단축코드 필드 탐색)
    # KrxStockMeta.isu_cd가 6자리 단축코드인지, ISIN인지 확인
    response_map: dict[str, dict] = {}
    for row in all_rows:
        # extra fields에서 ISU_SRT_CD (단축코드) 탐색
        extras = row.model_extra or {}
        short_code = extras.get("ISU_SRT_CD", "").strip()
        if short_code and short_code.isdigit():
            ticker = short_code.zfill(6)
        else:
            # fallback: isu_cd가 6자리면 그대로 사용
            ticker = row.isu_cd if len(row.isu_cd) == 6 else row.isu_cd
        response_map[ticker] = {
            "name": row.isu_nm,
            **extras,
        }

    response_tickers = set(response_map.keys())

    # 삼성전자 MKTCAP/LIST_SHRS 확인 (첫 번째 테스트만)
    samsung_data = response_map.get(SAMSUNG_TICKER)
    if samsung_data:
        print(f"\n삼성전자({SAMSUNG_TICKER}) 샘플:")
        mktcap_raw = samsung_data.get("MKTCAP", "(없음)")
        list_shrs_raw = samsung_data.get("LIST_SHRS", "(없음)")
        print(f"  MKTCAP:    {mktcap_raw!r}  (type={type(mktcap_raw).__name__})")
        print(f"  LIST_SHRS: {list_shrs_raw!r}  (type={type(list_shrs_raw).__name__})")

        # 파싱 시도
        for field_name, raw in [("MKTCAP", mktcap_raw), ("LIST_SHRS", list_shrs_raw)]:
            if isinstance(raw, str):
                cleaned = raw.replace(",", "").strip()
                try:
                    val = int(cleaned)
                    print(f"  {field_name} 파싱: {val:,}")
                except ValueError:
                    print(f"  {field_name} 파싱 실패: {cleaned!r}")

    # 응답 raw 필드 목록 (첫 1건)
    if all_rows:
        first = all_rows[0]
        extras = first.model_extra or {}
        all_fields = ["ISU_CD (isu_cd)", "ISU_NM (isu_nm)"] + sorted(extras.keys())
        print(f"\n응답 raw 필드 목록: {all_fields}")

        # raw JSON 샘플 (첫 1건)
        print(f"\n응답 raw 샘플 (첫 1건):")
        print(f"  isu_cd: {first.isu_cd!r}")
        print(f"  isu_nm: {first.isu_nm!r}")
        for k in sorted(extras.keys()):
            print(f"  {k}: {extras[k]!r}")

    # stocks 테이블 기대값
    expected = get_expected_tickers(conn, date_str)
    expected_tickers = set(expected.keys())

    print(f"\nstocks 테이블 기준 예상 (listed <= {date_str}, delisted > {date_str} or NULL):")
    print(f"  총: {len(expected_tickers)}")

    # 교차 비교
    in_both = response_tickers & expected_tickers
    only_in_response = response_tickers - expected_tickers
    only_in_expected = expected_tickers - response_tickers

    print(f"\n교차 비교:")
    print(f"  응답과 예상 공통:         {len(in_both)}")
    print(f"  응답에만 있음:            {len(only_in_response)}")
    print(f"  예상에만 있음 (미반환):   {len(only_in_expected)}  ← 핵심 지표")

    # 응답에만 있는 종목 샘플
    if only_in_response:
        print(f"\n응답에만 있는 종목 샘플 (최대 5건):")
        for t in sorted(only_in_response)[:5]:
            info = response_map[t]
            print(f"  {t}  {info.get('name', '?')}")

    # 예상에만 있는 종목 샘플 (핵심)
    if only_in_expected:
        print(f"\n예상에만 있는 종목 샘플 (최대 10건):")
        for t in sorted(only_in_expected)[:10]:
            info = expected[t]
            print(f"  {t}  {info['name']:<16}  listed={info['listed_date']}  "
                  f"delisted={info['delisted_date']}")

        # 폐지 종목 중 미반환 비율
        delisted_missing = [
            t for t in only_in_expected
            if expected[t]["delisted_date"] is not None
        ]
        active_missing = [
            t for t in only_in_expected
            if expected[t]["delisted_date"] is None
        ]
        print(f"\n  미반환 중 폐지 종목: {len(delisted_missing)}")
        print(f"  미반환 중 활성 종목: {len(active_missing)}")

    return {
        "date": date_str,
        "label": label,
        "kospi_count": len(kospi_rows),
        "kosdaq_count": len(kosdaq_rows),
        "total_response": len(all_rows),
        "total_expected": len(expected_tickers),
        "in_both": len(in_both),
        "only_in_response": len(only_in_response),
        "only_in_expected": len(only_in_expected),
    }


def main() -> int:
    client = KrxClient()
    results = []

    with get_connection() as conn:
        for date_str, label in TEST_DATES:
            result = run_test(client, conn, date_str, label)
            results.append(result)

    # ── 판정 ─────────────────────────────────────────────────
    section("판정")

    for r in results:
        print(f"\n{r['date']} ({r['label']}):")
        print(f"  응답 {r['total_response']} / 예상 {r['total_expected']} / "
              f"공통 {r['in_both']} / 미반환 {r['only_in_expected']}")

    # 가설 판정
    past_tests = [r for r in results if r["date"] != "2026-04-14"]
    max_missing = max(r["only_in_expected"] for r in past_tests) if past_tests else 0

    print()
    if max_missing <= 10:
        print("가설 1 (historical snapshot 제공):")
        print(f"  '예상에만 있음' 최대 {max_missing}건 — 극소수")
        print("  → Step 3 본격 수집 가능, 폐지 종목 시총 자동 포함")
        verdict = "가설 1"
    elif max_missing <= 50:
        print("불확실:")
        print(f"  '예상에만 있음' 최대 {max_missing}건 — 소수이나 무시 불가")
        print("  → 추가 분석 필요 (미반환 종목의 stock_type 분포 등)")
        verdict = "불확실"
    else:
        print("가설 2 (현재 시점 리스트만 반환):")
        print(f"  '예상에만 있음' 최대 {max_missing}건 — 다수")
        print("  → 폐지 종목 시총은 별도 경로 필요")
        verdict = "가설 2"

    print(f"\n본 탐색 결론: {verdict}")
    print(f"\n총 KRX API 호출: {client.call_count}회")

    return 0


if __name__ == "__main__":
    sys.exit(main())

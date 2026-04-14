"""KRX OpenAPI 수정주가(split-adjusted) 여부 검증.

액면분할 이력이 명확한 3종목을 분할 전후 날짜로 조회하여,
KRX OpenAPI 가 raw(비수정) 가격을 반환하는지 adjusted(수정주가)를 반환하는지 판별.

검증 케이스:
- 삼성전자 (005930): 2018-05-04 50:1 분할
- NAVER    (035420): 2018-10-12 1:5 분할
- 카카오   (035720): 2021-04-15 1:5 분할

판정:
- 분할 직전 종가가 raw_expected 근처 → raw 반환 (수정주가 변환 로직 필요)
- 분할 직전 종가가 adjusted_expected 근처 → adjusted 반환 (그대로 사용 가능)

Usage:
    python scripts/verify_krx_split_adjustment.py
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

# Windows cp949 환경에서 UTF-8 출력 강제
sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

API_KEY = os.getenv("KRX_API_KEY")
if not API_KEY:
    print("ERROR: KRX_API_KEY 가 .env 에 설정되지 않음")
    sys.exit(1)

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
ENDPOINT = "/sto/stk_bydd_trd"  # KOSPI 일별매매정보

VERIFICATION_CASES = [
    {
        "ticker": "005930",
        "name": "삼성전자",
        "event": "액면분할 50:1",
        "event_date": "2018-05-04",
        "check_dates": ["2018-04-30", "2018-05-02", "2018-05-08", "2018-05-09"],
        "raw_expected": 2650000,     # 분할 직전 raw 가격
        "adjusted_expected": 53000,   # 분할 후 adjusted 가격
    },
    {
        "ticker": "035420",
        "name": "NAVER",
        "event": "액면분할 1:5",
        "event_date": "2018-10-12",
        "check_dates": ["2018-10-10", "2018-10-11", "2018-10-15", "2018-10-16"],
        "raw_expected": 700000,
        "adjusted_expected": 140000,
    },
    {
        "ticker": "035720",
        "name": "카카오",
        "event": "액면분할 1:5",
        "event_date": "2021-04-15",
        "check_dates": ["2021-04-12", "2021-04-14", "2021-04-15", "2021-04-19"],
        "raw_expected": 558000,
        "adjusted_expected": 111600,
    },
]


def fetch_raw(date_yyyymmdd: str) -> list[dict]:
    url = BASE_URL + ENDPOINT
    headers = {"AUTH_KEY": API_KEY.strip()}
    resp = requests.get(
        url, headers=headers, params={"basDd": date_yyyymmdd}, timeout=30
    )
    resp.raise_for_status()
    return resp.json().get("OutBlock_1", [])


def find_ticker(records: list[dict], ticker: str) -> dict | None:
    for row in records:
        # KRX 응답: ISU_CD (기존 ISU_SRT_CD 에서 변경됨)
        if row.get("ISU_CD") == ticker or row.get("ISU_SRT_CD") == ticker:
            return row
    return None


def to_yyyymmdd(date_str: str) -> str:
    """'2018-05-04' → '20180504'."""
    return date_str.replace("-", "")


def judge(close: int, raw_expected: int, adj_expected: int) -> str:
    """종가가 raw/adjusted 중 어느 기준에 가까운지."""
    dist_raw = abs(close - raw_expected) / raw_expected
    dist_adj = abs(close - adj_expected) / adj_expected
    if dist_raw < 0.1 and dist_raw < dist_adj:
        return "RAW"
    if dist_adj < 0.1 and dist_adj < dist_raw:
        return "ADJUSTED"
    return "?"


def verify_case(case: dict) -> None:
    print(f"\n{'=' * 80}")
    print(f"[{case['name']} / {case['ticker']}] {case['event']} — {case['event_date']}")
    print(f"raw_expected={case['raw_expected']:,}  adjusted_expected={case['adjusted_expected']:,}")
    print("=" * 80)

    # 1) raw 응답 dump (분할 직전 첫 날짜로)
    probe_date = to_yyyymmdd(case["check_dates"][0])
    try:
        records = fetch_raw(probe_date)
        sample = find_ticker(records, case["ticker"])
        if sample:
            print(f"\n=== {case['name']} ({case['ticker']}) raw response @ {case['check_dates'][0]} ===")
            print(json.dumps(sample, ensure_ascii=False, indent=2)[:1500])
            print(f"\n=== Available fields: {list(sample.keys())} ===")
        else:
            print(f"(probe: {case['check_dates'][0]} 응답 {len(records)}건 중 {case['ticker']} 없음)")
    except Exception as e:
        print(f"(probe failed: {e})")

    # 2) 날짜별 종가 테이블
    print()
    print(f"{'날짜':<12} {'종가(원)':>15} {'거래량':>18}  판정  이벤트")
    print("-" * 80)

    for date_str in case["check_dates"]:
        date = to_yyyymmdd(date_str)
        try:
            records = fetch_raw(date)
            row = find_ticker(records, case["ticker"])
            if row is None:
                print(f"{date_str:<12} {'(데이터 없음)':>15}")
                continue
            close = int(row["TDD_CLSPRC"].replace(",", ""))
            volume = int(row["ACC_TRDVOL"].replace(",", ""))

            verdict = judge(close, case["raw_expected"], case["adjusted_expected"])

            marker = ""
            if date_str == case["event_date"]:
                marker = f" ← {case['event']} 당일"
            elif date_str < case["event_date"]:
                marker = " ← 분할 이전"
            else:
                marker = " ← 분할 이후"

            print(f"{date_str:<12} {close:>15,} {volume:>18,}  {verdict:<8}{marker}")
        except Exception as e:
            print(f"{date_str:<12} ERROR: {e}")


def main():
    for case in VERIFICATION_CASES:
        verify_case(case)

    print()
    print("=" * 80)
    print("전체 해석:")
    print("  - 분할 이전 날짜의 판정이 모두 'RAW'  → KRX 는 historical raw 반환 → 수정주가 변환 필요")
    print("  - 분할 이전 날짜의 판정이 모두 'ADJUSTED' → KRX 는 사후 수정주가 반환 → 그대로 사용")
    print("  - 섞여 있으면 → 종목/시기별 정책 차이 가능성, 수동 검토 필요")
    print("=" * 80)


if __name__ == "__main__":
    main()

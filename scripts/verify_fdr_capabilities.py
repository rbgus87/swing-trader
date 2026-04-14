"""FDR(FinanceDataReader) Phase 1 데이터 소스 적합성 검증.

검증 항목:
 1. 전종목 리스트 — fdr.StockListing('KRX') 로 KOSPI+KOSDAQ 전체 얻을 수 있는지
 2. 12년치 일봉 수집 — 삼성전자 2013~2025 전 기간 한 번에 받을 수 있는지, 속도
 3. 시가총액 포함 여부 — 리스팅/일봉 어느 쪽에서 시총 제공되는지
 4. (보너스) Rate limit / 연속 호출 안정성 — 상위 5종목 각각 12년치 연속 호출

Usage:
    python scripts/verify_fdr_capabilities.py
"""

import sys
import time

# Windows cp949 환경에서 UTF-8 출력 강제
sys.stdout.reconfigure(encoding="utf-8")

import FinanceDataReader as fdr  # noqa: E402


def section(title: str) -> None:
    print(f"\n{'=' * 80}")
    print(title)
    print("=" * 80)


# ─────────────────────────────────────────────────────────────
# 1. 전종목 리스트
# ─────────────────────────────────────────────────────────────
section("1. 전종목 리스트 — KOSPI + KOSDAQ 각각 조회")

# KRX 통합 조회는 JSON 파싱 에러 발생 → KOSPI/KOSDAQ 분리
t0 = time.time()
try:
    kospi = fdr.StockListing("KOSPI")
    kospi_ok = True
except Exception as e:
    print(f"KOSPI 조회 실패: {e}")
    kospi = None
    kospi_ok = False

try:
    kosdaq = fdr.StockListing("KOSDAQ")
    kosdaq_ok = True
except Exception as e:
    print(f"KOSDAQ 조회 실패: {e}")
    kosdaq = None
    kosdaq_ok = False

elapsed = time.time() - t0

if kospi_ok:
    print(f"\nKOSPI: {len(kospi):,}종목")
    print(f"컬럼: {list(kospi.columns)}")
    print(kospi.head(3).to_string())

if kosdaq_ok:
    print(f"\nKOSDAQ: {len(kosdaq):,}종목")
    print(f"컬럼: {list(kosdaq.columns)}")
    print(kosdaq.head(3).to_string())

print(f"\n총 소요: {elapsed:.2f}초")

# 이후 단계에서 사용할 리스팅 (KOSPI 기준)
listing = kospi if kospi_ok else (kosdaq if kosdaq_ok else None)


# ─────────────────────────────────────────────────────────────
# 2. 12년치 일봉 — 삼성전자
# ─────────────────────────────────────────────────────────────
section("2. 12년치 일봉 — 삼성전자 2013-01-01 ~ 2025-04-14")

t0 = time.time()
samsung = fdr.DataReader("005930", "2013-01-01", "2025-04-14")
elapsed = time.time() - t0

print(f"소요 시간: {elapsed:.2f}초")
print(f"행 수: {len(samsung):,}")
print(f"컬럼: {list(samsung.columns)}")
print(f"기간: {samsung.index.min()} ~ {samsung.index.max()}")
print()
print("처음 3행:")
print(samsung.head(3).to_string())
print()
print("마지막 3행:")
print(samsung.tail(3).to_string())


# ─────────────────────────────────────────────────────────────
# 3. 시가총액 제공 여부
# ─────────────────────────────────────────────────────────────
section("3. 시가총액 포함 여부")

# 3-1. 리스팅에 있는가
if listing is not None:
    cap_cols = [c for c in listing.columns if "cap" in c.lower() or "market" in c.lower() or "시총" in c]
    print(f"StockListing 내 시총 관련 컬럼: {cap_cols}")

    if cap_cols:
        for col in cap_cols:
            sample_cols = [c for c in ["Code", "Name", col] if c in listing.columns]
            print(f"\n샘플 ({col}):")
            print(listing[sample_cols].head(3).to_string())
else:
    print("StockListing 조회 실패 — 시총 확인 불가")

# 3-2. DataReader 에 있는가
daily_cap_cols = [c for c in samsung.columns if "cap" in c.lower() or "market" in c.lower() or "시총" in c]
print(f"\nDataReader 일봉 내 시총 관련 컬럼: {daily_cap_cols if daily_cap_cols else '(없음)'}")


# ─────────────────────────────────────────────────────────────
# 4. 연속 호출 안정성 (상위 5종목 12년치)
# ─────────────────────────────────────────────────────────────
section("4. 연속 호출 안정성 — 상위 5종목 각 12년치")

test_codes = ["005930", "000660", "035420", "035720", "051910"]  # 삼성/SK하이닉스/네이버/카카오/LG화학
timings = []

for code in test_codes:
    t0 = time.time()
    try:
        df = fdr.DataReader(code, "2013-01-01", "2025-04-14")
        el = time.time() - t0
        timings.append(el)
        print(f"  {code}: {len(df):,}행, {el:.2f}초")
    except Exception as e:
        print(f"  {code}: ERROR — {e}")
        timings.append(None)

valid = [t for t in timings if t is not None]
if valid:
    avg = sum(valid) / len(valid)
    print(f"\n평균: {avg:.2f}초  /  합계: {sum(valid):.2f}초")
    # 전종목 ~2700개 × 평균 → 예상 시간
    if listing is not None:
        estimated_full = avg * len(listing)
        print(f"샘플 리스팅 {len(listing):,}개 순차 수집 시 예상: {estimated_full / 60:.1f}분")
    # 대략 2700 전종목 기준
    print(f"전체 2,700종목 가정: {avg * 2700 / 60:.1f}분 ({avg * 2700:.0f}초)")


print(f"\n{'=' * 80}\n검증 완료")

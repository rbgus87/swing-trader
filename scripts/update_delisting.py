"""상장폐지 종목 데이터 갱신 — 단일 진입점.

KRX 정보데이터시스템(data.krx.co.kr)에서 받은 `상장폐지현황.xls`를
`data/raw/delisting/` 에 덮어쓴 후 이 스크립트 실행.

사용:
    python scripts/update_delisting.py
    python scripts/update_delisting.py --skip-candles    # 일봉 수집 생략
    python scripts/update_delisting.py --skip-infer      # delisted_date 추론 생략

월 1회 권장. 모든 단계는 멱등(반복 실행 안전).

내부 흐름:
    1. import_delisted_list.main()  — xls → stocks 테이블 INSERT/UPDATE
    2. collect_daily_candles.main() — 신규 폐지 종목 일봉 수집
    3. infer_delisted()             — 일봉 마지막 거래일로 delisted_date 역산
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from loguru import logger

from src.data_pipeline import import_delisted_list, collect_daily_candles, infer_delisted


def banner(text: str) -> None:
    line = "=" * 50
    print(line)
    print(text)
    print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="상장폐지 데이터 갱신")
    parser.add_argument("--skip-candles", action="store_true",
                        help="일봉 수집 단계 생략 (xls 파싱만)")
    parser.add_argument("--skip-infer", action="store_true",
                        help="delisted_date 추론 단계 생략")
    args = parser.parse_args()

    delist_file = _PROJECT_ROOT / "data" / "raw" / "delisting" / "상장폐지현황.xls"
    if not delist_file.exists():
        logger.error(f"파일 없음: {delist_file}")
        logger.error("data.krx.co.kr 에서 상장폐지현황.xls 를 받아 위 경로에 저장하세요.")
        return 1

    banner("Step 1/3: Import delisting list to stocks")
    rc = import_delisted_list.main()
    if rc != 0:
        logger.error(f"import 실패 (rc={rc}). 중단.")
        return rc

    if args.skip_candles:
        logger.info("Step 2/3: SKIP (--skip-candles)")
    else:
        banner("Step 2/3: Collect daily candles (new delisted only)")
        # .sh와 동일하게 BACKFILL 모드 (default kwargs)
        collect_daily_candles.main()

    if args.skip_infer:
        logger.info("Step 3/3: SKIP (--skip-infer)")
    else:
        banner("Step 3/3: Infer delisted_date (fallback)")
        infer_delisted.infer_delisted()

    print()
    logger.info("Delisting data update complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

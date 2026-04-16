#!/bin/bash
# Phase 1 Step 2b — 상장폐지 종목 데이터 갱신
#
# 사용법:
#   1. data.krx.co.kr에서 새 상장폐지현황.xls 다운로드
#   2. data/raw/delisting/상장폐지현황.xls 에 덮어쓰기
#   3. 이 스크립트 실행: bash scripts/update_delisting.sh
#
# 월 1회 정도 갱신 권장.

set -e

cd "$(dirname "$0")/.."

DELIST_FILE="data/raw/delisting/상장폐지현황.xls"

if [ ! -f "$DELIST_FILE" ]; then
    echo "ERROR: $DELIST_FILE not found"
    echo "Please download from data.krx.co.kr and place it there."
    exit 1
fi

echo "=========================================="
echo "Step 1/3: Import delisting list to stocks"
echo "=========================================="
python src/data_pipeline/import_delisted_list.py

echo ""
echo "=========================================="
echo "Step 2/3: Collect daily candles (new delisted only)"
echo "=========================================="
python src/data_pipeline/collect_daily_candles.py

echo ""
echo "=========================================="
echo "Step 3/3: Infer delisted_date (fallback)"
echo "=========================================="
python src/data_pipeline/infer_delisted.py

echo ""
echo "Delisting data update complete."

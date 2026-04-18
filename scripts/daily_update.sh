#!/bin/bash
# Phase 3 — 일일 데이터 갱신 파이프라인
#
# 매일 장 마감 후 실행 (15:40 이후 권장)
# 순서: 신규 상장 감지 → 일봉 증분 → 시총 증분
#
# 사용법:
#   bash scripts/daily_update.sh
#
# cron 등록 예시 (매일 16:00):
#   0 16 * * 1-5 cd /path/to/swing-trader && bash scripts/daily_update.sh >> logs/daily_update.log 2>&1

set -e
cd "$(dirname "$0")/.."

LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

echo "$LOG_PREFIX === Daily Update Start ==="

echo "$LOG_PREFIX Step 1/3: 신규 상장 감지..."
python src/data_pipeline/detect_new_listings.py

echo "$LOG_PREFIX Step 2/3: 일봉 증분 수집..."
python src/data_pipeline/collect_daily_candles.py --incremental

echo "$LOG_PREFIX Step 3/3: 시총 증분 수집..."
python src/data_pipeline/collect_market_cap.py

echo "$LOG_PREFIX === Daily Update Complete ==="

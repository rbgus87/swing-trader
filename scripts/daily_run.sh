#!/bin/bash
# Phase 3 — 매일 실행 (데이터 갱신 + 시그널 생성 + 주문)
#
# 매일 장 마감 후 실행 (16:00 이후 권장)
#
# 사용법:
#   bash scripts/daily_run.sh
#
# cron (매일 16:00, 월~금):
#   0 16 * * 1-5 cd /path/to/swing-trader && bash scripts/daily_run.sh >> logs/daily_run.log 2>&1

set -e
cd "$(dirname "$0")/.."

mkdir -p logs
LOG="logs/daily_run_$(date '+%Y%m%d').log"

echo "$(date) === Daily Run Start ===" | tee -a "$LOG"

echo "$(date) Step 1/2: Data Update..." | tee -a "$LOG"
bash scripts/daily_update.sh 2>&1 | tee -a "$LOG"

echo "$(date) Step 2/2: Signal Engine..." | tee -a "$LOG"
python src/engine/orchestrator.py 2>&1 | tee -a "$LOG"

echo "$(date) === Daily Run Complete ===" | tee -a "$LOG"

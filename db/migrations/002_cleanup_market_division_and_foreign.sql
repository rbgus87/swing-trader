-- Migration 002: market_division 정규화 + FOREIGN stock_type 도입
-- 2026-04-14 Phase 1 Step 1b-2b
--
-- 스키마 변경 없음. 데이터 정정은 별도 Python 스크립트에서 수행:
--   python src/data_pipeline/migrate_market_division_cleanup.py
-- 본 파일은 버전 트래킹 목적의 no-op marker.

SELECT 1;

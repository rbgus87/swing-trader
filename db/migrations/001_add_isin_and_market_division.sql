-- Migration 001: stocks 테이블에 isin, market_division 컬럼 추가
-- 2026-04-14 Phase 1 Step 1b-1/1b-2

ALTER TABLE stocks ADD COLUMN isin TEXT;
ALTER TABLE stocks ADD COLUMN market_division TEXT;  -- KOSDAQ 소속부 (산업 분류 아님)

CREATE INDEX idx_stocks_isin ON stocks(isin);

-- Migration 004: market_cap 수집 체크포인트 테이블
-- 2026-04-16 Phase 1 Step 3b

CREATE TABLE IF NOT EXISTS market_cap_collection_log (
    market TEXT NOT NULL,          -- 'KOSPI' / 'KOSDAQ'
    date DATE NOT NULL,
    attempted_at DATETIME NOT NULL,
    status TEXT NOT NULL,          -- 'SUCCESS' / 'FAILED' / 'SKIPPED'
    rows_saved INTEGER,
    error_message TEXT,
    PRIMARY KEY (market, date)
);
CREATE INDEX IF NOT EXISTS idx_mcap_log_status ON market_cap_collection_log(status);

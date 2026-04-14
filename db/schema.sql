-- ============================================================
-- swing-trader Phase 1 Schema
-- Created: 2026-04-14
-- Data sources: FDR (OHLCV) + KRX OpenAPI (meta/cap/events)
-- ============================================================

CREATE TABLE stocks (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL,                -- 'KOSPI' / 'KOSDAQ'
    sector TEXT,                         -- 산업 분류 (별도 Phase에서 채움)
    stock_type TEXT NOT NULL,            -- 'COMMON' / 'PREFERRED' / 'SPAC' / 'REIT' / 'FOREIGN' / 'ETF' / 'ETN' / 'OTHER'
    parent_ticker TEXT,
    listed_date DATE,
    delisted_date DATE,
    delisting_reason TEXT,
    first_candle_date DATE,
    last_candle_date DATE,
    isin TEXT,                           -- KRX ISU_CD (KR7XXXXXXXXX)
    market_division TEXT,                -- KOSDAQ 소속부 (산업 분류 아님)
    last_updated DATETIME NOT NULL
);
CREATE INDEX idx_stocks_market ON stocks(market);
CREATE INDEX idx_stocks_active ON stocks(delisted_date) WHERE delisted_date IS NULL;
CREATE INDEX idx_stocks_type ON stocks(stock_type);
CREATE INDEX idx_stocks_isin ON stocks(isin);

CREATE TABLE daily_candles (
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    change_rate REAL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX idx_candles_date ON daily_candles(date);

CREATE TABLE market_cap_history (
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    market_cap INTEGER NOT NULL,
    shares_outstanding INTEGER NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX idx_mcap_date ON market_cap_history(date);

CREATE TABLE stock_status_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    event_type TEXT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE,
    reason TEXT,
    source TEXT NOT NULL,
    collected_at DATETIME NOT NULL
);
CREATE INDEX idx_status_ticker ON stock_status_events(ticker);
CREATE INDEX idx_status_dates ON stock_status_events(start_date, end_date);
CREATE INDEX idx_status_type ON stock_status_events(event_type);

CREATE TABLE ticker_reuse_events (
    original_ticker TEXT NOT NULL,
    reuse_start_date DATE NOT NULL,
    old_company_name TEXT NOT NULL,
    new_company_name TEXT NOT NULL,
    old_delisted_date DATE,
    notes TEXT,
    verified BOOLEAN DEFAULT 0,
    PRIMARY KEY (original_ticker, reuse_start_date)
);

CREATE TABLE name_change_events (
    ticker TEXT NOT NULL,
    change_date DATE NOT NULL,
    old_name TEXT NOT NULL,
    new_name TEXT NOT NULL,
    PRIMARY KEY (ticker, change_date)
);

CREATE TABLE collection_log (
    ticker TEXT NOT NULL,
    data_type TEXT NOT NULL,
    last_collected_date DATE,
    last_attempt_at DATETIME NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    PRIMARY KEY (ticker, data_type)
);
CREATE INDEX idx_collection_status ON collection_log(status);

CREATE TABLE data_anomaly_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    date DATE,
    anomaly_type TEXT NOT NULL,
    details TEXT,
    severity TEXT NOT NULL,
    detected_at DATETIME NOT NULL,
    reviewed BOOLEAN DEFAULT 0
);
CREATE INDEX idx_anomaly_ticker ON data_anomaly_log(ticker);
CREATE INDEX idx_anomaly_severity ON data_anomaly_log(severity);

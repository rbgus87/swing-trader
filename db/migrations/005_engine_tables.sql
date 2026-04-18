-- Migration 005: Phase 3 엔진 테이블
-- positions: 보유 종목 관리
-- signals: 진입/청산 신호 기록
-- daily_portfolio_snapshot: 일별 포트폴리오 상태

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'TF',
    entry_date DATE NOT NULL,
    entry_price REAL NOT NULL,
    shares INTEGER NOT NULL,
    initial_shares INTEGER NOT NULL,
    atr_at_entry REAL NOT NULL,
    stop_price REAL NOT NULL,
    tp1_price REAL NOT NULL,
    highest_since_entry REAL NOT NULL,
    tp1_triggered INTEGER DEFAULT 0,
    status TEXT DEFAULT 'OPEN',
    exit_date DATE,
    exit_price REAL,
    exit_reason TEXT,
    pnl_amount REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    ticker TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    strategy TEXT DEFAULT 'TF',
    price REAL,
    reason TEXT,
    executed INTEGER DEFAULT 0,
    executed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);

CREATE TABLE IF NOT EXISTS daily_portfolio_snapshot (
    date DATE PRIMARY KEY,
    cash REAL NOT NULL,
    portfolio_value REAL NOT NULL,
    positions_count INTEGER NOT NULL,
    breadth REAL,
    gate_status TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

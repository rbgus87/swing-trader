"""Shared SQLite connection utility — Phase A: data/trade 역할 분리.

세 가지 connector를 제공한다:
  - get_data_db()     : swing_data.db (일봉/시총/지수/stocks)
  - get_trade_db()    : swing_trade.db (positions/trades/snapshot/signals)
  - get_combined_db() : data primary + trade ATTACHed as 'trade' schema
                        → trade.<table>로 매매 테이블 접근

하위 호환: get_connection() = get_data_db().

자동 마이그레이션: import 시 swing.db → swing_data.db,
swing_legacy.db → swing_trade.db로 1회 리네임.
"""
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.data_pipeline import DATA_DB_PATH, TRADE_DB_PATH


def _migrate_legacy_db_files() -> None:
    """첫 실행 시 옛 파일명을 새 파일명으로 리네임."""
    project_root = Path(DATA_DB_PATH).parent

    old_data = project_root / "swing.db"
    new_data = Path(DATA_DB_PATH)
    if old_data.exists() and not new_data.exists():
        shutil.move(str(old_data), str(new_data))
        # WAL/SHM 동반 이동 (있으면)
        for suffix in ("-wal", "-shm"):
            old_aux = project_root / f"swing.db{suffix}"
            if old_aux.exists():
                shutil.move(str(old_aux), str(new_data) + suffix)

    old_trade = project_root / "swing_legacy.db"
    new_trade = Path(TRADE_DB_PATH)
    if old_trade.exists() and not new_trade.exists():
        shutil.move(str(old_trade), str(new_trade))
        for suffix in ("-wal", "-shm"):
            old_aux = project_root / f"swing_legacy.db{suffix}"
            if old_aux.exists():
                shutil.move(str(old_aux), str(new_trade) + suffix)

    _migrate_snapshot_to_trade_db(new_data, new_trade)


def _migrate_snapshot_to_trade_db(data_db: Path, trade_db: Path) -> None:
    """data DB의 daily_portfolio_snapshot 데이터를 trade DB로 1회 이전.

    TradingEngine이 과거 swing.db에 기록한 GUI 표시용 스냅샷을 손실 없이
    옮긴 뒤, data DB에서 매매 전용 테이블(positions/signals/snapshot 등)을
    제거한다. 멱등 — 재실행해도 안전.
    """
    if not data_db.exists():
        return

    try:
        dconn = sqlite3.connect(str(data_db))
        dconn.row_factory = sqlite3.Row

        # 매매 전용 테이블 존재 여부
        trade_only_tables = (
            "positions", "signals", "daily_portfolio_snapshot",
            "trades", "daily_performance", "ohlcv_cache",
        )
        existing = {
            r[0] for r in dconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        present = [t for t in trade_only_tables if t in existing]

        # 스냅샷 데이터 이전
        if "daily_portfolio_snapshot" in present:
            rows = dconn.execute(
                "SELECT date, cash, portfolio_value, positions_count, "
                "breadth, gate_status, created_at "
                "FROM daily_portfolio_snapshot"
            ).fetchall()
            if rows:
                tconn = sqlite3.connect(str(trade_db))
                tconn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_portfolio_snapshot (
                        date DATE PRIMARY KEY,
                        cash REAL NOT NULL,
                        portfolio_value REAL NOT NULL,
                        positions_count INTEGER NOT NULL,
                        breadth REAL,
                        gate_status TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                for r in rows:
                    tconn.execute(
                        "INSERT OR IGNORE INTO daily_portfolio_snapshot "
                        "(date, cash, portfolio_value, positions_count, "
                        " breadth, gate_status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        tuple(r),
                    )
                tconn.commit()
                tconn.close()

        # signals 데이터 이전 (orchestrator)
        if "signals" in present:
            try:
                rows = dconn.execute(
                    "SELECT date, ticker, signal_type, strategy, price, "
                    "reason, executed, executed_at, created_at "
                    "FROM signals"
                ).fetchall()
                if rows:
                    tconn = sqlite3.connect(str(trade_db))
                    tconn.execute(
                        """
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
                        )
                        """
                    )
                    for r in rows:
                        tconn.execute(
                            "INSERT INTO signals "
                            "(date, ticker, signal_type, strategy, price, "
                            " reason, executed, executed_at, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            tuple(r),
                        )
                    tconn.commit()
                    tconn.close()
            except sqlite3.Error:
                pass  # 스키마 어긋나면 그냥 스킵

        # data DB에서 매매 전용 테이블 제거
        for t in present:
            try:
                dconn.execute(f"DROP TABLE IF EXISTS {t}")
            except sqlite3.Error:
                pass
        dconn.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            dconn.close()
        except Exception:
            pass


# 모듈 import 시 1회 실행
_migrate_legacy_db_files()


@contextmanager
def get_data_db() -> Iterator[sqlite3.Connection]:
    """데이터 DB 연결 — daily_candles, market_cap_history, index_daily, stocks."""
    conn = sqlite3.connect(str(DATA_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_trade_db() -> Iterator[sqlite3.Connection]:
    """매매 DB 연결 — positions, trades, daily_performance, snapshot, signals."""
    conn = sqlite3.connect(str(TRADE_DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_combined_db() -> Iterator[sqlite3.Connection]:
    """data DB primary + trade DB ATTACHed as 'trade'.

    Use:
        with get_combined_db() as conn:
            conn.execute("SELECT * FROM trade.positions")          # 매매
            conn.execute("SELECT * FROM daily_candles")            # 데이터
            conn.execute("SELECT p.*, s.name FROM trade.positions p"
                         " LEFT JOIN stocks s ON s.ticker = p.code")
    """
    conn = sqlite3.connect(str(DATA_DB_PATH))
    conn.row_factory = sqlite3.Row
    # ATTACH는 literal path만 받는다. 경로는 내부 상수 → SQL injection 위험 없음.
    conn.execute(f"ATTACH DATABASE '{str(TRADE_DB_PATH)}' AS trade")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 하위 호환: 기존 get_connection() 사용처를 데이터 DB로 자동 리다이렉트.
get_connection = get_data_db

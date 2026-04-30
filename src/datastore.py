"""SQLite 데이터 저장소.

포지션, 매매 기록, 일일 성과, OHLCV 캐시 등의 CRUD를 제공.
모든 쿼리는 파라미터 바인딩을 사용하여 SQL injection 방지.
멀티스레드 환경에서 안전하게 동작하도록 threading.Lock 적용.
"""

import sqlite3
import threading
from pathlib import Path

from loguru import logger
from src.models import Position, TradeRecord

# update_position에서 허용하는 컬럼 화이트리스트
_POSITION_UPDATABLE_COLUMNS = frozenset({
    "code", "name", "entry_date", "entry_price", "quantity",
    "stop_price", "target_price", "status", "updated_at",
    "high_since_entry", "hold_days", "partial_sold",
    # v2.5
    "initial_quantity", "tp2_price", "partial_sold_2",
})


class DataStore:
    """SQLite 기반 매매 데이터 저장소 (swing_trade.db).

    Args:
        db_path: SQLite 파일 경로. 기본값은 ``TRADE_DB_PATH`` (swing_trade.db).
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            from src.data_pipeline import TRADE_DB_PATH
            db_path = str(TRADE_DB_PATH)
        # 상대 경로면 앱 디렉토리 기준으로 해석 (exe 호환)
        if not Path(db_path).is_absolute():
            from src.utils.config import _get_app_dir
            db_path = str(_get_app_dir() / db_path)
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        """DB 연결."""
        self._conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        """DB 연결 종료."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        """활성 연결 반환. 없으면 자동 연결."""
        if self._conn is None:
            self.connect()
        return self._conn

    def create_tables(self) -> None:
        """테이블 생성 (없으면)."""
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    entry_date TEXT NOT NULL,
                    entry_price INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    stop_price INTEGER NOT NULL,
                    target_price INTEGER DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'closed', 'selling')),
                    high_since_entry INTEGER DEFAULT 0,
                    hold_days INTEGER DEFAULT 0,
                    partial_sold INTEGER DEFAULT 0,
                    entry_strategy TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    initial_quantity INTEGER DEFAULT 0,
                    tp2_price INTEGER DEFAULT 0,
                    partial_sold_2 INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    price INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    fee REAL DEFAULT 0,
                    tax REAL DEFAULT 0,
                    pnl REAL DEFAULT 0,
                    pnl_pct REAL DEFAULT 0,
                    reason TEXT DEFAULT '',
                    executed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_performance (
                    date TEXT PRIMARY KEY,
                    realized_pnl REAL,
                    unrealized_pnl REAL,
                    total_capital REAL,
                    daily_return REAL,
                    mdd_current REAL,
                    trade_count INTEGER
                );

                CREATE TABLE IF NOT EXISTS ohlcv_cache (
                    code TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open INTEGER,
                    high INTEGER,
                    low INTEGER,
                    close INTEGER,
                    volume INTEGER,
                    amount INTEGER,
                    PRIMARY KEY (code, date)
                );

                -- 성능 인덱스: positions.status (get_open_positions 최적화)
                CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON positions(status);

                -- 성능 인덱스: positions.code (종목별 조회 최적화)
                CREATE INDEX IF NOT EXISTS idx_positions_code
                    ON positions(code);

                -- 성능 인덱스: trades(code, executed_at) (get_last_trade, get_trades_by_date 최적화)
                CREATE INDEX IF NOT EXISTS idx_trades_code_executed
                    ON trades(code, executed_at);

                -- 성능 인덱스: daily_performance(date)
                CREATE INDEX IF NOT EXISTS idx_daily_performance_date
                    ON daily_performance(date);

                -- 성능 인덱스: trades(executed_at) (get_trades_by_date 최적화)
                CREATE INDEX IF NOT EXISTS idx_trades_executed_at
                    ON trades(executed_at);

                CREATE TABLE IF NOT EXISTS daily_watchlist (
                    date TEXT PRIMARY KEY,
                    stocks TEXT NOT NULL,
                    source TEXT DEFAULT 'condition_search',
                    stock_count INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_daily_watchlist_date
                    ON daily_watchlist(date);

                -- 스키마 버전 관리
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()
            self._run_migrations()

    def _run_migrations(self) -> None:
        """스키마 마이그레이션 실행.

        현재 버전을 확인하고, 미적용된 마이그레이션을 순차 실행.
        """
        current = self._get_schema_version()

        if current < 1:
            # v1: partial_sold 컬럼 추가 (기존 DB 호환)
            try:
                self.conn.execute(
                    "ALTER TABLE positions ADD COLUMN partial_sold INTEGER DEFAULT 0"
                )
                logger.info("마이그레이션 v1: positions.partial_sold 컬럼 추가")
            except sqlite3.OperationalError:
                pass  # 이미 존재
            self._set_schema_version(1)

        if current < 2:
            # v2: entry_strategy 컬럼 추가 (진입 전략 추적)
            try:
                self.conn.execute(
                    "ALTER TABLE positions ADD COLUMN entry_strategy TEXT NOT NULL DEFAULT ''"
                )
                logger.info("마이그레이션 v2: positions.entry_strategy 컬럼 추가")
            except sqlite3.OperationalError:
                pass  # 이미 존재
            self._set_schema_version(2)

        if current < 3:
            # v3: daily_watchlist 컬럼 변경 (codes → stocks, JSON dict 배열)
            try:
                self.conn.execute("DROP TABLE IF EXISTS daily_watchlist")
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_watchlist (
                        date TEXT PRIMARY KEY,
                        stocks TEXT NOT NULL,
                        source TEXT DEFAULT 'condition_search',
                        stock_count INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_daily_watchlist_date
                    ON daily_watchlist(date)
                """)
                logger.info("마이그레이션 v3: daily_watchlist 테이블 재생성 (codes→stocks)")
            except sqlite3.OperationalError:
                pass
            self._set_schema_version(3)

        if current < 4:
            # v4: positions에 v2.5 TP2 컬럼 추가 (initial_quantity, tp2_price, partial_sold_2)
            for col, ddl in (
                ("initial_quantity",
                 "ALTER TABLE positions ADD COLUMN initial_quantity INTEGER DEFAULT 0"),
                ("tp2_price",
                 "ALTER TABLE positions ADD COLUMN tp2_price INTEGER DEFAULT 0"),
                ("partial_sold_2",
                 "ALTER TABLE positions ADD COLUMN partial_sold_2 INTEGER DEFAULT 0"),
            ):
                try:
                    self.conn.execute(ddl)
                    logger.info(f"마이그레이션 v4: positions.{col} 컬럼 추가")
                except sqlite3.OperationalError:
                    pass  # 이미 존재
            # 기존 open 포지션 — initial_quantity가 0이면 현재 quantity로 백필
            try:
                self.conn.execute(
                    "UPDATE positions SET initial_quantity = quantity "
                    "WHERE status = 'open' AND initial_quantity = 0"
                )
            except sqlite3.OperationalError:
                pass
            self.conn.commit()
            self._set_schema_version(4)

    def _get_schema_version(self) -> int:
        """현재 스키마 버전 조회."""
        try:
            cursor = self.conn.execute(
                "SELECT MAX(version) FROM schema_version"
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            return 0

    def _set_schema_version(self, version: int) -> None:
        """스키마 버전 기록."""
        from datetime import datetime
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now().isoformat()),
        )
        self.conn.commit()
        logger.info(f"스키마 버전 → v{version}")

    # ── Positions ──────────────────────────────────────────────

    def insert_position(self, pos: Position) -> int:
        """포지션 삽입.

        Args:
            pos: Position 데이터클래스 인스턴스.

        Returns:
            삽입된 row의 id.
        """
        with self._lock:
            initial_qty = pos.initial_quantity if pos.initial_quantity > 0 else pos.quantity
            cursor = self.conn.execute(
                """
                INSERT INTO positions
                    (code, name, entry_date, entry_price, quantity,
                     stop_price, target_price, status, high_since_entry,
                     partial_sold, entry_strategy, updated_at,
                     initial_quantity, tp2_price, partial_sold_2)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.code,
                    pos.name,
                    pos.entry_date,
                    pos.entry_price,
                    pos.quantity,
                    pos.stop_price,
                    pos.target_price,
                    pos.status,
                    pos.high_since_entry,
                    0,
                    pos.entry_strategy,
                    pos.updated_at,
                    initial_qty,
                    pos.tp2_price,
                    0,
                ),
            )
            self.conn.commit()
            return cursor.lastrowid

    def update_position(self, position_id: int, **kwargs) -> None:
        """포지션 업데이트.

        Args:
            position_id: 대상 포지션 ID.
            **kwargs: 업데이트할 컬럼=값 쌍.

        Raises:
            ValueError: 허용되지 않은 컬럼명이 포함된 경우.
        """
        if not kwargs:
            return

        # 보안: 컬럼 화이트리스트 검증 (SQL injection 방지)
        invalid_cols = set(kwargs.keys()) - _POSITION_UPDATABLE_COLUMNS
        if invalid_cols:
            raise ValueError(
                f"허용되지 않은 컬럼: {invalid_cols}. "
                f"허용 목록: {_POSITION_UPDATABLE_COLUMNS}"
            )

        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [position_id]
        with self._lock:
            self.conn.execute(
                f"UPDATE positions SET {set_clause} WHERE id = ?",
                values,
            )
            self.conn.commit()

    def get_open_positions(self) -> list[dict]:
        """열린 포지션 목록 조회.

        Returns:
            dict 리스트 (각 row).
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM positions WHERE status = ?", ("open",)
            )
            return [dict(row) for row in cursor.fetchall()]

    def count_open_positions(self) -> int:
        """열린 포지션 수 조회."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status = ?", ("open",)
            )
            return cursor.fetchone()[0]

    def get_positions_by_status(self, status: str) -> list[dict]:
        """특정 상태의 포지션 목록 조회.

        Args:
            status: 포지션 상태 ("open", "closed", "selling").

        Returns:
            dict 리스트 (각 row).
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM positions WHERE status = ?", (status,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_positions_by_code_and_status(
        self, code: str, status: str
    ) -> list[dict]:
        """특정 종목+상태의 포지션 목록 조회.

        Args:
            code: 종목코드.
            status: 포지션 상태.

        Returns:
            dict 리스트.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM positions WHERE code = ? AND status = ?",
                (code, status),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ── Trades ─────────────────────────────────────────────────

    def record_trade(self, trade: TradeRecord) -> int:
        """매매 기록 삽입.

        Args:
            trade: TradeRecord 인스턴스.

        Returns:
            삽입된 row의 id.
        """
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO trades
                    (code, name, side, price, quantity, amount,
                     fee, tax, pnl, pnl_pct, reason, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.code,
                    trade.name,
                    trade.side,
                    trade.price,
                    trade.quantity,
                    trade.amount,
                    trade.fee,
                    trade.tax,
                    trade.pnl,
                    trade.pnl_pct,
                    trade.reason,
                    trade.executed_at,
                ),
            )
            self.conn.commit()
            return cursor.lastrowid

    def get_last_trade(self, code: str) -> dict | None:
        """특정 종목의 마지막 매도 기록 조회 (재진입 쿨다운용).

        Args:
            code: 종목 코드.

        Returns:
            매매 기록 dict 또는 None.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM trades WHERE code = ? AND side = 'sell' ORDER BY id DESC LIMIT 1",
                (code,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_trades_by_date(self, target_date: str) -> list[dict]:
        """특정 날짜의 매매 기록 조회.

        Args:
            target_date: "YYYY-MM-DD" 형식 날짜.

        Returns:
            매매 기록 dict 리스트.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM trades WHERE executed_at >= ? AND executed_at < ?",
                (f"{target_date} 00:00:00", f"{target_date} 23:59:59"),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_trade_statistics(self, limit: int = 50) -> dict | None:
        """최근 N건 매도 거래의 승률/평균손익 통계.

        Args:
            limit: 최근 N건 (매도 거래만).

        Returns:
            {"count": int, "win_rate": float, "avg_win": float, "avg_loss": float}
            거래 없으면 None.
        """
        try:
            with self._lock:
                cursor = self.conn.execute(
                    "SELECT pnl_pct FROM trades WHERE side = 'sell' "
                    "ORDER BY executed_at DESC LIMIT ?",
                    (limit,),
                )
                rows = cursor.fetchall()
            if not rows:
                return None

            pnls = [r[0] for r in rows]
            wins = [p for p in pnls if p > 0]
            losses = [abs(p) for p in pnls if p <= 0]

            return {
                "count": len(pnls),
                "win_rate": len(wins) / len(pnls) if pnls else 0.5,
                "avg_win": sum(wins) / len(wins) if wins else 0.08,
                "avg_loss": sum(losses) / len(losses) if losses else 0.04,
            }
        except Exception:
            return None

    # ── Daily Performance ──────────────────────────────────────

    def save_daily_performance(
        self,
        date: str,
        realized_pnl: float,
        unrealized_pnl: float,
        total_capital: float,
        daily_return: float,
        mdd_current: float,
        trade_count: int,
    ) -> None:
        """일일 성과 저장 (upsert).

        Args:
            date: "YYYY-MM-DD" 형식.
            realized_pnl: 실현 손익.
            unrealized_pnl: 미실현 손익.
            total_capital: 총 자산.
            daily_return: 일일 수익률.
            mdd_current: 현재 MDD.
            trade_count: 매매 횟수.
        """
        with self._lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO daily_performance
                    (date, realized_pnl, unrealized_pnl, total_capital,
                     daily_return, mdd_current, trade_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    date,
                    realized_pnl,
                    unrealized_pnl,
                    total_capital,
                    daily_return,
                    mdd_current,
                    trade_count,
                ),
            )
            self.conn.commit()

    def get_daily_performance(self, date: str) -> dict | None:
        """일일 성과 조회.

        Args:
            date: "YYYY-MM-DD" 형식.

        Returns:
            성과 dict 또는 None.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM daily_performance WHERE date = ?", (date,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    # ── Daily Watchlist ────────────────────────────────────────

    def save_daily_watchlist(
        self, date: str, stocks: list[dict],
        source: str = "condition_search",
    ) -> None:
        """날짜별 watchlist 저장 (종목 코드+이름). 같은 날짜면 덮어쓰기.

        Args:
            date: 대상 날짜 (YYYY-MM-DD)
            stocks: [{"code": "005930", "name": "삼성전자"}, ...]
            source: 소스 태그 (기본 'condition_search')
        """
        import json
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO daily_watchlist "
                    "(date, stocks, source, stock_count) VALUES (?, ?, ?, ?)",
                    (date, json.dumps(stocks, ensure_ascii=False), source, len(stocks)),
                )
                self.conn.commit()
                logger.info(f"daily_watchlist 저장: {date} ({len(stocks)}종목, source={source})")
            except Exception as e:
                logger.error(f"daily_watchlist 저장 실패 ({date}): {e}")
                raise

    def load_daily_watchlist(self, date: str) -> list[dict] | None:
        """특정 날짜 watchlist 로드.

        Args:
            date: 대상 날짜 (YYYY-MM-DD)

        Returns:
            [{"code": "005930", "name": "삼성전자"}, ...] 또는 None.
        """
        import json
        with self._lock:
            try:
                cursor = self.conn.execute(
                    "SELECT stocks FROM daily_watchlist WHERE date = ?",
                    (date,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    stocks = json.loads(row[0])
                    logger.info(f"daily_watchlist 로드: {date} ({len(stocks)}종목)")
                    return stocks
                return None
            except Exception as e:
                logger.error(f"daily_watchlist 로드 실패 ({date}): {e}")
                return None

    # ── OHLCV Cache ────────────────────────────────────────────

    def cache_ohlcv(self, code: str, records: list[dict]) -> None:
        """OHLCV 데이터 캐시 저장.

        Args:
            code: 종목 코드.
            records: [{"date": ..., "open": ..., ...}] 형태 딕셔너리 리스트.
        """
        with self._lock:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO ohlcv_cache
                    (code, date, open, high, low, close, volume, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        code,
                        r["date"],
                        r.get("open"),
                        r.get("high"),
                        r.get("low"),
                        r.get("close"),
                        r.get("volume"),
                        r.get("amount"),
                    )
                    for r in records
                ],
            )
            self.conn.commit()

    def get_cached_ohlcv(
        self, code: str, start_date: str, end_date: str
    ) -> list[dict]:
        """캐시된 OHLCV 데이터 조회.

        Args:
            code: 종목 코드.
            start_date: 시작일 "YYYY-MM-DD".
            end_date: 종료일 "YYYY-MM-DD".

        Returns:
            OHLCV dict 리스트 (날짜순 정렬).
        """
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT * FROM ohlcv_cache
                WHERE code = ? AND date >= ? AND date <= ?
                ORDER BY date
                """,
                (code, start_date, end_date),
            )
            return [dict(row) for row in cursor.fetchall()]

    def cleanup_ohlcv_cache(self, retention_days: int = 400) -> int:
        """오래된 OHLCV 캐시 데이터 삭제.

        Args:
            retention_days: 보존할 일수 (기본 400일).

        Returns:
            삭제된 행 수.
        """
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM ohlcv_cache WHERE date < ?", (cutoff,)
            )
            deleted = cursor.rowcount
            self.conn.commit()
            if deleted > 0:
                logger.info(f"OHLCV 캐시 정리: {deleted}행 삭제 (기준: {cutoff} 이전)")
            return deleted

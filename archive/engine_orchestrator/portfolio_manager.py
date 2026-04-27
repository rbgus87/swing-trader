"""Layer 4: 포트폴리오 매니저 (Portfolio Manager).

보유 종목 관리 + 청산 조건 체크 + 진입 신호 배분.
Phase 2 portfolio_backtester.py의 로직을 라이브용으로 구현.
"""
from datetime import datetime
from dataclasses import dataclass
import pandas as pd
from loguru import logger

from src.data_pipeline.db import get_combined_db, get_trade_db
from src.strategy.trend_following_v2 import StrategyParams, calculate_indicators


MAX_POSITIONS = 4
MIN_POSITION_AMOUNT = 300_000
MCAP_THRESHOLD = 3_000_000_000_000
TRADING_VALUE_THRESHOLD = 5_000_000_000
EXCLUDED_TYPES = ('SPAC', 'REIT', 'FOREIGN', 'PREFERRED')


@dataclass
class SignalResult:
    ticker: str
    name: str
    signal_type: str
    strategy: str
    price: float
    shares: int
    reason: str
    atr: float = 0.0
    stop_price: float = 0.0
    tp1_price: float = 0.0


class PortfolioManager:
    def __init__(
        self,
        initial_capital: float = 5_000_000,
        max_positions: int = MAX_POSITIONS,
        params: StrategyParams = None,
    ):
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.params = params or StrategyParams()

    def get_open_positions(self) -> list:
        with get_trade_db() as conn:
            self._ensure_v23_positions(conn)
            cursor = conn.execute(
                "SELECT * FROM v23_positions WHERE status = 'OPEN'"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_cash(self) -> float:
        """초기 자본 + 누적 청산 PnL - 현재 보유 매입액."""
        with get_trade_db() as conn:
            self._ensure_v23_positions(conn)
            cursor = conn.execute(
                "SELECT COALESCE(SUM(shares * entry_price), 0) as invested "
                "FROM v23_positions WHERE status = 'OPEN'"
            )
            invested = cursor.fetchone()['invested']
            cursor = conn.execute(
                "SELECT COALESCE(SUM(pnl_amount), 0) as total_pnl "
                "FROM v23_positions WHERE status = 'CLOSED'"
            )
            total_pnl = cursor.fetchone()['total_pnl']
        return self.initial_capital + total_pnl - invested

    @staticmethod
    def _ensure_v23_positions(conn):
        """Orchestrator 스키마 v23_positions 테이블 보장 (DataStore.positions와 분리)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS v23_positions (
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
            )
        """)

    def check_exits(self, date_str: str) -> list:
        """보유 종목 청산 조건 체크 (EOD)."""
        positions = self.get_open_positions()
        if not positions:
            return []

        exit_signals = []

        with get_combined_db() as conn:
            for pos in positions:
                ticker = pos['ticker']
                cursor = conn.execute(
                    "SELECT open, high, low, close FROM daily_candles "
                    "WHERE ticker = ? AND date = ?",
                    (ticker, date_str),
                )
                row = cursor.fetchone()
                if not row:
                    continue

                day_close = row['close']
                day_high = row['high']
                day_low = row['low']

                cursor = conn.execute(
                    "SELECT COUNT(*) as cnt FROM daily_candles "
                    "WHERE ticker = ? AND date > ? AND date <= ?",
                    (ticker, pos['entry_date'], date_str),
                )
                hold_days = cursor.fetchone()['cnt']

                new_highest = max(pos['highest_since_entry'], day_high)
                if new_highest > pos['highest_since_entry']:
                    conn.execute(
                        "UPDATE trade.v23_positions SET highest_since_entry = ? WHERE id = ?",
                        (new_highest, pos['id']),
                    )

                trailing_stop = (
                    new_highest - pos['atr_at_entry'] * self.params.trailing_atr
                )

                exit_reason = None
                exit_price = None

                if day_low <= pos['stop_price']:
                    exit_reason = 'STOP_LOSS'
                    exit_price = pos['stop_price']
                elif not pos['tp1_triggered'] and day_high >= pos['tp1_price']:
                    partial_shares = int(pos['shares'] * self.params.tp1_sell_ratio)
                    if partial_shares > 0:
                        exit_signals.append(SignalResult(
                            ticker=ticker,
                            name=self._get_name(ticker),
                            signal_type='EXIT',
                            strategy=pos['strategy'],
                            price=pos['tp1_price'],
                            shares=partial_shares,
                            reason='TAKE_PROFIT_1',
                        ))
                        conn.execute(
                            "UPDATE trade.v23_positions SET tp1_triggered = 1, "
                            "shares = shares - ? WHERE id = ?",
                            (partial_shares, pos['id']),
                        )
                    continue
                elif day_low <= trailing_stop:
                    exit_reason = 'TRAILING'
                    exit_price = trailing_stop
                else:
                    cursor = conn.execute(
                        "SELECT close FROM daily_candles "
                        "WHERE ticker = ? AND date <= ? "
                        "ORDER BY date DESC LIMIT 25",
                        (ticker, date_str),
                    )
                    recent = cursor.fetchall()
                    if len(recent) >= 21:
                        closes = [r['close'] for r in reversed(recent)]
                        ma5 = sum(closes[-5:]) / 5
                        ma20 = sum(closes[-20:]) / 20
                        prev_ma5 = sum(closes[-6:-1]) / 5
                        prev_ma20 = sum(closes[-21:-1]) / 20
                        if prev_ma5 >= prev_ma20 and ma5 < ma20:
                            exit_reason = 'TREND_EXIT'
                            exit_price = day_close

                if exit_reason is None and hold_days >= self.params.max_hold_days:
                    exit_reason = 'TIME_EXIT'
                    exit_price = day_close

                if exit_reason:
                    exit_signals.append(SignalResult(
                        ticker=ticker,
                        name=self._get_name(ticker),
                        signal_type='EXIT',
                        strategy=pos['strategy'],
                        price=exit_price,
                        shares=pos['shares'],
                        reason=exit_reason,
                    ))

        return exit_signals

    def scan_entries(self, date_str: str, active_strategies: list) -> list:
        """TF v2.3 상태 기반 추세추종 스캔."""
        if 'TF' not in active_strategies:
            return []

        open_positions = self.get_open_positions()
        open_slots = self.max_positions - len(open_positions)
        if open_slots <= 0:
            return []

        cash = self.get_cash()
        alloc = cash * (1.0 / self.max_positions)
        if alloc < MIN_POSITION_AMOUNT:
            logger.warning(
                f"Insufficient capital: alloc={alloc:,.0f} < {MIN_POSITION_AMOUNT:,.0f}"
            )
            return []

        held_tickers = {p['ticker'] for p in open_positions}
        candidates = []
        rs_period = self.params.relative_strength_period

        with get_combined_db() as conn:
            # KOSPI 20일 수익률 (상대강도용)
            cursor = conn.execute(
                "SELECT date, close FROM index_daily "
                "WHERE index_code = 'KOSPI' AND date <= ? "
                "ORDER BY date DESC LIMIT ?",
                (date_str, rs_period + 5),
            )
            kospi_rows = cursor.fetchall()
            kospi_ret_n = None
            if len(kospi_rows) >= rs_period + 1:
                kospi_ret_n = (
                    kospi_rows[0]['close'] / kospi_rows[rs_period]['close']
                ) - 1.0
                logger.info(
                    f"KOSPI {rs_period}d return: {kospi_ret_n:+.2%}"
                )
            else:
                logger.warning(
                    f"index_daily(KOSPI) insufficient "
                    f"(got {len(kospi_rows)} rows) — relative strength disabled"
                )

            cursor = conn.execute(
                """
                SELECT DISTINCT m.ticker
                FROM market_cap_history m
                JOIN stocks s ON m.ticker = s.ticker
                WHERE m.date = ?
                  AND m.market_cap >= ?
                  AND s.stock_type NOT IN (?, ?, ?, ?)
                  AND (s.delisted_date IS NULL OR s.delisted_date > ?)
                """,
                (date_str, MCAP_THRESHOLD, *EXCLUDED_TYPES, date_str),
            )
            universe = [row['ticker'] for row in cursor.fetchall()]

            for ticker in universe:
                if ticker in held_tickers:
                    continue

                cursor = conn.execute(
                    "SELECT date, open, high, low, close, volume "
                    "FROM daily_candles WHERE ticker = ? AND date <= ? "
                    "ORDER BY date DESC LIMIT 150",
                    (ticker, date_str),
                )
                rows = cursor.fetchall()
                if len(rows) < self.params.ma_long + 5:
                    continue

                df = pd.DataFrame([dict(r) for r in reversed(rows)])
                df['date'] = pd.to_datetime(df['date'])
                df = calculate_indicators(df, self.params)
                if df.empty:
                    continue

                today = df.iloc[-1]

                req = ['ma20', 'ma60', 'ma120', 'ma60_slope', 'ma60_dist',
                       'atr', 'adx', 'macd_hist', 'avg_volume_5',
                       'avg_volume_20', 'avg_trading_value_20', 'stock_ret_n']
                if any(pd.isna(today.get(k)) for k in req):
                    continue
                if today['atr'] <= 0 or today['close'] <= 0:
                    continue

                # 완전 정배열 close > MA20 > MA60 > MA120
                if not (today['close'] > today['ma20']
                        > today['ma60'] > today['ma120']):
                    continue
                # MA60 기울기 (+)
                if today['ma60_slope'] <= 0:
                    continue
                # MA60 대비 위치 +5~20%
                if not (self.params.ma60_position_min
                        <= today['ma60_dist']
                        <= self.params.ma60_position_max):
                    continue
                # MACD histogram > 0
                if today['macd_hist'] <= 0:
                    continue
                # 거래량: 5일 평균 > 20일 평균
                if today['avg_volume_5'] <= today['avg_volume_20']:
                    continue
                # ADX / 거래대금
                if today['adx'] < self.params.adx_threshold:
                    continue
                if today['avg_trading_value_20'] < self.params.min_trading_value:
                    continue
                # ATR 밴드
                atr_ratio = today['atr'] / today['close']
                if not (self.params.atr_price_min <= atr_ratio
                        <= self.params.atr_price_max):
                    continue
                # 상대강도 (KOSPI 대비)
                if kospi_ret_n is not None:
                    rs = today['stock_ret_n'] - kospi_ret_n
                    if rs < self.params.relative_strength_threshold:
                        continue

                shares = int(alloc / today['close'])
                if shares <= 0:
                    continue
                candidates.append(SignalResult(
                    ticker=ticker,
                    name=self._get_name(ticker),
                    signal_type='ENTRY',
                    strategy='TF',
                    price=float(today['close']),
                    shares=shares,
                    reason=(f"trend state: MA aligned, "
                            f"ADX={today['adx']:.1f}, "
                            f"MA60_dist={today['ma60_dist']:+.1%}, "
                            f"MACD_hist={today['macd_hist']:+.3f}"),
                    atr=float(today['atr']),
                    stop_price=float(today['close']
                                     - today['atr'] * self.params.stop_loss_atr),
                    tp1_price=float(today['close']
                                    + today['atr'] * self.params.take_profit_atr),
                ))

        def _score(sig):
            try:
                return float(sig.reason.split('ADX=')[1].split(',')[0])
            except (IndexError, ValueError):
                return 0.0

        candidates.sort(key=_score, reverse=True)
        return candidates[:open_slots]

    def record_signals(self, date_str: str, signals: list):
        with get_trade_db() as conn:
            conn.execute("""
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
            """)
            for sig in signals:
                conn.execute(
                    "INSERT INTO signals "
                    "(date, ticker, signal_type, strategy, price, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (date_str, sig.ticker, sig.signal_type, sig.strategy,
                     sig.price, sig.reason),
                )

    def save_snapshot(self, date_str: str, breadth: float, gate_open: bool):
        positions = self.get_open_positions()
        cash = self.get_cash()
        portfolio_value = cash
        with get_combined_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade.daily_portfolio_snapshot (
                    date DATE PRIMARY KEY,
                    cash REAL NOT NULL,
                    portfolio_value REAL NOT NULL,
                    positions_count INTEGER NOT NULL,
                    breadth REAL,
                    gate_status TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for pos in positions:
                cursor = conn.execute(
                    "SELECT close FROM daily_candles WHERE ticker = ? AND date = ?",
                    (pos['ticker'], date_str),
                )
                row = cursor.fetchone()
                if row:
                    portfolio_value += pos['shares'] * row['close']
            conn.execute(
                "INSERT OR REPLACE INTO trade.daily_portfolio_snapshot "
                "(date, cash, portfolio_value, positions_count, breadth, gate_status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date_str, cash, portfolio_value, len(positions),
                 breadth, 'OPEN' if gate_open else 'CLOSED'),
            )

    def _get_name(self, ticker: str) -> str:
        from src.data_pipeline.db import get_data_db
        with get_data_db() as conn:
            cursor = conn.execute(
                "SELECT name FROM stocks WHERE ticker = ?", (ticker,)
            )
            row = cursor.fetchone()
            return row['name'] if row else ticker

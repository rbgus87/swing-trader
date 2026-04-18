"""Layer 4: 포트폴리오 매니저 (Portfolio Manager).

보유 종목 관리 + 청산 조건 체크 + 진입 신호 배분.
Phase 2 portfolio_backtester.py의 로직을 라이브용으로 구현.
"""
from datetime import datetime
from dataclasses import dataclass
import pandas as pd
from loguru import logger

from src.data_pipeline.db import get_connection
from src.strategy.trend_following_v0 import StrategyParams, calculate_indicators


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
        with get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN'"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_cash(self) -> float:
        """초기 자본 + 누적 청산 PnL - 현재 보유 매입액."""
        with get_connection() as conn:
            cursor = conn.execute(
                "SELECT COALESCE(SUM(shares * entry_price), 0) as invested "
                "FROM positions WHERE status = 'OPEN'"
            )
            invested = cursor.fetchone()['invested']
            cursor = conn.execute(
                "SELECT COALESCE(SUM(pnl_amount), 0) as total_pnl "
                "FROM positions WHERE status = 'CLOSED'"
            )
            total_pnl = cursor.fetchone()['total_pnl']
        return self.initial_capital + total_pnl - invested

    def check_exits(self, date_str: str) -> list:
        """보유 종목 청산 조건 체크 (EOD)."""
        positions = self.get_open_positions()
        if not positions:
            return []

        exit_signals = []

        with get_connection() as conn:
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
                        "UPDATE positions SET highest_since_entry = ? WHERE id = ?",
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
                    partial_shares = pos['shares'] // 2
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
                            "UPDATE positions SET tp1_triggered = 1, shares = shares - ? "
                            "WHERE id = ?",
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
        """TF v1 진입 후보 스캔."""
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

        with get_connection() as conn:
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
                    "ORDER BY date DESC LIMIT 70",
                    (ticker, date_str),
                )
                rows = cursor.fetchall()
                if len(rows) < 65:
                    continue

                df = pd.DataFrame([dict(r) for r in reversed(rows)])
                df['date'] = pd.to_datetime(df['date'])
                df = calculate_indicators(df, self.params)
                if df.empty:
                    continue

                today = df.iloc[-1]
                if (pd.isna(today.get('ma60')) or pd.isna(today.get('adx'))
                        or pd.isna(today.get('atr'))):
                    continue
                if today['atr'] <= 0:
                    continue

                aligned = today['ma5'] > today['ma20'] > today['ma60']
                trending = today['adx'] >= self.params.adx_threshold
                liquid = (
                    today.get('avg_trading_value_20', 0)
                    >= self.params.min_trading_value
                )
                if not (aligned and trending and liquid):
                    continue

                if len(df) < 2:
                    continue
                yesterday = df.iloc[-2]
                if pd.isna(yesterday.get('highest_n')):
                    continue

                breakout = today['close'] > yesterday['highest_n']
                vol_confirm = (
                    today['volume']
                    > today['avg_volume_20'] * self.params.volume_multiplier
                )

                if breakout and vol_confirm:
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
                        reason=f"60d breakout, ADX={today['adx']:.1f}",
                        atr=float(today['atr']),
                        stop_price=float(today['close']
                                         - today['atr'] * self.params.stop_loss_atr),
                        tp1_price=float(today['close']
                                        + today['atr'] * self.params.take_profit_atr),
                    ))

        def _score(sig):
            try:
                return float(sig.reason.split('ADX=')[1])
            except (IndexError, ValueError):
                return 0.0

        candidates.sort(key=_score, reverse=True)
        return candidates[:open_slots]

    def record_signals(self, date_str: str, signals: list):
        with get_connection() as conn:
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
        with get_connection() as conn:
            for pos in positions:
                cursor = conn.execute(
                    "SELECT close FROM daily_candles WHERE ticker = ? AND date = ?",
                    (pos['ticker'], date_str),
                )
                row = cursor.fetchone()
                if row:
                    portfolio_value += pos['shares'] * row['close']
            conn.execute(
                "INSERT OR REPLACE INTO daily_portfolio_snapshot "
                "(date, cash, portfolio_value, positions_count, breadth, gate_status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date_str, cash, portfolio_value, len(positions),
                 breadth, 'OPEN' if gate_open else 'CLOSED'),
            )

    def _get_name(self, ticker: str) -> str:
        with get_connection() as conn:
            cursor = conn.execute(
                "SELECT name FROM stocks WHERE ticker = ?", (ticker,)
            )
            row = cursor.fetchone()
            return row['name'] if row else ticker

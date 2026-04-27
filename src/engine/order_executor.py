"""주문 실행 — paper/live 분기.

paper 모드: positions 테이블에 INSERT/UPDATE만 (실제 주문 없음)
live 모드: 키움 OrderManager (Phase 4에서 상세 구현, 현재 stub → paper fallback)
"""
import os
from datetime import datetime
from loguru import logger

from src.data_pipeline.db import get_combined_db, get_trade_db
from src.engine.portfolio_manager import PortfolioManager, SignalResult


class OrderExecutor:
    def __init__(self):
        self.is_paper = os.getenv('IS_PAPER_TRADING', 'true').lower() == 'true'
        logger.info(f"OrderExecutor: {'PAPER' if self.is_paper else 'LIVE'} mode")

    def execute_entries(self, signals: list) -> list:
        results = []
        for sig in signals:
            if sig.signal_type != 'ENTRY':
                continue
            if self.is_paper:
                results.append(self._paper_entry(sig))
            else:
                results.append(self._live_entry(sig))
        return results

    def execute_exits(self, signals: list) -> list:
        results = []
        for sig in signals:
            if sig.signal_type != 'EXIT':
                continue
            if self.is_paper:
                results.append(self._paper_exit(sig))
            else:
                results.append(self._live_exit(sig))
        return results

    def _paper_entry(self, sig: SignalResult) -> dict:
        """PENDING 상태로 INSERT. 익일 시가 체결 시 OPEN 전환."""
        now = datetime.now().isoformat()
        with get_trade_db() as conn:
            PortfolioManager._ensure_v23_positions(conn)
            conn.execute(
                """
                INSERT INTO v23_positions
                (ticker, strategy, entry_date, entry_price, shares, initial_shares,
                 atr_at_entry, stop_price, tp1_price, highest_since_entry,
                 tp1_triggered, status, created_at)
                VALUES (?, ?, date('now', '+1 day'), ?, ?, ?,
                        ?, ?, ?, ?,
                        0, 'PENDING', ?)
                """,
                (sig.ticker, sig.strategy, sig.price, sig.shares, sig.shares,
                 sig.atr, sig.stop_price, sig.tp1_price, sig.price, now),
            )
            conn.execute(
                """
                UPDATE signals SET executed = 1, executed_at = ?
                WHERE date = date('now') AND ticker = ? AND signal_type = 'ENTRY'
                """,
                (now, sig.ticker),
            )
        logger.info(
            f"[PAPER] ENTRY: {sig.ticker} {sig.name} {sig.shares}주 @ ~{sig.price:,.0f}"
        )
        return {'ticker': sig.ticker, 'action': 'ENTRY', 'mode': 'PAPER',
                'shares': sig.shares}

    def _paper_exit(self, sig: SignalResult) -> dict:
        now = datetime.now().isoformat()
        cost_pct = 0.00015 + 0.00015 + 0.0018 + 0.0005 * 2  # 왕복 0.31%

        with get_trade_db() as conn:
            PortfolioManager._ensure_v23_positions(conn)
            cursor = conn.execute(
                """
                SELECT id, entry_price, shares FROM v23_positions
                WHERE ticker = ? AND status = 'OPEN'
                ORDER BY entry_date ASC LIMIT 1
                """,
                (sig.ticker,),
            )
            pos = cursor.fetchone()
            if not pos:
                logger.warning(f"No open position for {sig.ticker}")
                return {'ticker': sig.ticker, 'action': 'EXIT', 'mode': 'PAPER',
                        'error': 'no position'}

            pnl_pct = (sig.price / pos['entry_price'] - 1) - cost_pct
            pnl_amount = sig.shares * pos['entry_price'] * pnl_pct

            if sig.reason == 'TAKE_PROFIT_1':
                # PortfolioManager.check_exits에서 이미 shares 감소 + tp1_triggered 갱신됨
                # 여기선 signals만 마크
                pass
            else:
                conn.execute(
                    """
                    UPDATE v23_positions
                    SET status = 'CLOSED',
                        exit_date = ?, exit_price = ?,
                        exit_reason = ?, pnl_amount = ?
                    WHERE id = ?
                    """,
                    (datetime.now().strftime('%Y-%m-%d'), sig.price,
                     sig.reason, pnl_amount, pos['id']),
                )
            conn.execute(
                """
                UPDATE signals SET executed = 1, executed_at = ?
                WHERE date = date('now') AND ticker = ? AND signal_type = 'EXIT'
                """,
                (now, sig.ticker),
            )
        logger.info(
            f"[PAPER] EXIT: {sig.ticker} {sig.name} {sig.reason} @ {sig.price:,.0f} "
            f"PnL={pnl_amount:+,.0f}"
        )
        return {'ticker': sig.ticker, 'action': 'EXIT', 'mode': 'PAPER',
                'reason': sig.reason}

    def _live_entry(self, sig: SignalResult) -> dict:
        logger.warning(
            f"[LIVE] ENTRY stub: {sig.ticker} — 실제 주문 미구현, DB만 기록"
        )
        return self._paper_entry(sig)

    def _live_exit(self, sig: SignalResult) -> dict:
        logger.warning(
            f"[LIVE] EXIT stub: {sig.ticker} — 실제 주문 미구현, DB만 기록"
        )
        return self._paper_exit(sig)

    def confirm_pending_entries(self, date_str: str):
        """PENDING 포지션 → OPEN 전환 (당일 시가 기준 재계산)."""
        with get_combined_db() as conn:
            PortfolioManager._ensure_v23_positions(conn)
            cursor = conn.execute(
                "SELECT id, ticker, atr_at_entry FROM trade.v23_positions "
                "WHERE status = 'PENDING'"
            )
            pendings = cursor.fetchall()
            if not pendings:
                return

            from src.strategy.trend_following_v2 import StrategyParams
            params = StrategyParams()

            for p in pendings:
                cursor = conn.execute(
                    "SELECT open FROM daily_candles WHERE ticker = ? AND date = ?",
                    (p['ticker'], date_str),
                )
                row = cursor.fetchone()
                if row and row['open'] > 0:
                    actual_price = row['open']
                    atr = p['atr_at_entry']
                    conn.execute(
                        """
                        UPDATE trade.v23_positions
                        SET status = 'OPEN',
                            entry_date = ?,
                            entry_price = ?,
                            stop_price = ?,
                            tp1_price = ?,
                            highest_since_entry = ?
                        WHERE id = ?
                        """,
                        (date_str, actual_price,
                         actual_price - atr * params.stop_loss_atr,
                         actual_price + atr * params.take_profit_atr,
                         actual_price, p['id']),
                    )
                    logger.info(
                        f"CONFIRMED: {p['ticker']} PENDING→OPEN @ {actual_price:,.0f}"
                    )
                else:
                    logger.warning(
                        f"No open price for {p['ticker']} on {date_str}, keeping PENDING"
                    )

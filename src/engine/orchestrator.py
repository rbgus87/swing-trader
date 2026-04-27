"""메인 오케스트레이터 — 4-레이어 순차 실행.

매일 장 마감 후 1회 실행:
  0. PENDING 포지션 체결 확인
  1. Layer 1: RegimeDetector
  2. Layer 2: StrategyRouter
  3. Layer 4: PortfolioManager (청산 → 진입)
  4. OrderExecutor (paper/live)
  5. Notifier (텔레그램)
  6. 스냅샷 저장
"""
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, Exception):
    pass

from loguru import logger

from src.data_pipeline.db import get_data_db
from src.engine.regime_detector import RegimeDetector
from src.engine.strategy_router import StrategyRouter
from src.engine.portfolio_manager import PortfolioManager
from src.engine.order_executor import OrderExecutor
from src.engine.notifier import Notifier


class Orchestrator:
    def __init__(self):
        self.regime = RegimeDetector()
        self.router = StrategyRouter()
        self.portfolio = PortfolioManager()
        self.executor = OrderExecutor()
        self.notifier = Notifier()

    def run(self, date_str: str = None):
        if date_str is None:
            date_str = self._get_latest_trading_date()

        logger.info("=" * 50)
        logger.info(f"Orchestrator run: {date_str}")
        logger.info("=" * 50)

        # 0. PENDING → OPEN 확인
        self.executor.confirm_pending_entries(date_str)

        # 1. Regime
        regime = self.regime.check(date_str)

        # 2. Router
        active_strategies = self.router.route(regime['gate_open'])

        # 3. 청산 체크
        exit_signals = self.portfolio.check_exits(date_str)
        if exit_signals:
            self.portfolio.record_signals(date_str, exit_signals)
            self.executor.execute_exits(exit_signals)
            logger.info(f"Exit signals executed: {len(exit_signals)}")

        # 4. 진입 스캔
        entry_signals = self.portfolio.scan_entries(date_str, active_strategies)
        if entry_signals:
            self.portfolio.record_signals(date_str, entry_signals)
            self.executor.execute_entries(entry_signals)
            logger.info(f"Entry signals executed: {len(entry_signals)}")

        # 5. 스냅샷
        self.portfolio.save_snapshot(
            date_str, regime['breadth'], regime['gate_open']
        )

        # 6. 리포트
        cash = self.portfolio.get_cash()
        positions = self.portfolio.get_open_positions()
        portfolio_value = cash
        with get_data_db() as conn:
            for pos in positions:
                cursor = conn.execute(
                    "SELECT close FROM daily_candles WHERE ticker = ? AND date = ?",
                    (pos['ticker'], date_str),
                )
                row = cursor.fetchone()
                if row:
                    portfolio_value += pos['shares'] * row['close']

        self.notifier.send_daily_report(
            date_str=date_str,
            breadth=regime['breadth'],
            gate_open=regime['gate_open'],
            exit_signals=exit_signals,
            entry_signals=entry_signals,
            cash=cash,
            portfolio_value=portfolio_value,
        )

        logger.info(
            f"Summary: exits={len(exit_signals)}, entries={len(entry_signals)}, "
            f"positions={len(positions)}, cash={cash:,.0f}, "
            f"portfolio={portfolio_value:,.0f}"
        )

    def _get_latest_trading_date(self) -> str:
        with get_data_db() as conn:
            cursor = conn.execute("SELECT MAX(date) FROM daily_candles")
            return cursor.fetchone()[0]


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Swing Trader Daily Orchestrator')
    parser.add_argument('--date', type=str, default=None,
                        help='처리할 날짜 (YYYY-MM-DD). 미지정 시 최신 거래일')
    args = parser.parse_args()

    orch = Orchestrator()
    orch.run(date_str=args.date)


if __name__ == '__main__':
    main()

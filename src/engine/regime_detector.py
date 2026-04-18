"""Layer 1: 시장 국면 판단 (Regime Detector).

Universe 종목 중 종가 > MA200 비율(breadth)로 gate OPEN/CLOSED 결정.
Phase 2 백테스트의 calc_market_breadth()와 동일 로직을 라이브용으로 구현.
"""
from loguru import logger
from src.data_pipeline.db import get_connection

BREADTH_THRESHOLD = 0.40
MCAP_THRESHOLD = 3_000_000_000_000
EXCLUDED_TYPES = ('SPAC', 'REIT', 'FOREIGN', 'PREFERRED')


class RegimeDetector:
    def __init__(self, threshold: float = BREADTH_THRESHOLD):
        self.threshold = threshold
        self.breadth = None
        self.gate_open = True

    def check(self, date_str: str) -> dict:
        """특정 일자 기준 breadth 계산 + gate 판정."""
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

            if not universe:
                self.breadth = 0.5
                self.gate_open = True
                return {'breadth': 0.5, 'gate_open': True}

            above = 0
            total = 0

            for ticker in universe:
                cursor = conn.execute(
                    """
                    SELECT close FROM daily_candles
                    WHERE ticker = ? AND date <= ?
                    ORDER BY date DESC
                    LIMIT 200
                    """,
                    (ticker, date_str),
                )
                rows = [r['close'] for r in cursor.fetchall()]

                if len(rows) < 200:
                    continue

                current = rows[0]
                ma200 = sum(rows) / len(rows)
                total += 1
                if current > ma200:
                    above += 1

            self.breadth = above / total if total > 0 else 0.5
            self.gate_open = self.breadth >= self.threshold

        logger.info(
            f"Regime: breadth={self.breadth:.1%}, "
            f"gate={'OPEN' if self.gate_open else 'CLOSED'}"
        )
        return {'breadth': self.breadth, 'gate_open': self.gate_open}

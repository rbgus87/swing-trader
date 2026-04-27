"""Layer 2: 전략 라우팅 (Strategy Router).

현재 TF v1 단독이므로 pass-through.
Phase 5에서 MR 등 추가 시 여기서 전략 분기.
"""
from loguru import logger


class StrategyRouter:
    def __init__(self):
        self.active_strategies = ['TF']

    def route(self, gate_open: bool) -> list:
        """gate 상태에 따라 활성 전략 목록 반환."""
        if not gate_open:
            logger.info("Gate CLOSED → 신규 진입 전략 없음 (보유 종목 청산만)")
            return []
        return self.active_strategies

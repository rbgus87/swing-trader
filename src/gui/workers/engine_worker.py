"""Orchestrator(4-레이어 엔진)를 별도 스레드에서 1회 실행하는 QThread 래퍼.

EOD 배치 전략 — 폴링/asyncio 불필요. run()이 orchestrator.run() 1회 호출 후 종료.
실시간 상태/포지션/체결은 MainWindow의 DB 타이머가 직접 조회한다.
"""

from PyQt5.QtCore import QThread
from loguru import logger

from src.gui.workers.signals import EngineSignals


class EngineWorker(QThread):
    """Orchestrator를 별도 스레드에서 1회 실행."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.signals = EngineSignals()
        self.setTerminationEnabled(True)

    def run(self):
        """orchestrator.run() 1회 실행 후 종료."""
        self.signals.started.emit()
        try:
            from src.engine.orchestrator import Orchestrator
            orch = Orchestrator()
            orch.run()
        except Exception as e:
            logger.error(f"Orchestrator 실행 오류: {e}")
            self.signals.error.emit(str(e))
        finally:
            self.signals.stopped.emit()

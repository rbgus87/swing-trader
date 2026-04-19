"""중앙 시그널 정의 — Worker ↔ UI 통신."""

from PyQt5.QtCore import QObject, pyqtSignal


class EngineSignals(QObject):
    """엔진-UI 간 시그널 모음.

    Worker → UI:
        started: orchestrator 실행 시작.
        stopped: orchestrator 실행 완료.
        error:   실행 오류 (str).
    """

    started = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)

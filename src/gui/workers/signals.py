"""중앙 시그널 정의 — Worker ↔ UI 통신."""

from PyQt5.QtCore import QObject, pyqtSignal


class EngineSignals(QObject):
    """엔진-UI 간 시그널.

    Worker → UI:
        started / stopped: 엔진 라이프사이클
        error: 실행 오류
        status_updated: dict — 장중 엔진 상태
        positions_updated: list[dict] — 보유 포지션 (실시간 current_price 포함)
        trades_updated: list[dict] — 당일 체결
        candidates_updated: list[dict] — 당일 후보

    UI → Worker:
        request_stop / request_halt / request_resume: 제어 명령
    """

    started = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)
    status_updated = pyqtSignal(dict)
    positions_updated = pyqtSignal(list)
    trades_updated = pyqtSignal(list)
    candidates_updated = pyqtSignal(list)

    request_stop = pyqtSignal()
    request_halt = pyqtSignal()
    request_resume = pyqtSignal()

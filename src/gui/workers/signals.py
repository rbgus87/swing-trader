"""중앙 시그널 정의 — 모든 Worker ↔ UI 통신은 여기서 정의."""

from PyQt5.QtCore import QObject, pyqtSignal


class EngineSignals(QObject):
    """엔진-UI 간 시그널 모음.

    Worker → UI (상태 전달):
        started: 엔진 시작 완료.
        stopped: 엔진 중지 완료.
        error: 엔진 오류 (str).
        status_updated: 엔진 상태 dict.
        positions_updated: 포지션 list[dict].
        trades_updated: 당일 체결 list[dict].
        candidates_updated: 매수 후보 list[dict].

    UI → Worker (제어 명령):
        request_stop: 엔진 중지 요청.
        request_halt: 매매 중단 요청.
        request_resume: 매매 재개 요청.
    """

    # Worker → UI
    started = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)
    status_updated = pyqtSignal(dict)
    positions_updated = pyqtSignal(list)
    trades_updated = pyqtSignal(list)
    candidates_updated = pyqtSignal(list)

    # UI → Worker
    request_stop = pyqtSignal()
    request_halt = pyqtSignal()
    request_resume = pyqtSignal()

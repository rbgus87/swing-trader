"""일일 실행 Worker — 수집 스크립트를 직접 import + 함수 호출.

subprocess 방식은 Windows에서 별도 콘솔 창, ACCESS_VIOLATION, 한글 깨짐 이슈가 있어
같은 프로세스 내 QThread에서 함수 호출로 대체한다.

단계:
  1/5 신규 상장 감지       detect_new_listings.main()
  2/5 일봉 증분           collect_daily_candles.main(incremental=True)
  3/5 시총 증분           collect_market_cap.main()
  4/5 지수 갱신           collect_index_daily.main() (sys.argv 임시 치환)
  5/5 시그널 생성         Orchestrator().run()

로그: 각 단계는 loguru로 로그 → main_window의 GUI sink가 미니 로그/로그탭에 표시.
Worker는 단계 시작·완료·실패만 신호로 emit.
"""

import sys
from typing import Callable

from PyQt5.QtCore import QThread, pyqtSignal
from loguru import logger


class DailyRunWorker(QThread):
    """수집 스크립트를 직접 호출하는 순차 실행 QThread."""

    log_signal = pyqtSignal(str, str)          # (message, level)
    step_signal = pyqtSignal(int, int, str)    # (current, total, label)
    finished_signal = pyqtSignal(bool, str)    # (success, summary)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False
        self.setTerminationEnabled(True)

    def cancel(self):
        self._cancelled = True

    @property
    def _steps(self) -> list[tuple[str, Callable[[], None], bool]]:
        """(label, run_fn, warn_only) 튜플 목록.

        warn_only=True: 예외 발생 시 WARNING만 남기고 다음 단계 진행.
        """
        return [
            ("1/5 신규 상장 감지", self._step_detect_new_listings, False),
            ("2/5 일봉 증분",     self._step_daily_candles,        True),
            ("3/5 시총 증분",     self._step_market_cap,           True),
            ("4/5 지수 갱신",     self._step_index_daily,          False),
            ("5/5 시그널 생성",   self._step_orchestrator,         False),
        ]

    def run(self):
        steps = self._steps
        total = len(steps)
        for i, (label, fn, warn_only) in enumerate(steps, start=1):
            if self._cancelled:
                self.finished_signal.emit(False, "사용자 취소")
                return

            self.step_signal.emit(i, total, label)
            self.log_signal.emit(f"📦 {label} 시작", "INFO")

            try:
                fn()
            except SystemExit as e:
                code = int(getattr(e, "code", 0) or 0)
                if code != 0:
                    msg = f"{label} SystemExit rc={code}"
                    if warn_only:
                        self.log_signal.emit(f"⚠ {msg}", "WARNING")
                    else:
                        self.log_signal.emit(f"❌ {msg}", "ERROR")
                        self.finished_signal.emit(False, msg)
                        return
            except Exception as e:
                msg = f"{label} 실패: {type(e).__name__}: {e}"
                if warn_only:
                    self.log_signal.emit(f"⚠ {msg}", "WARNING")
                    logger.opt(exception=True).warning(msg)
                else:
                    self.log_signal.emit(f"❌ {msg}", "ERROR")
                    logger.opt(exception=True).error(msg)
                    self.finished_signal.emit(False, msg)
                    return
            else:
                self.log_signal.emit(f"✅ {label} 완료", "INFO")

        self.finished_signal.emit(True, "일일 실행 완료")

    # ── 단계별 실행 ──

    def _step_detect_new_listings(self):
        from src.data_pipeline import detect_new_listings as m
        m.main()

    def _step_daily_candles(self):
        from src.data_pipeline import collect_daily_candles as m
        m.main(force_resume=False, incremental=True)

    def _step_market_cap(self):
        from src.data_pipeline import collect_market_cap as m
        m.main()

    def _step_index_daily(self):
        # argparse 스크립트 — sys.argv 임시 치환
        from src.data_pipeline import collect_index_daily as m
        orig_argv = sys.argv
        sys.argv = [sys.argv[0], "--update-only"]
        try:
            m.main()
        finally:
            sys.argv = orig_argv

    def _step_orchestrator(self):
        from src.engine.orchestrator import Orchestrator
        orch = Orchestrator()
        orch.run()

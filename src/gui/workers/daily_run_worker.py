"""일일 실행 Worker — scripts/daily_run.sh의 Python 단계를 순차 실행.

단계:
  1. 신규 상장 감지
  2. 일봉 증분 수집 (--incremental)
  3. 시총 증분 수집
  4. 지수 일봉 갱신 (--update-only)
  5. Orchestrator 시그널 생성
"""

import subprocess
import sys
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal
from loguru import logger


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class DailyRunWorker(QThread):
    """daily_run.sh 상당 작업을 별도 스레드에서 순차 실행."""

    log_signal = pyqtSignal(str, str)          # (message, level)
    step_signal = pyqtSignal(int, int, str)    # (current, total, label)
    finished_signal = pyqtSignal(bool, str)    # (success, summary)

    STEPS: list[tuple[str, list[str], int]] = [
        ("1/5 신규 상장 감지",
         [sys.executable, "src/data_pipeline/detect_new_listings.py"], 120),
        ("2/5 일봉 증분",
         [sys.executable, "src/data_pipeline/collect_daily_candles.py",
          "--incremental"], 900),
        ("3/5 시총 증분",
         [sys.executable, "src/data_pipeline/collect_market_cap.py"], 600),
        ("4/5 지수 갱신",
         [sys.executable, "-m", "src.data_pipeline.collect_index_daily",
          "--update-only"], 120),
        ("5/5 시그널 생성",
         [sys.executable, "-m", "src.engine.orchestrator"], 300),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self.setTerminationEnabled(True)

    def cancel(self):
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def run(self):
        total = len(self.STEPS)
        for i, (label, cmd, timeout) in enumerate(self.STEPS, start=1):
            if self._cancelled:
                self.finished_signal.emit(False, "사용자 취소")
                return

            self.step_signal.emit(i, total, label)
            self.log_signal.emit(f"📦 {label} 시작", "INFO")

            try:
                rc = self._stream_process(cmd, timeout)
            except subprocess.TimeoutExpired:
                self.log_signal.emit(f"⏱ {label} 타임아웃({timeout}s)", "ERROR")
                self.finished_signal.emit(False, f"{label} 타임아웃")
                return
            except Exception as e:
                self.log_signal.emit(f"❌ {label} 오류: {e}", "ERROR")
                self.finished_signal.emit(False, f"{label} 실패: {e}")
                return

            if rc != 0:
                # 일봉 증분은 일부 종목 FAILED여도 전체 파이프라인은 성공으로 취급
                level = "WARNING" if i == 2 else "ERROR"
                self.log_signal.emit(
                    f"{'⚠' if level=='WARNING' else '❌'} {label} rc={rc}",
                    level,
                )
                if level == "ERROR":
                    self.finished_signal.emit(False, f"{label} exit code {rc}")
                    return
            else:
                self.log_signal.emit(f"✅ {label} 완료", "INFO")

        self.finished_signal.emit(True, "일일 실행 완료")

    def _stream_process(self, cmd: list[str], timeout: int) -> int:
        """subprocess.Popen으로 실행하고 stdout을 라인 단위로 log_signal emit."""
        env_py = dict(**__import__("os").environ)
        env_py.setdefault("PYTHONIOENCODING", "utf-8")
        env_py.setdefault("PYTHONUTF8", "1")

        self._proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env_py,
            bufsize=1,
        )
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                if self._cancelled:
                    self._proc.terminate()
                    break
                line = line.rstrip()
                if not line:
                    continue
                # loguru 포맷(예: [32m2026-04-20 ...)에서 색 코드 제거
                stripped = self._strip_ansi(line)
                level = self._infer_level(stripped)
                # 긴 라인은 말미만
                if len(stripped) > 240:
                    stripped = stripped[:200] + " … " + stripped[-30:]
                self.log_signal.emit(stripped, level)
            self._proc.wait(timeout=timeout)
            return self._proc.returncode
        finally:
            proc = self._proc
            self._proc = None
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    @staticmethod
    def _strip_ansi(s: str) -> str:
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", s)

    @staticmethod
    def _infer_level(line: str) -> str:
        up = line.upper()
        if "ERROR" in up or "FAILED" in up or "EXCEPTION" in up:
            return "ERROR"
        if "WARNING" in up or " WARN " in up:
            return "WARNING"
        return "INFO"

"""GUI 진입점.

Usage:
    python gui.py
    python gui.py --selftest    # 환경/의존성 검증 후 종료 (10초 내)
"""

import atexit
import multiprocessing
import os
import sys


def _force_exit():
    """atexit 핸들러 — 프로세스가 남아있으면 강제 종료."""
    # noinspection PyBroadException,PyUnresolvedReferences
    try:
        os._exit(0)
    except Exception:
        pass


if __name__ == "__main__":
    # PyInstaller exe 호환
    multiprocessing.freeze_support()

    # --selftest 플래그: GUI 진입 전 환경 검증 (exit code 반환)
    if "--selftest" in sys.argv:
        from selftest import run_selftest
        sys.exit(run_selftest())

    atexit.register(_force_exit)

    from src.utils.config import config
    from src.utils.logger import setup_logger

    setup_logger(log_level=config.get_env("LOG_LEVEL", "INFO"))

    from src.gui.main_window import run_gui

    run_gui()

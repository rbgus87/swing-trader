"""메인 진입점 — asyncio 기반.

Usage:
    python main.py              # IS_PAPER_TRADING 환경변수에 따라 자동 선택
    python main.py --mode paper # 강제 모의투자
    python main.py --mode live  # 강제 실거래
"""

import argparse
import asyncio
import atexit
import multiprocessing
import os
import signal
import sys

from loguru import logger

# 전역 참조 — atexit에서 사용
_engine = None


def _force_exit():
    """atexit 핸들러 — 프로세스가 남아있으면 강제 종료."""
    try:
        os._exit(0)
    except Exception:
        pass


async def main():
    """메인 함수."""
    global _engine

    # config 먼저 로드
    from src.utils.config import config

    # 명령줄 인자 — --mode 미지정 시 IS_PAPER_TRADING 환경변수 사용
    parser = argparse.ArgumentParser(description="스윙 자동매매 시스템")
    parser.add_argument("--mode", choices=["paper", "live"], default=None)
    args = parser.parse_args()

    mode = args.mode or config.mode

    from src.utils.logger import setup_logger

    setup_logger(log_level=config.get_env("LOG_LEVEL", "INFO"))

    # live 모드 안전 체크
    if mode == "live":
        if not config.get_env("TELEGRAM_BOT_TOKEN"):
            logger.error("LIVE 모드: TELEGRAM_BOT_TOKEN 필수")
            sys.exit(1)

    from src.trading_engine import TradingEngine

    engine = TradingEngine(mode=mode)
    _engine = engine

    # Windows용 종료 시그널 핸들링
    if sys.platform == "win32":
        # Windows: signal.signal()로 SIGINT(Ctrl+C), SIGBREAK(콘솔 닫기) 처리
        def _win_shutdown(signum, frame):
            logger.info(f"종료 시그널 수신: {signum}")
            engine._running = False

        signal.signal(signal.SIGINT, _win_shutdown)
        try:
            signal.signal(signal.SIGBREAK, _win_shutdown)
        except (AttributeError, ValueError):
            pass
    else:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.stop()))

    try:
        await engine.start()
        # 실행 대기
        while engine._running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("사용자 중단")
    finally:
        await engine.stop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    atexit.register(_force_exit)
    asyncio.run(main())

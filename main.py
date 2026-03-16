"""메인 진입점 — asyncio 기반.

Usage:
    python main.py --mode paper
    python main.py --mode simulate
    python main.py --mode live
"""

import argparse
import asyncio
import signal
import sys

from loguru import logger


async def main():
    """메인 함수."""
    # config 먼저 로드
    from src.utils.config import config

    # 명령줄 인자 — --mode 미지정 시 config.yaml의 trading.mode 사용
    config_mode = config.get("trading.mode", "paper")
    parser = argparse.ArgumentParser(description="스윙 자동매매 시스템")
    parser.add_argument("--mode", choices=["paper", "simulate", "live"], default=config_mode)
    from src.utils.logger import setup_logger

    setup_logger(log_level=config.get_env("LOG_LEVEL", "INFO"))

    # live 모드 안전 체크
    if args.mode == "live":
        if not config.get_env("TELEGRAM_BOT_TOKEN"):
            logger.error("LIVE 모드: TELEGRAM_BOT_TOKEN 필수")
            sys.exit(1)

    from src.engine import TradingEngine

    engine = TradingEngine(mode=args.mode)

    # 종료 시그널 핸들링
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.stop()))
        except NotImplementedError:
            pass  # Windows에서 SIGTERM 미지원

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
    asyncio.run(main())

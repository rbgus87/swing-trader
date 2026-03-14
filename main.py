"""메인 진입점.

Usage:
    python main.py --mode paper
    python main.py --mode live
"""

import argparse
import sys

from loguru import logger


def main():
    """메인 함수."""
    parser = argparse.ArgumentParser(description="스윙 자동매매 시스템")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = parser.parse_args()

    # 로거 초기화
    from src.utils.config import config
    from src.utils.logger import setup_logger

    setup_logger(log_level=config.get_env("LOG_LEVEL", "INFO"))

    # live 모드 안전 체크
    if args.mode == "live":
        telegram_token = config.get_env("TELEGRAM_BOT_TOKEN")
        if not telegram_token:
            logger.error("LIVE 모드: TELEGRAM_BOT_TOKEN 필수")
            sys.exit(1)

    # PyQt5 이벤트루프
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)

    from src.engine import TradingEngine

    engine = TradingEngine(mode=args.mode)
    engine.start()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

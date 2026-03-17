"""로깅 설정.

loguru 기반 콘솔 + 파일 동시 출력.
매매 전용 로그(trades)는 별도 핸들러로 분리.
"""

import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

from src.utils.config import _get_app_dir


# 커스텀 레벨: 매매 실행 전용
TRADE_LEVEL = "TRADE"


def setup_logger(
    log_dir: str = "logs",
    log_level: str = "INFO",
    rotation: str = "00:00",
    retention: str = "30 days",
) -> None:
    """loguru 로거 초기화.

    Args:
        log_dir: 로그 파일 저장 디렉터리.
        log_level: 최소 로그 레벨.
        rotation: 로그 파일 교체 주기.
        retention: 로그 파일 보존 기간.
    """
    # exe 환경: 실행 파일 기준 디렉토리에 로그 생성
    if Path(log_dir).is_absolute():
        log_path = Path(log_dir)
    else:
        log_path = _get_app_dir() / log_dir
    log_path.mkdir(parents=True, exist_ok=True)

    # 기존 핸들러 제거
    logger.remove()

    # 콘솔 핸들러
    logger.add(
        sys.stderr,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # 일반 로그 파일 핸들러
    today = datetime.now().strftime("%Y%m%d")
    logger.add(
        str(log_path / f"trading_{today}.log"),
        level=log_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
    )

    # 매매 전용 로그 핸들러
    try:
        logger.level(TRADE_LEVEL, no=25, color="<yellow>")
    except TypeError:
        # 이미 등록된 레벨이면 무시
        pass

    logger.add(
        str(log_path / f"trades_{today}.log"),
        level=TRADE_LEVEL,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{message}"
        ),
        filter=lambda record: record["level"].name == TRADE_LEVEL,
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
    )

    logger.info("로거 초기화 완료 (레벨: {})", log_level)

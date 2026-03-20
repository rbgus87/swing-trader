"""로깅 설정.

loguru 기반 콘솔 + 파일 동시 출력.
매매 전용 로그(trades)는 별도 핸들러로 분리.

파일 관리 방식:
- 단독 파일: trading.log, trades.log
- 용량 초과 시 롤링: trading.log → trading_1.log → trading_2.log ...
- 최대 5개 백업 유지 (retention=5)
"""

import sys
from pathlib import Path

from loguru import logger

from src.utils.config import _get_app_dir


# 커스텀 레벨: 매매 실행 전용
TRADE_LEVEL = "TRADE"


def setup_logger(
    log_dir: str = "logs",
    log_level: str = "INFO",
    max_size: str = "10 MB",
    backup_count: int = 5,
) -> None:
    """loguru 로거 초기화.

    Args:
        log_dir: 로그 파일 저장 디렉터리.
        log_level: 최소 로그 레벨.
        max_size: 로그 파일 최대 크기 (예: "10 MB", "50 MB").
        backup_count: 백업 파일 최대 개수.
    """
    # exe 환경: 실행 파일 기준 디렉토리에 로그 생성
    if Path(log_dir).is_absolute():
        log_path = Path(log_dir)
    else:
        log_path = _get_app_dir() / log_dir
    log_path.mkdir(parents=True, exist_ok=True)

    # 기존 핸들러 제거
    logger.remove()

    # 콘솔 핸들러 (PyInstaller GUI exe에서는 sys.stderr가 None)
    if sys.stderr is not None:
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
    # 단독 파일 + 용량 초과 시 롤링 (trading.log → trading.log.1 → trading.log.2 ...)
    logger.add(
        str(log_path / "trading.log"),
        level=log_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        rotation=max_size,
        retention=backup_count,
        encoding="utf-8",
    )

    # 커스텀 레벨 등록
    for lvl_name, lvl_no, lvl_color in [
        (TRADE_LEVEL, 25, "<yellow>"),
        ("PROGRESS", 15, "<blue>"),  # DEBUG(10) < PROGRESS(15) < INFO(20)
    ]:
        try:
            logger.level(lvl_name, no=lvl_no, color=lvl_color)
        except TypeError:
            pass  # 이미 등록된 레벨이면 무시

    logger.add(
        str(log_path / "trades.log"),
        level=TRADE_LEVEL,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{message}"
        ),
        filter=lambda record: record["level"].name == TRADE_LEVEL,
        rotation=max_size,
        retention=backup_count,
        encoding="utf-8",
    )

    logger.info("로거 초기화 완료 (레벨: {}, 롤링: {})", log_level, max_size)

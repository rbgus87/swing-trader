"""로깅 설정.

loguru 기반 콘솔 + 파일 동시 출력.
매매 전용 로그(trades)는 별도 핸들러로 분리.
구조화 로그(trading_json.log)는 JSON Lines 포맷으로 추가 기록.

파일 관리 방식:
- 단독 파일: trading.log, trades.log, trading_json.log
- 용량 초과 시 롤링: trading.log → trading_1.log → trading_2.log ...
- 최대 5개 백업 유지 (retention=5)
"""

import json
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from src.utils.config import _get_app_dir


# 커스텀 레벨: 매매 실행 전용
TRADE_LEVEL = "TRADE"


def _json_serializer(record: dict) -> str:
    """loguru format 콜백 → JSON Lines 한 줄.

    loguru는 format 콜백 결과에 format_map()을 적용하므로,
    JSON의 중괄호를 이중화({{ }})하여 이스케이프한다.
    format_map 처리 후 원래 JSON이 복원된다.

    매매 이벤트에 logger.bind(event=..., data={...}) 패턴을 쓰면
    JSON data 필드에 포함된다. 일반 로그는 data 필드 없이 기록된다.
    """
    entry: dict[str, Any] = {
        "timestamp": record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "level": record["level"].name,
        "module": record["name"],
        "function": record["function"],
        "line": record["line"],
        "message": record["message"],
    }
    extra: dict = record["extra"]
    if extra:
        data: dict[str, Any] = {}
        if "event" in extra:
            data["event"] = extra["event"]
        nested = extra.get("data")
        if isinstance(nested, dict):
            data.update(nested)
        elif extra:
            data.update({k: v for k, v in extra.items() if k not in ("event", "_json_only")})
        if data:
            entry["data"] = data
    exc = record["exception"]
    if exc is not None and exc.value is not None:
        entry["exception"] = repr(exc.value)
    json_str = json.dumps(entry, ensure_ascii=False, default=str)
    # loguru format_map이 JSON 중괄호를 format placeholder로 해석하지 않도록 이스케이프
    return json_str.replace("{", "{{").replace("}", "}}") + "\n"


def setup_logger(
    log_dir: str = "logs",
    log_level: str = "INFO",
    max_size: str = "10 MB",
    backup_count: int = 5,
    json_enabled: bool = True,
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
        except (TypeError, ValueError):
            pass  # 이미 등록된 레벨이면 무시 (loguru 0.7: ValueError)

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

    if json_enabled:
        logger.add(
            str(log_path / "trading_json.log"),
            level=log_level,
            format=_json_serializer,
            rotation=max_size,
            retention=backup_count,
            encoding="utf-8",
        )

    logger.info("로거 초기화 완료 (레벨: {}, 롤링: {}, JSON: {})", log_level, max_size, json_enabled)

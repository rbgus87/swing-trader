"""애플리케이션 설정 관리.

config.yaml과 .env를 로드하여 통합 설정을 제공하는 싱글턴 모듈.
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv


class AppConfig:
    """config.yaml + .env 통합 설정 관리 클래스.

    싱글턴 패턴으로, 모듈 레벨 인스턴스(config)를 통해 접근.

    Usage:
        from src.utils.config import config
        mode = config.get("trading.mode")
        max_pos = config.get("risk.max_position_ratio")
    """

    def __init__(self, config_path: str | None = None):
        """설정 초기화.

        Args:
            config_path: config.yaml 경로. None이면 CONFIG_PATH 환경변수
                         또는 기본값(./config.yaml) 사용.
        """
        # .env 로드
        load_dotenv()

        # config.yaml 경로 결정
        if config_path is None:
            config_path = os.getenv("CONFIG_PATH", "./config.yaml")

        self._config_path = Path(config_path)
        self._data: dict = {}

        if self._config_path.exists():
            self._load_yaml()

    def _load_yaml(self) -> None:
        """config.yaml 파일 로드."""
        with open(self._config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}

    def get(self, key: str, default=None):
        """dot notation으로 nested key 접근.

        Args:
            key: 점(.)으로 구분된 키 경로. 예: "risk.max_position_ratio"
            default: 키가 없을 때 반환할 기본값.

        Returns:
            설정값 또는 default.
        """
        keys = key.split(".")
        value = self._data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def get_env(self, key: str, default: str | None = None) -> str | None:
        """환경변수 조회.

        Args:
            key: 환경변수 이름.
            default: 기본값.

        Returns:
            환경변수 값 또는 default.
        """
        return os.getenv(key, default)

    @property
    def data(self) -> dict:
        """전체 설정 딕셔너리 반환."""
        return self._data

    def reload(self, config_path: str | None = None) -> None:
        """설정 파일 다시 로드.

        Args:
            config_path: 새 config.yaml 경로. None이면 기존 경로 유지.
        """
        if config_path:
            self._config_path = Path(config_path)
        if self._config_path.exists():
            self._load_yaml()


# 모듈 레벨 싱글턴 인스턴스
config = AppConfig()

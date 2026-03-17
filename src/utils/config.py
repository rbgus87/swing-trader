"""애플리케이션 설정 관리.

config.yaml과 .env를 로드하여 통합 설정을 제공하는 싱글턴 모듈.
"""

import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv


def _get_app_dir() -> Path:
    """exe 또는 스크립트 기준 디렉토리 반환.

    PyInstaller --onedir: exe가 있는 폴더
    PyInstaller --onefile: 임시 폴더(_MEIPASS)가 아닌 exe 위치
    일반 실행: 프로젝트 루트 (cwd)
    """
    if getattr(sys, "frozen", False):
        # PyInstaller exe
        return Path(sys.executable).parent
    return Path.cwd()


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
        # .env 로드 — exe 기준 디렉토리에서 .env 탐색
        app_dir = _get_app_dir()
        env_path = app_dir / ".env"
        load_dotenv(env_path if env_path.exists() else None)

        # config.yaml 경로 결정
        if config_path is None:
            env_config = os.getenv("CONFIG_PATH")
            if env_config:
                config_path = env_config
            else:
                config_path = str(app_dir / "config.yaml")

        self._config_path = Path(config_path)
        self._data: dict = {}

        if self._config_path.exists():
            self._load_yaml()
            self._validate()

    def _load_yaml(self) -> None:
        """config.yaml 파일 로드."""
        with open(self._config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}

    def _validate(self) -> None:
        """설정값 기본 검증 — 누락 시 기본값 적용, 범위 위반 시 보정."""
        # 필수 숫자 범위 검증 + 기본값
        _DEFAULTS = {
            "trading.max_positions": (1, 20, 3),
            "trading.max_hold_days": (1, 60, 15),
            "trading.reentry_cooldown_days": (0, 30, 3),
            "risk.daily_loss_limit": (-0.20, 0.0, -0.03),
            "risk.max_mdd": (-0.50, 0.0, -0.20),
            "risk.max_position_ratio": (0.01, 1.0, 0.15),
            "risk.stop_atr_multiplier": (0.5, 5.0, 1.5),
            "risk.max_stop_pct": (0.01, 0.30, 0.07),
            "strategy.target_return": (0.01, 0.50, 0.08),
        }
        for key, (min_v, max_v, default) in _DEFAULTS.items():
            val = self.get(key)
            if val is None:
                self._set_nested(key, default)
            elif isinstance(val, (int, float)):
                if val < min_v or val > max_v:
                    self._set_nested(key, default)

        # 스케줄 시간 형식 검증 (HH:MM)
        import re
        for time_key in [
            "schedule.screening_time",
            "schedule.daily_report_time",
            "schedule.reconnect_time",
        ]:
            val = self.get(time_key)
            if val and not re.match(r"^\d{2}:\d{2}$", str(val)):
                self._set_nested(time_key, "08:30" if "screening" in time_key
                                 else "16:00" if "report" in time_key else "08:45")

    def _set_nested(self, key: str, value) -> None:
        """dot notation 키에 값 설정."""
        keys = key.split(".")
        d = self._data
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

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
    def is_paper(self) -> bool:
        """모의투자 여부. IS_PAPER_TRADING 환경변수 기반."""
        val = os.getenv("IS_PAPER_TRADING", "True")
        return val.lower() in ("true", "1", "yes")

    @property
    def mode(self) -> str:
        """실행 모드 ('paper' 또는 'live')."""
        return "paper" if self.is_paper else "live"

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

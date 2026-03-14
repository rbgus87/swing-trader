"""유틸리티 모듈 테스트."""

from datetime import date

import yaml

from src.utils.config import AppConfig
from src.utils.market_calendar import (
    KST,
    is_trading_day,
    get_next_trading_day,
)


class TestAppConfig:
    def test_load_config_from_yaml(self, tmp_config_file):
        """yaml 파일에서 설정을 올바르게 로드하는지 확인."""
        cfg = AppConfig(config_path=tmp_config_file)
        assert cfg.get("trading.mode") == "paper"
        assert cfg.get("trading.max_positions") == 5

    def test_nested_key_access(self, tmp_config_file):
        """dot notation으로 중첩 키에 접근."""
        cfg = AppConfig(config_path=tmp_config_file)
        assert cfg.get("risk.max_position_ratio") == 0.15
        assert cfg.get("backtest.initial_capital") == 10_000_000

    def test_default_value(self, tmp_config_file):
        """존재하지 않는 키에 기본값 반환."""
        cfg = AppConfig(config_path=tmp_config_file)
        assert cfg.get("nonexistent.key") is None
        assert cfg.get("nonexistent.key", "default") == "default"

    def test_missing_config_file(self, tmp_path):
        """존재하지 않는 파일 경로에서 빈 설정으로 초기화."""
        cfg = AppConfig(config_path=str(tmp_path / "missing.yaml"))
        assert cfg.data == {}
        assert cfg.get("any.key") is None

    def test_reload(self, tmp_path, sample_config):
        """설정 파일 재로드."""
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(sample_config, f)

        cfg = AppConfig(config_path=str(config_path))
        assert cfg.get("trading.mode") == "paper"

        # 설정 변경 후 재로드
        sample_config["trading"]["mode"] = "live"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(sample_config, f)

        cfg.reload()
        assert cfg.get("trading.mode") == "live"

    def test_get_env(self, tmp_config_file, monkeypatch):
        """환경변수 조회."""
        monkeypatch.setenv("TEST_VAR", "hello")
        cfg = AppConfig(config_path=tmp_config_file)
        assert cfg.get_env("TEST_VAR") == "hello"
        assert cfg.get_env("MISSING_VAR", "fallback") == "fallback"


class TestMarketCalendar:
    def test_weekday_is_trading_day(self):
        """평일은 거래일 (공휴일이 아닌 경우)."""
        # 2025-01-13은 월요일
        assert is_trading_day(date(2025, 1, 13)) is True

    def test_saturday_is_not_trading_day(self):
        """토요일은 거래일이 아님."""
        # 2025-01-11은 토요일
        assert is_trading_day(date(2025, 1, 11)) is False

    def test_sunday_is_not_trading_day(self):
        """일요일은 거래일이 아님."""
        # 2025-01-12는 일요일
        assert is_trading_day(date(2025, 1, 12)) is False

    def test_korean_holiday(self):
        """한국 공휴일은 거래일이 아님."""
        # 2025-01-01은 신정
        assert is_trading_day(date(2025, 1, 1)) is False
        # 설날 (음력 1월 1일) — 2025년 설날은 1/29
        assert is_trading_day(date(2025, 1, 29)) is False

    def test_get_next_trading_day_from_friday(self):
        """금요일 다음 거래일은 월요일."""
        # 2025-01-10 금요일
        next_day = get_next_trading_day(date(2025, 1, 10))
        assert next_day == date(2025, 1, 13)  # 월요일

    def test_get_next_trading_day_from_weekday(self):
        """평일 다음 거래일은 다음 날."""
        # 2025-01-13 월요일
        next_day = get_next_trading_day(date(2025, 1, 13))
        assert next_day == date(2025, 1, 14)  # 화요일

    def test_kst_timezone(self):
        """KST 타임존 상수 확인."""
        assert str(KST) == "Asia/Seoul"

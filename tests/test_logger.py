"""tests/test_logger.py — JSON 구조화 로그 테스트."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from loguru import logger

from src.utils.logger import TRADE_LEVEL, _json_serializer, setup_logger


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _read_json_lines(path: Path) -> list[dict]:
    """JSON Lines 파일을 읽어 dict 리스트로 반환."""
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# _json_serializer 유닛 테스트 (통합 방식 — logger 경유)
# ---------------------------------------------------------------------------

class TestJsonSerializer:
    """_json_serializer 포맷 검증 (실제 로거를 통해)."""

    def test_json_file_created(self, tmp_path: Path):
        setup_logger(log_dir=str(tmp_path), json_enabled=True, log_level="DEBUG")
        logger.info("json file creation test")
        assert (tmp_path / "trading_json.log").exists()

    def test_json_lines_valid(self, tmp_path: Path):
        setup_logger(log_dir=str(tmp_path), json_enabled=True, log_level="DEBUG")
        logger.info("valid json line test")
        entries = _read_json_lines(tmp_path / "trading_json.log")
        assert entries, "JSON 로그 파일이 비어있음"
        for entry in entries:
            assert "timestamp" in entry
            assert "level" in entry
            assert "module" in entry
            assert "function" in entry
            assert "line" in entry
            assert "message" in entry

    def test_plain_log_no_data_field(self, tmp_path: Path):
        setup_logger(log_dir=str(tmp_path), json_enabled=True, log_level="DEBUG")
        logger.info("plain log without bind")
        entries = _read_json_lines(tmp_path / "trading_json.log")
        # 최신 항목이 data 필드를 갖지 않아야 함
        last = entries[-1]
        assert last.get("data") is None

    def test_buy_event_has_data_event_field(self, tmp_path: Path):
        setup_logger(log_dir=str(tmp_path), json_enabled=True, log_level="DEBUG")
        logger.bind(
            event="BUY",
            data={"code": "005930", "price": 72300, "qty": 10},
        ).log(TRADE_LEVEL, "매수: 삼성전자 10주 @72,300")
        entries = _read_json_lines(tmp_path / "trading_json.log")
        buy = next(
            (e for e in entries if e.get("data", {}).get("event") == "BUY"), None
        )
        assert buy is not None, "BUY 이벤트가 JSON 로그에 없음"
        assert buy["data"]["event"] == "BUY"
        assert buy["data"]["code"] == "005930"
        assert buy["data"]["price"] == 72300

    def test_sell_event_has_data_event_field(self, tmp_path: Path):
        setup_logger(log_dir=str(tmp_path), json_enabled=True, log_level="DEBUG")
        logger.bind(
            event="SELL",
            data={"code": "000660", "pnl": -5000, "hold_days": 3},
        ).log(TRADE_LEVEL, "매도: SK하이닉스 5주 @185,500 (stop_loss)")
        entries = _read_json_lines(tmp_path / "trading_json.log")
        sell = next(
            (e for e in entries if e.get("data", {}).get("event") == "SELL"), None
        )
        assert sell is not None, "SELL 이벤트가 JSON 로그에 없음"
        assert sell["data"]["event"] == "SELL"
        assert sell["data"]["pnl"] == -5000

    def test_json_disabled_no_file(self, tmp_path: Path):
        setup_logger(log_dir=str(tmp_path), json_enabled=False, log_level="DEBUG")
        logger.info("json disabled test")
        assert not (tmp_path / "trading_json.log").exists()

    def test_text_logs_still_created(self, tmp_path: Path):
        """JSON 핸들러 추가 후 기존 텍스트 로그도 정상 생성."""
        setup_logger(log_dir=str(tmp_path), json_enabled=True, log_level="DEBUG")
        logger.info("text log check")
        assert (tmp_path / "trading.log").exists()

    def test_json_with_korean_message(self, tmp_path: Path):
        """한글 메시지가 ensure_ascii=False로 정상 직렬화."""
        setup_logger(log_dir=str(tmp_path), json_enabled=True, log_level="DEBUG")
        logger.bind(
            event="BUY",
            data={"name": "삼성전자", "code": "005930"},
        ).log(TRADE_LEVEL, "매수: 삼성전자")
        entries = _read_json_lines(tmp_path / "trading_json.log")
        buy = next(
            (e for e in entries if e.get("data", {}).get("event") == "BUY"), None
        )
        assert buy is not None
        assert buy["data"]["name"] == "삼성전자"
        assert "삼성전자" in buy["message"]

    def test_json_level_field(self, tmp_path: Path):
        """TRADE 레벨이 JSON level 필드에 기록됨."""
        setup_logger(log_dir=str(tmp_path), json_enabled=True, log_level="DEBUG")
        logger.bind(event="BUY", data={"code": "005930"}).log(TRADE_LEVEL, "trade test")
        entries = _read_json_lines(tmp_path / "trading_json.log")
        trade = next(
            (e for e in entries if e.get("data", {}).get("event") == "BUY"), None
        )
        assert trade is not None
        assert trade["level"] == TRADE_LEVEL

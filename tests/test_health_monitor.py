"""tests/test_health_monitor.py — HealthMonitor 단위 테스트."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.engine.health_monitor import HealthMonitor, HealthStatus


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _make_monitor(tmp_path: Path, **kwargs) -> HealthMonitor:
    hb_file = tmp_path / "heartbeat.json"
    return HealthMonitor(heartbeat_file=hb_file, **kwargs)


# ---------------------------------------------------------------------------
# HealthStatus 기본값
# ---------------------------------------------------------------------------

class TestHealthStatus:
    def test_defaults(self):
        s = HealthStatus()
        assert s.last_heartbeat == 0.0
        assert s.consecutive_poll_failures == 0
        assert s.polling_active is False
        assert s.engine_started is False
        assert s.warnings == []


# ---------------------------------------------------------------------------
# beat / record_tick / record_poll_failure
# ---------------------------------------------------------------------------

class TestBeat:
    def test_beat_updates_heartbeat(self, tmp_path):
        mon = _make_monitor(tmp_path)
        before = time.time()
        mon.beat()
        assert mon.status.last_heartbeat >= before

    def test_beat_resets_poll_failures(self, tmp_path):
        mon = _make_monitor(tmp_path)
        mon.status.consecutive_poll_failures = 3
        mon.beat()
        assert mon.status.consecutive_poll_failures == 0

    def test_beat_writes_heartbeat_file(self, tmp_path):
        mon = _make_monitor(tmp_path)
        mon.beat()
        hb_file = tmp_path / "heartbeat.json"
        assert hb_file.exists()

    def test_record_tick(self, tmp_path):
        mon = _make_monitor(tmp_path)
        before = time.time()
        mon.record_tick()
        assert mon.status.last_tick_time >= before

    def test_record_poll_failure_increments(self, tmp_path):
        mon = _make_monitor(tmp_path)
        mon.record_poll_failure()
        assert mon.status.consecutive_poll_failures == 1
        mon.record_poll_failure()
        assert mon.status.consecutive_poll_failures == 2


# ---------------------------------------------------------------------------
# poll_fail_threshold → 텔레그램 알림
# ---------------------------------------------------------------------------

class TestPollFailAlert:
    def test_alert_sent_at_threshold(self, tmp_path):
        telegram = MagicMock()
        mon = _make_monitor(tmp_path, poll_fail_threshold=3, telegram=telegram)
        for _ in range(3):
            mon.record_poll_failure()
        telegram.send.assert_called_once()
        msg = telegram.send.call_args[0][0]
        assert "폴링 실패" in msg

    def test_alert_not_sent_below_threshold(self, tmp_path):
        telegram = MagicMock()
        mon = _make_monitor(tmp_path, poll_fail_threshold=5, telegram=telegram)
        for _ in range(4):
            mon.record_poll_failure()
        telegram.send.assert_not_called()

    def test_alert_sent_only_once(self, tmp_path):
        telegram = MagicMock()
        mon = _make_monitor(tmp_path, poll_fail_threshold=2, telegram=telegram)
        for _ in range(5):
            mon.record_poll_failure()
        # 임계치 초과 후에도 중복 발송 안 함
        assert telegram.send.call_count == 1


# ---------------------------------------------------------------------------
# check_health — 장외 시간 무시
# ---------------------------------------------------------------------------

class TestCheckHealth:
    def test_outside_market_hours_returns_empty(self, tmp_path):
        mon = _make_monitor(tmp_path)
        # 장외 시간(예: 20시)에는 항상 빈 리스트
        with patch("src.engine.health_monitor.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 20
            result = mon.check_health()
        assert result == []

    def test_inside_market_hours_stale_heartbeat(self, tmp_path):
        mon = _make_monitor(tmp_path, stale_threshold=10)
        mon.status.last_heartbeat = time.time() - 30  # 30초 전
        with patch("src.engine.health_monitor.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            result = mon.check_health()
        assert len(result) == 1
        assert "Heartbeat" in result[0]

    def test_fresh_heartbeat_no_warning(self, tmp_path):
        mon = _make_monitor(tmp_path, stale_threshold=60)
        mon.status.last_heartbeat = time.time()  # 방금 전
        with patch("src.engine.health_monitor.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            result = mon.check_health()
        assert result == []

    def test_tick_stale_warning(self, tmp_path):
        mon = _make_monitor(tmp_path)
        mon.status.polling_active = True
        mon.status.last_tick_time = time.time() - 400  # 400초 전 = 5분 초과
        with patch("src.engine.health_monitor.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 11
            result = mon.check_health()
        assert any("틱" in w for w in result)


# ---------------------------------------------------------------------------
# reset_alerts
# ---------------------------------------------------------------------------

class TestResetAlerts:
    def test_reset_clears_alert_set(self, tmp_path):
        telegram = MagicMock()
        mon = _make_monitor(tmp_path, poll_fail_threshold=1, telegram=telegram)
        mon.record_poll_failure()          # 1회 알림 발송
        assert telegram.send.call_count == 1

        mon.reset_alerts()

        # 리셋 후 같은 키(POLL_FAIL)로 다시 알림 가능
        mon.record_poll_failure()
        assert telegram.send.call_count == 2

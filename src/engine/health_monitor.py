"""헬스체크 모니터 — 엔진 상태 감시 + 이상 알림."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class HealthStatus:
    """엔진 건강 상태."""

    last_heartbeat: float = 0.0          # 마지막 heartbeat timestamp
    last_tick_time: float = 0.0          # 마지막 틱 수신 시간
    last_screening_time: float = 0.0     # 마지막 스크리닝 시간
    consecutive_poll_failures: int = 0   # 연속 폴링 실패 횟수 (사이클 단위)
    polling_active: bool = False         # 폴링 루프 활성 여부
    engine_started: bool = False         # 엔진 시작 여부
    last_daily_update: str = ""          # 마지막 일일 데이터 갱신 날짜
    warnings: list[str] = field(default_factory=list)


class HealthMonitor:
    """엔진 상태를 감시하고 이상 시 텔레그램 알림."""

    def __init__(
        self,
        heartbeat_file: Path | None = None,
        stale_threshold: int = 120,      # 2분 이상 heartbeat 없으면 stale
        poll_fail_threshold: int = 5,    # 연속 N 사이클 실패 시 알림
        telegram=None,
    ) -> None:
        self._heartbeat_file = heartbeat_file or Path("logs/heartbeat.json")
        self._stale_threshold = stale_threshold
        self._poll_fail_threshold = poll_fail_threshold
        self._telegram = telegram
        self.status = HealthStatus()
        self._alerted: set[str] = set()  # 중복 알림 방지 키 세트

    # ── 상태 기록 ──

    def beat(self) -> None:
        """정상 동작 heartbeat 기록. 폴링 사이클마다 한 번 호출."""
        self.status.last_heartbeat = time.time()
        self.status.consecutive_poll_failures = 0
        self._write_heartbeat()

    def record_tick(self) -> None:
        """틱(현재가) 수신 기록."""
        self.status.last_tick_time = time.time()

    def record_poll_failure(self) -> None:
        """폴링 사이클 전체 실패 기록. 임계치 도달 시 알림."""
        self.status.consecutive_poll_failures += 1
        if self.status.consecutive_poll_failures >= self._poll_fail_threshold:
            self._alert(
                "POLL_FAIL",
                f"⚠️ 연속 폴링 실패 {self.status.consecutive_poll_failures}회",
            )

    def record_screening(self) -> None:
        """스크리닝 완료 기록."""
        self.status.last_screening_time = time.time()

    def record_daily_update(self, date_str: str) -> None:
        """일일 데이터 갱신 완료 기록."""
        self.status.last_daily_update = date_str

    # ── 상태 점검 ──

    def check_health(self) -> list[str]:
        """현재 상태 점검 → 경고 목록 반환.

        장중(09:00~15:59)에만 경고 발생. 장외 시간은 빈 리스트 반환.
        """
        warnings: list[str] = []
        now = time.time()

        # 장외 시간 무시 (09:00~15:59만 체크)
        hour = datetime.now().hour
        if not (9 <= hour <= 15):
            return warnings

        # heartbeat 지연 체크
        if self.status.last_heartbeat > 0:
            elapsed = now - self.status.last_heartbeat
            if elapsed > self._stale_threshold:
                msg = (
                    f"⚠️ Heartbeat 지연: {elapsed:.0f}초 "
                    f"(임계: {self._stale_threshold}초)"
                )
                warnings.append(msg)
                self._alert("HEARTBEAT_STALE", msg)

        # 폴링 중인데 틱이 5분 이상 안 들어오는 경우
        if self.status.polling_active and self.status.last_tick_time > 0:
            tick_elapsed = now - self.status.last_tick_time
            if tick_elapsed > 300:
                msg = f"⚠️ 틱 수신 중단: {tick_elapsed:.0f}초 경과"
                warnings.append(msg)
                self._alert("TICK_STALE", msg)

        self.status.warnings = warnings
        return warnings

    def reset_alerts(self) -> None:
        """일일 리셋 — 알림 중복 방지 세트 초기화."""
        self._alerted.clear()

    # ── 내부 ──

    def _alert(self, key: str, message: str) -> None:
        """중복 방지 텔레그램 알림. 동일 key는 reset_alerts() 전까지 1회만 발송."""
        if key not in self._alerted and self._telegram is not None:
            try:
                self._telegram.send(f"🔍 헬스체크\n{message}")
            except Exception:
                pass
            self._alerted.add(key)

    def _write_heartbeat(self) -> None:
        """heartbeat 상태를 JSON 파일로 기록 (외부 watchdog용). 실패 시 묵살."""
        try:
            self._heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "timestamp": self.status.last_heartbeat,
                "polling": self.status.polling_active,
                "engine": self.status.engine_started,
                "last_tick": self.status.last_tick_time,
                "poll_failures": self.status.consecutive_poll_failures,
            }
            self._heartbeat_file.write_text(
                json.dumps(data, default=str), encoding="utf-8"
            )
        except Exception:
            pass  # heartbeat 기록 실패는 엔진 크래시로 이어지면 안 됨

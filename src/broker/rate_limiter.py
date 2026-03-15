"""API 요청 속도 제한기.

키움 API 초당 요청 제한(TR 5건/초, 주문 5건/초)을 준수하기 위한
슬라이딩 윈도우 기반 속도 제한기.
"""

import asyncio
import time
from collections import deque


class RateLimiter:
    """키움 API 초당 요청 제한: TR 5건/초, 주문 5건/초.

    deque 기반 슬라이딩 윈도우로 period 내 호출 횟수를 추적하고,
    초과 시 자동 대기한다.

    Args:
        max_calls: period 내 최대 호출 횟수.
        period: 슬라이딩 윈도우 크기(초).
    """

    def __init__(self, max_calls: int = 5, period: float = 1.0):
        self._max_calls = max_calls
        self._period = period
        self._calls: deque = deque()

    def _purge_old(self) -> None:
        """period 이전 호출 기록 제거."""
        now = time.monotonic()
        while self._calls and (now - self._calls[0]) >= self._period:
            self._calls.popleft()

    def can_call(self) -> bool:
        """대기 없이 즉시 호출 가능 여부 확인.

        Returns:
            호출 가능하면 True.
        """
        self._purge_old()
        return len(self._calls) < self._max_calls

    def wait(self) -> None:
        """호출 가능할 때까지 대기 후 호출 기록 추가.

        period 내 max_calls를 초과하면 가장 오래된 호출이
        윈도우를 벗어날 때까지 sleep한다.
        """
        self._purge_old()

        if len(self._calls) >= self._max_calls:
            sleep_time = self._period - (time.monotonic() - self._calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
            self._purge_old()

        self._calls.append(time.monotonic())


class AsyncRateLimiter:
    """비동기 슬라이딩 윈도우 rate limiter.

    asyncio 기반 비동기 코드에서 사용하는 속도 제한기.
    period 내 max_calls 횟수를 초과하면 비동기 대기한다.

    Args:
        max_calls: period 내 최대 호출 횟수.
        period: 슬라이딩 윈도우 크기(초).
    """

    def __init__(self, max_calls: int = 5, period: float = 1.0):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque = deque()

    def can_call(self) -> bool:
        """대기 없이 즉시 호출 가능 여부 확인."""
        self._purge_old()
        return len(self._calls) < self.max_calls

    async def wait(self):
        """호출 가능할 때까지 비동기 대기 후 호출 기록 추가."""
        now = time.monotonic()
        while self._calls and now - self._calls[0] > self.period:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            sleep_time = self.period - (now - self._calls[0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
        self._calls.append(time.monotonic())

    def _purge_old(self):
        """period 이전 호출 기록 제거."""
        now = time.monotonic()
        while self._calls and now - self._calls[0] > self.period:
            self._calls.popleft()

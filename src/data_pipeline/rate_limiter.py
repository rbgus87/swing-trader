"""Thread-safe rate limiter."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """초당 max_per_second회 호출 제한. 스레드 안전."""

    def __init__(self, max_per_second: int) -> None:
        self.min_interval = 1.0 / max_per_second
        self.last_call_time = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        """다음 호출까지 필요한 만큼 대기."""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_call_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last_call_time = time.time()

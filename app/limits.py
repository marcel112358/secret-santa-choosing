import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window limit per key. Lives in memory, so it only holds within one process."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> deque[float]:
        hits = self._hits.get(key, deque())
        cutoff = time.monotonic() - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def is_limited(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def hit(self, key: str) -> None:
        with self._lock:
            hits = self._recent(key)
            hits.append(time.monotonic())
            self._hits[key] = hits

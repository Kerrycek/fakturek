from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from collections import OrderedDict, deque
from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int
    remaining: int


class SlidingWindowRateLimiter:
    """Thread-safe, memory-bounded in-process sliding-window limiter."""

    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: int,
        max_buckets: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = max(1, int(window_seconds))
        self.max_buckets = max(1, int(max_buckets))
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    @staticmethod
    def _normalize_key(key: str) -> str:
        value = str(key)
        if len(value) <= 256:
            return value
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _prune_bucket(bucket: deque[float], cutoff: float) -> None:
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _sweep_expired(self, *, now: float, cutoff: float) -> None:
        sweep_interval = min(float(self.window_seconds), 60.0)
        if len(self._hits) < self.max_buckets and now - self._last_sweep < sweep_interval:
            return
        expired: list[str] = []
        for key, bucket in self._hits.items():
            self._prune_bucket(bucket, cutoff)
            if not bucket:
                expired.append(key)
        for key in expired:
            self._hits.pop(key, None)
        self._last_sweep = now

    def check(self, key: str) -> RateLimitDecision:
        now = float(self._clock())
        window = float(self.window_seconds)
        cutoff = now - window
        normalized_key = self._normalize_key(key)

        with self._lock:
            self._sweep_expired(now=now, cutoff=cutoff)
            bucket = self._hits.get(normalized_key)
            if bucket is None:
                if len(self._hits) >= self.max_buckets:
                    # Evict the least recently used bucket. Sharing one overflow
                    # bucket would let high-cardinality traffic exhaust the quota
                    # for every new client and turn the limiter into a global DoS.
                    self._hits.popitem(last=False)
                bucket = deque()
                self._hits[normalized_key] = bucket
            else:
                self._hits.move_to_end(normalized_key)
            self._prune_bucket(bucket, cutoff)

            if len(bucket) >= self.max_requests:
                retry_after = (
                    int(ceil(window - (now - bucket[0])))
                    if bucket
                    else self.window_seconds
                )
                return RateLimitDecision(
                    allowed=False,
                    retry_after=max(1, retry_after),
                    remaining=0,
                )

            bucket.append(now)
            return RateLimitDecision(
                allowed=True,
                retry_after=0,
                remaining=max(0, self.max_requests - len(bucket)),
            )

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._hits)

"""Per-service rate limiting and backoff.

Every service caps how often it will answer, and exceeding that cap costs more
than it saves: the 429 responses force a wait far longer than the polling
interval that provoked them. So requests are paced before they are sent, and a
service that pushes back is given room rather than retried immediately.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    """Classic token bucket: a steady rate with a burst allowance.

    The burst matters because a sync pass is naturally bursty -- it lists a
    dozen collections at once, then goes quiet for fifteen minutes. Pacing
    strictly to the average rate would make every pass needlessly slow while
    still leaving the long-run quota almost untouched.
    """

    rate_per_second: float
    burst: float
    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default_factory=time.monotonic, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = self.burst

    def take(self, amount: float = 1.0, timeout: float = 60.0) -> bool:
        """Wait for capacity, then consume it. False if it never came."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.burst, self._tokens + (now - self._last) * self.rate_per_second
                )
                self._last = now
                if self._tokens >= amount:
                    self._tokens -= amount
                    return True
                shortfall = amount - self._tokens
                wait = shortfall / self.rate_per_second if self.rate_per_second else timeout

            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 1.0))


#: Conservative defaults, well inside each service's published quota. Being
#: slower than strictly necessary costs a few seconds per sync; being faster
#: costs a rate-limit ban that stalls syncing for minutes.
_LIMITS: dict[str, tuple[float, float]] = {
    "google": (8.0, 20.0),
    "todoist": (1.0, 10.0),
    "ticktick": (2.0, 8.0),
    "microsoft": (5.0, 15.0),
    "apple": (2.0, 6.0),
    "things3": (1.0, 4.0),
    "radicale": (50.0, 100.0),  # Loopback; the limit only guards against loops.
}

_buckets: dict[str, TokenBucket] = {}
_buckets_lock = threading.Lock()

#: Services currently in a backoff window, mapped to when they may be used again.
_cooldowns: dict[str, float] = {}


def bucket_for(service: str) -> TokenBucket:
    with _buckets_lock:
        if service not in _buckets:
            rate, burst = _LIMITS.get(service, (2.0, 5.0))
            _buckets[service] = TokenBucket(rate_per_second=rate, burst=burst)
        return _buckets[service]


def acquire(service: str, timeout: float = 60.0) -> bool:
    """Wait until it is polite to call this service."""
    remaining = cooldown_remaining(service)
    if remaining > 0:
        return False
    return bucket_for(service).take(timeout=timeout)


def note_rate_limit(service: str, retry_after: float | None) -> float:
    """Record that a service pushed back, and return how long to stay away.

    Falls back to a minute when the service gives no Retry-After header, which
    is long enough to matter and short enough that a single transient limit does
    not skip a whole sync interval.
    """
    delay = retry_after if retry_after and retry_after > 0 else 60.0
    delay = min(delay, 900.0)  # Never sit out longer than fifteen minutes.
    with _buckets_lock:
        _cooldowns[service] = time.monotonic() + delay
    logger.warning("Backing off %s for %.0f seconds", service, delay)
    return delay


def cooldown_remaining(service: str) -> float:
    with _buckets_lock:
        until = _cooldowns.get(service)
    if until is None:
        return 0.0
    remaining = until - time.monotonic()
    return max(0.0, remaining)


def clear_cooldown(service: str) -> None:
    with _buckets_lock:
        _cooldowns.pop(service, None)

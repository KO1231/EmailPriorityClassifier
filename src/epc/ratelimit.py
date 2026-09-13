"""Rate limiting that paces requests instead of stalling in blocks.

The previous implementation ran `concurrency` requests, waited for the slowest,
then slept. One slow response stalled every worker, and throughput sagged well
under the configured limit.

A token bucket shaped as a next-slot clock gives each worker its own turn: they
take slots in order, the interval between slots is the reciprocal of the rate,
and a slow request delays only itself.
"""

import threading
import time


class RateLimiter:
    """Admit at most `per_minute` calls a minute, shared across threads."""

    def __init__(self, per_minute: int, *, burst: int = 1) -> None:
        if per_minute <= 0:
            raise ValueError(f"per_minute must be positive, got {per_minute}")
        self._interval = 60.0 / per_minute
        self._burst = max(1, burst)
        self._lock = threading.Lock()
        self._next_slot = time.monotonic()

    def acquire(self) -> float:
        """Block until a slot is free. Returns how long it waited."""
        with self._lock:
            now = time.monotonic()
            # An idle period earns back up to `burst` slots and no more, so a
            # pause does not turn into a thundering herd afterwards.
            earliest = now - self._interval * (self._burst - 1)
            slot = max(self._next_slot, earliest)
            self._next_slot = slot + self._interval
            wait = max(0.0, slot - now)

        if wait > 0:
            time.sleep(wait)
        return wait

    def __enter__(self) -> RateLimiter:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        return None

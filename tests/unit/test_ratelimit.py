"""Pacing, not block-sleeping."""

import threading
import time

import pytest

from epc.ratelimit import RateLimiter


def test_the_first_call_is_immediate() -> None:
    assert RateLimiter(60).acquire() == 0.0


def test_subsequent_calls_are_spaced() -> None:
    limiter = RateLimiter(6000)  # 100/s -> 10ms apart
    limiter.acquire()
    started = time.monotonic()
    limiter.acquire()
    limiter.acquire()
    assert time.monotonic() - started >= 0.015


def test_a_non_positive_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        RateLimiter(0)


def test_slots_are_handed_out_once_each_under_contention() -> None:
    """Every worker gets its own turn; none share a slot."""
    limiter = RateLimiter(60000, burst=1)  # 1ms apart
    order: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        limiter.acquire()
        with lock:
            order.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(order) == 20


def test_an_idle_period_earns_back_at_most_the_burst() -> None:
    """Otherwise a pause turns into a thundering herd afterwards."""
    limiter = RateLimiter(60000, burst=3)
    time.sleep(0.05)  # long idle relative to the 1ms interval

    immediate = sum(1 for _ in range(10) if limiter.acquire() == 0.0)
    assert immediate <= 3


def test_it_works_as_a_context_manager() -> None:
    limiter = RateLimiter(60000)
    with limiter:
        pass

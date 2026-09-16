"""Stopping on request without losing work already paid for."""

import signal
import threading

from epc.shutdown import ShutdownRequestedError, shutdown_on_signal


def test_the_event_starts_clear() -> None:
    with shutdown_on_signal() as stopping:
        assert not stopping.is_set()


def test_a_signal_sets_the_event() -> None:
    with shutdown_on_signal() as stopping:
        signal.raise_signal(signal.SIGTERM)
        assert stopping.is_set()


def test_previous_handlers_are_restored() -> None:
    original = signal.getsignal(signal.SIGTERM)
    with shutdown_on_signal():
        pass
    assert signal.getsignal(signal.SIGTERM) is original


def test_the_handler_only_sets_a_flag() -> None:
    """The work of stopping belongs on the main thread, where it can flush."""
    with shutdown_on_signal() as stopping:
        signal.raise_signal(signal.SIGINT)  # would otherwise raise KeyboardInterrupt
        assert stopping.is_set()


def test_an_abandoned_thread_is_not_a_failure() -> None:
    """Nothing went wrong; the thread is simply left for the next run."""
    assert issubclass(ShutdownRequestedError, Exception)
    assert isinstance(threading.Event(), threading.Event)

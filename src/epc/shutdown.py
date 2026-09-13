"""Stopping on request without losing work already paid for.

ECS sends `SIGTERM` and then `SIGKILL` after `stopTimeout` — thirty seconds by
default. A run that ignores the first signal gets killed mid-flight, and every
classification it had finished but not yet written is lost. Those cost money.

So a shutdown request means: stop *starting* work, let what is running finish,
flush what has been decided, and exit saying so. It does not mean stop at once.
"""

import contextlib
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

from epc.errors import EpcError

# Signals worth honouring. SIGTERM is what a scheduler sends; SIGINT is Ctrl-C.
_HANDLED = (signal.SIGTERM, signal.SIGINT)


class ShutdownRequestedError(EpcError):
    """Raised in place of work that was never started because we are stopping.

    Not a failure: nothing went wrong, and the thread it stood for is simply
    left for the next run to pick up.
    """


@contextmanager
def shutdown_on_signal() -> Iterator[threading.Event]:
    """Yield an event that is set when a stop is requested.

    Previous handlers are restored on the way out, so this is safe to use in a
    library context and in tests.
    """
    event = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        # Deliberately does nothing but set a flag: the work of stopping
        # belongs on the main thread, where it can flush.
        event.set()

    previous = {}
    for number in _HANDLED:
        # Not the main thread means signals cannot be installed here; the event
        # still works for anyone who sets it by hand.
        with contextlib.suppress(ValueError):
            previous[number] = signal.signal(number, request_stop)

    try:
        yield event
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)

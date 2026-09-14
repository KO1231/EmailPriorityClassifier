"""Where a mutation goes once it has been planned.

Three destinations today. A queue-backed one joins them when the apply side is
deployed separately; it implements the same two methods.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from epc.actions.model import ThreadMutation
from epc.dispatch.applier import ApplyReport, MutationApplier
from epc.gmail.client import BATCH_MODIFY_LIMIT
from epc.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class MutationSink(Protocol):
    """Accepts mutations and is responsible for getting them somewhere."""

    def emit(self, mutation: ThreadMutation) -> None: ...

    def close(self) -> ApplyReport:
        """Flush anything held back, and report what happened."""
        ...


class DirectSink:
    """Apply in this process.

    Buffers rather than writing per thread: `batchModify` takes 1000 message IDs
    at a time, and a mutation at a time would throw that away. Nothing is lost
    on an early exit that `close()` is reached for — and the pipeline reaches it
    on the interrupt path too.
    """

    def __init__(self, applier: MutationApplier, *, batch_size: int = BATCH_MODIFY_LIMIT) -> None:
        self._applier = applier
        self._batch_size = batch_size
        self._buffer: list[ThreadMutation] = []
        self._report = ApplyReport()

    def emit(self, mutation: ThreadMutation) -> None:
        self._buffer.append(mutation)
        if len(self._buffer) >= self._batch_size:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        result = self._applier.apply(batch)
        self._report = _merge(self._report, result)

    def close(self) -> ApplyReport:
        self._flush()
        return self._report


class JsonlSink:
    """Write mutations to a file instead of applying them.

    This *is* dry-run — a destination rather than a flag checked inside a loop.
    The file it writes is replayable, so "review, then apply exactly what I
    reviewed" is a thing that can actually be done.

    Written a line at a time so an interrupted run still leaves a usable file.
    """

    def __init__(self, path: Path, *, log_planned: bool = False) -> None:
        self._path = path
        self._count = 0
        self._log_planned = log_planned
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="utf-8")

    def emit(self, mutation: ThreadMutation) -> None:
        self._handle.write(mutation.model_dump_json() + "\n")
        self._handle.flush()
        self._count += 1
        if self._log_planned:
            # Facts only. The reason is model output about the mail, and does
            # not go to a log.
            logger.info(
                "planned",
                thread_id=mutation.thread_id,
                origin=mutation.origin,
                priority=mutation.priority.value,
                add=mutation.add_label_ids,
                remove=mutation.remove_label_ids,
                suspicious=mutation.suspicious,
            )

    def close(self) -> ApplyReport:
        self._handle.close()
        # Nothing was applied, and saying so plainly is the point of a dry run.
        return ApplyReport(skipped_noop=self._count)

    @property
    def path(self) -> Path:
        return self._path


def read_mutations(path: Path) -> Iterable[ThreadMutation]:
    """Read back a file written by :class:`JsonlSink`.

    Yields rather than returning a list: a replay file can hold a whole mailbox.
    """
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield ThreadMutation.model_validate_json(stripped)
            except ValueError as exc:
                raise ValueError(f"{path}:{number} is not a valid mutation: {exc}") from exc


def _merge(first: ApplyReport, second: ApplyReport) -> ApplyReport:
    return ApplyReport(
        applied=first.applied + second.applied,
        skipped_noop=first.skipped_noop + second.skipped_noop,
        failed=first.failed + second.failed,
        api_calls=first.api_calls + second.api_calls,
        failures=[*first.failures, *second.failures],
    )


__all__ = ["DirectSink", "JsonlSink", "MutationSink", "read_mutations"]

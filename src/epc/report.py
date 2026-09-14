"""The record of what a run decided, kept so it can be examined later.

Three things become possible once classifications are written down, and none of
them are possible without it: finding out why a thread was rated the way it was
weeks afterwards, measuring whether a prompt change helped, and building an
evaluation set out of real decisions.

**What is recorded is deliberately not the mail.** A subject is stored as a
short digest and a sender as a bare domain. That is enough to recognise a
thread you are investigating and to spot patterns by sender, and not enough to
reconstruct the mailbox from the file — which matters because this file
outlives the run, gets copied to S3, and is the kind of thing that ends up in a
backup nobody thinks about.

The model's `reason` is left out too, unless asked for. It is a sentence the
model wrote *about* the mail — "the invoice for ¥3,000,000 from X is overdue" —
which makes it the mail's content by another route. Answering "why P1?" weeks
later needs it; keeping it is `observability.history_include_reason`.
"""

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from epc.actions.model import ThreadMutation
from epc.classify.base import Usage
from epc.priority import Priority

SUBJECT_DIGEST_CHARS = 16


def subject_digest(subject: str) -> str:
    """A stable short digest of a subject.

    Enough to match a record against a thread you are holding; not enough to
    read the subject back out of the file.
    """
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:SUBJECT_DIGEST_CHARS]


class ClassificationRecord(BaseModel):
    """One decision, as it is written to the history file."""

    ts: datetime
    thread_id: str
    subject_digest: str = ""
    sender_domain: str = ""
    message_count: int = 0

    priority: Priority
    confidence: float = 0.0
    # Empty unless `history_include_reason`; see the module docstring.
    reason: str = ""
    signals: list[str] = Field(default_factory=list)

    backend: str = ""
    model: str = ""
    prompt_version: str = ""

    suspicious: bool = False
    injection_patterns: list[str] = Field(default_factory=list)

    input_tokens: int = 0
    output_tokens: int = 0

    add_label_ids: list[str] = Field(default_factory=list)
    remove_label_ids: list[str] = Field(default_factory=list)
    # Where the change was handed: applied here, queued for another process, or
    # written to a plan file. Not whether it landed — a record is written as the
    # change is handed over, and `batchModify` reports no per-thread outcome to
    # wait for. An `applied: true` that could not know claimed more than that.
    dispatched_via: Literal["direct", "sqs", "jsonl"] = "direct"


def record_for(
    mutation: ThreadMutation,
    *,
    subject: str,
    sender_domain: str,
    message_count: int,
    usage: Usage,
    injection_patterns: list[str] | None = None,
    dispatched_via: Literal["direct", "sqs", "jsonl"] = "direct",
    include_reason: bool = False,
) -> ClassificationRecord:
    return ClassificationRecord(
        ts=datetime.now(UTC),
        thread_id=mutation.thread_id,
        subject_digest=subject_digest(subject),
        sender_domain=sender_domain,
        message_count=message_count,
        priority=mutation.priority,
        confidence=mutation.confidence,
        reason=mutation.reason if include_reason else "",
        signals=mutation.signals,
        backend=mutation.backend,
        model=mutation.model,
        prompt_version=mutation.prompt_version,
        suspicious=mutation.suspicious,
        injection_patterns=injection_patterns or [],
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        add_label_ids=mutation.add_label_ids,
        remove_label_ids=mutation.remove_label_ids,
        dispatched_via=dispatched_via,
    )


@runtime_checkable
class HistorySink(Protocol):
    """Somewhere to put classification records."""

    def write(self, record: ClassificationRecord) -> None: ...

    def close(self) -> None: ...


class NullHistorySink:
    """Keep no history. The default, because a record of decisions about your
    mail is something to opt into rather than to discover later."""

    def write(self, record: ClassificationRecord) -> None:  # noqa: ARG002 - protocol shape
        return None

    def close(self) -> None:
        return None


class JsonlHistorySink:
    """Append records to a date-stamped JSONL file.

    Appended and flushed per record, so an interrupted run still leaves
    everything it had decided by then.
    """

    def __init__(self, directory: Path, *, now: datetime | None = None) -> None:
        stamp = (now or datetime.now(UTC)).strftime("%Y%m%d")
        directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / f"classifications-{stamp}.jsonl"
        self._handle = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: ClassificationRecord) -> None:
        self._handle.write(record.model_dump_json() + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def read_records(path: Path) -> Iterator[ClassificationRecord]:
    """Read a history file back, for evaluation or for answering "why P1?"."""
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield ClassificationRecord.model_validate_json(stripped)
            except ValueError as exc:
                raise ValueError(f"{path}:{number} is not a valid record: {exc}") from exc


def estimate_cost_usd(usage: Usage, *, input_per_mtok: float, output_per_mtok: float) -> float:
    """Rough spend for a run.

    Rates are supplied rather than baked in: they differ per model and per
    provider, and a stale table in the source would be worse than no number.
    """
    return (usage.input_tokens * input_per_mtok + usage.output_tokens * output_per_mtok) / 1_000_000


__all__ = [
    "ClassificationRecord",
    "HistorySink",
    "JsonlHistorySink",
    "NullHistorySink",
    "estimate_cost_usd",
    "read_records",
    "record_for",
    "subject_digest",
]

"""What a run remembers between invocations: which threads keep failing.

There is no checkpoint. Every run searches the configured query, and the query
excludes threads that already carry a priority label — so it returns exactly
the threads still needing one, and the listing costs a page or two. That is
also what makes removing a label in Gmail, from mail of any age, a request to
classify it again. A checkpoint would have to be designed around that, and would
save almost nothing for the trouble.

What does need remembering is the thread that fails *every* time: a provider
refusing its content, a model that cannot produce a usable answer for it, a
message the parser cannot read. Without a record it is retried on every run,
each run exits reporting a failure, and an alarm on that exit code never stops.

A record is deliberately hard to earn and easy to lose:

* Only failures caused by the thread itself are recorded. Timeouts, throttling,
  outages, credential problems and bugs are not, so no outage can ever cause
  mail to be skipped.
* A thread is skipped only after failing on two runs *and* after some other
  thread has been classified successfully since it first failed. Without the
  second condition, a fault that rejects every request with the same error
  would look thread-specific one thread at a time.
* The record is dropped — and the thread tried again — as soon as the thread
  changes in Gmail, the model or the prompt changes, or thirty days pass.

The store is a protocol because where this lives differs by deployment: a file
locally, a parameter or an object in the cloud. It holds thread IDs and no
content, so it needs no special handling — just somewhere durable.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# Two, not one: a single refusal can be a fluke of sampling or of the provider.
SKIP_AFTER_ATTEMPTS = 2

# Long enough that a thread is not retried every few days, short enough that a
# provider relaxing a policy is eventually noticed without anyone asking.
FORGET_AFTER = timedelta(days=30)

# SSM Parameter Store's Standard tier holds 4 KB. `tests/unit/test_state.py`
# checks that this many worst-case records fit with room to spare. Past the
# limit, records that have not yet earned a skip go first, oldest first. A
# forgotten record costs a retry, never a skipped thread — but forgetting an
# earned skip first would let every new failure push one out, and the thread
# pushed out would fail, be recorded, and push out the next, on every run.
MAX_RECORDED_FAILURES = 24


def _now() -> datetime:
    # Seconds are plenty, and a shorter timestamp is more records per kilobyte.
    return datetime.now(UTC).replace(microsecond=0)


class FailureRecord(BaseModel):
    """One thread that failed for reasons of its own."""

    # The thread's Gmail `historyId` when it failed. Any change to the thread —
    # a reply, a label, being read — moves it, and a changed thread is worth
    # another attempt.
    history_id: str
    attempts: int = 1
    first_failed_at: datetime


class RunState(BaseModel):
    """The failure records, and what they are only valid for."""

    # Which mailbox the records belong to. Thread IDs mean nothing elsewhere.
    mailbox: str | None = None
    # Backend, model and a fingerprint of the prompts and policy. A thread one
    # model or prompt could not handle is worth offering to the next.
    classifier: str | None = None
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    failures: dict[str, FailureRecord] = Field(default_factory=dict)

    def for_run(self, *, mailbox: str, classifier: str, now: datetime | None = None) -> RunState:
        """This state with every record that no longer applies removed."""
        now = now or _now()
        if self.mailbox not in (None, mailbox) or self.classifier not in (None, classifier):
            return RunState(mailbox=mailbox, classifier=classifier)
        return self.model_copy(
            update={
                "mailbox": mailbox,
                "classifier": classifier,
                "failures": {
                    thread_id: record
                    for thread_id, record in self.failures.items()
                    if now - record.first_failed_at < FORGET_AFTER
                },
            }
        )

    def should_skip(self, thread_id: str, history_id: str) -> bool:
        """Whether this thread, as it is now, has earned being left alone."""
        record = self.failures.get(thread_id)
        return (
            record is not None
            and record.history_id == history_id
            and record.attempts >= SKIP_AFTER_ATTEMPTS
            and self.last_success_at is not None
            # At or after: a success in the same run as the first failure counts.
            and self.last_success_at >= record.first_failed_at
        )

    def after_run(
        self,
        *,
        failed: dict[str, str],
        succeeded: set[str],
        now: datetime | None = None,
    ) -> RunState:
        """Fold one run's outcome in.

        `failed` maps thread ID to the `historyId` it had, for thread-caused
        failures only; the caller decides what counts.
        """
        now = now or _now()
        failures = {tid: record for tid, record in self.failures.items() if tid not in succeeded}
        for thread_id, history_id in failed.items():
            previous = failures.get(thread_id)
            if previous is not None and previous.history_id == history_id:
                failures[thread_id] = previous.model_copy(update={"attempts": previous.attempts + 1})
            else:
                failures[thread_id] = FailureRecord(history_id=history_id, first_failed_at=now)

        if len(failures) > MAX_RECORDED_FAILURES:
            ranked = sorted(
                failures.items(),
                key=lambda item: (item[1].attempts >= SKIP_AFTER_ATTEMPTS, item[1].first_failed_at),
                reverse=True,
            )
            failures = dict(ranked[:MAX_RECORDED_FAILURES])

        return self.model_copy(
            update={
                "last_run_at": now,
                "last_success_at": now if succeeded else self.last_success_at,
                "failures": failures,
            }
        )

    def skipped(self) -> list[str]:
        """Thread IDs that have earned a skip, regardless of their current history."""
        return sorted(
            thread_id for thread_id, record in self.failures.items() if self.should_skip(thread_id, record.history_id)
        )


@runtime_checkable
class StateStore(Protocol):
    def load(self) -> RunState: ...

    def save(self, state: RunState) -> None: ...


class LocalFileStateStore:
    """State in a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> RunState:
        """Missing or unreadable state means "no records", never a crash.

        The cost of getting this wrong is retrying a few known failures; the
        cost of raising is a run that does nothing at all.
        """
        if not self._path.is_file():
            return RunState()
        try:
            return RunState.model_validate_json(self._path.read_text(encoding="utf-8"))
        except ValueError, json.JSONDecodeError, OSError:
            return RunState()

    def save(self, state: RunState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename: an interrupted save leaves the previous state intact
        # rather than a truncated file that reads as "no records".
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(self._path)


class NullStateStore:
    """Remember nothing. A thread that always fails is retried on every run."""

    def load(self) -> RunState:
        return RunState()

    def save(self, state: RunState) -> None:  # noqa: ARG002 - protocol shape
        return None


class SsmStateStore:
    """State in an SSM Parameter Store parameter.

    A plain `String`, not a `SecureString`: thread IDs and counters are not
    secrets, and encrypting them would only add a `kms:Decrypt` to the task role
    for nothing.
    """

    def __init__(self, name: str, client: Any = None, *, region: str | None = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("ssm", region_name=region)
        self._client = client
        self._name = name

    def load(self) -> RunState:
        """Missing or unreadable state means "no records", never a crash."""
        try:
            response = self._client.get_parameter(Name=self._name)
            return RunState.model_validate_json(str(response["Parameter"]["Value"]))
        except Exception:
            # Absent, malformed, or momentarily unreachable. The cost of being
            # wrong here is retrying a few known failures; the cost of raising
            # is a run that does nothing at all.
            return RunState()

    def save(self, state: RunState) -> None:
        self._client.put_parameter(Name=self._name, Value=state.model_dump_json(), Type="String", Overwrite=True)


class S3StateStore:
    """State as an object in S3.

    For when the state shares a bucket with the classification history and one
    place for run artefacts is simpler than two.
    """

    def __init__(self, bucket: str, key: str, client: Any = None, *, region: str | None = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region)
        self._client = client
        self._bucket = bucket
        self._key = key

    def load(self) -> RunState:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key)
            return RunState.model_validate_json(response["Body"].read().decode("utf-8"))
        except Exception:
            return RunState()

    def save(self, state: RunState) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key,
            Body=state.model_dump_json().encode("utf-8"),
            ContentType="application/json",
        )

"""Handing mutations to a queue, and picking them up again.

This is what lets the two halves fail and scale separately. Today a Gmail 429
during the write phase costs the classification that produced it; with a queue
the mutation waits, gets retried with backoff, and lands in a dead-letter queue
if it never succeeds — where it can be looked at and replayed rather than being
a line in a log.

**FIFO, with `MessageGroupId` set to the thread ID.** The ordering is the part
that protects correctness: once a rule can *remove* a label, an add and a remove
racing on the same thread is a real bug. Different threads are different groups,
so they still process in parallel.

**On deduplication, precisely.** `MessageDeduplicationId` gives a five-minute
window. That suppresses duplicates from a retry storm; it is not a durable
"never twice" guarantee, and nothing here depends on one. The real idempotency
comes from `batchModify` being naturally idempotent and from already-labelled
threads being excluded — SQS dedup is an optimisation on top of that.

`boto3` lives in the `aws` extra, so this module is imported only when selected.
"""

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from epc.actions.model import ThreadMutation
from epc.dispatch.applier import ApplyReport

if TYPE_CHECKING:  # pragma: no cover - stubs are a dev dependency
    from mypy_boto3_sqs.client import SQSClient
    from mypy_boto3_sqs.type_defs import SendMessageBatchRequestEntryTypeDef
else:
    SQSClient = Any
    SendMessageBatchRequestEntryTypeDef = Any

# `SendMessageBatch` takes at most ten entries, and the total must stay under
# 256 KB. A mutation is well under a kilobyte, so the count is the binding limit.
SEND_BATCH_LIMIT = 10
RECEIVE_BATCH_LIMIT = 10


class SqsSink:
    """Enqueue mutations instead of applying them here.

    Batched because ten messages in one call cost one round trip rather than
    ten, and a classification run produces them in bursts.
    """

    def __init__(self, client: SQSClient, queue_url: str, *, batch_size: int = SEND_BATCH_LIMIT) -> None:
        self._client = client
        self._queue_url = queue_url
        self._batch_size = min(batch_size, SEND_BATCH_LIMIT)
        self._buffer: list[ThreadMutation] = []
        self._sent = 0
        self._failed = 0
        self._failures: list[str] = []

    def emit(self, mutation: ThreadMutation) -> None:
        if mutation.is_noop:
            # Nothing to ask for. Enqueuing it would cost a message and a
            # consumer invocation to discover the same thing.
            return
        self._buffer.append(mutation)
        if len(self._buffer) >= self._batch_size:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []

        entries: list[SendMessageBatchRequestEntryTypeDef] = [
            {
                "Id": str(index),
                "MessageBody": mutation.model_dump_json(),
                # One group per thread: ordering within a thread, parallelism
                # across threads.
                "MessageGroupId": mutation.thread_id,
                # Thread plus the exact change requested — so a replay of the
                # same change dedupes and a different change does not.
                "MessageDeduplicationId": mutation.idempotency_key,
            }
            for index, mutation in enumerate(batch)
        ]

        response = self._client.send_message_batch(QueueUrl=self._queue_url, Entries=entries)
        failed = response.get("Failed") or []
        self._sent += len(batch) - len(failed)
        self._failed += len(failed)
        for failure in failed:
            index = int(failure.get("Id", 0))
            thread_id = batch[index].thread_id if index < len(batch) else "?"
            self._failures.append(f"{thread_id}: {failure.get('Code')} {failure.get('Message')}")

    def close(self) -> ApplyReport:
        self._flush()
        # Nothing was applied here — the consumer does that. Reporting the
        # enqueue count as `applied` would claim work that has not happened.
        return ApplyReport(
            applied=0,
            handed_off=self._sent,
            failed=self._failed,
            api_calls=0,
            failures=self._failures,
        )

    @property
    def enqueued(self) -> int:
        return self._sent


class SqsSource:
    """Drain a queue, yielding mutations and deleting what was handled.

    Deletion is the caller's signal that a message is done, so a mutation that
    fails to apply becomes visible again and eventually reaches the dead-letter
    queue rather than disappearing.
    """

    def __init__(
        self,
        client: SQSClient,
        queue_url: str,
        *,
        wait_seconds: int = 20,
        visibility_timeout: int = 60,
    ) -> None:
        self._client = client
        self._queue_url = queue_url
        # Long polling: an empty short poll costs a request and returns nothing.
        self._wait_seconds = wait_seconds
        self._visibility_timeout = visibility_timeout

    def __iter__(self) -> Iterator[tuple[ThreadMutation, str]]:
        """Yield each mutation with the receipt handle that acknowledges it."""
        while True:
            response = self._client.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=RECEIVE_BATCH_LIMIT,
                WaitTimeSeconds=self._wait_seconds,
                VisibilityTimeout=self._visibility_timeout,
            )
            messages = response.get("Messages") or []
            if not messages:
                return
            for message in messages:
                body = message.get("Body") or ""
                receipt = message.get("ReceiptHandle") or ""
                try:
                    mutation = ThreadMutation.model_validate_json(body)
                except ValueError:
                    # Malformed, and it will stay malformed. Leave it alone so
                    # the redrive policy moves it to the dead-letter queue,
                    # where it can be looked at.
                    continue
                yield mutation, receipt

    def acknowledge(self, receipts: list[str]) -> None:
        """Delete handled messages. Anything not deleted comes back."""
        for start in range(0, len(receipts), RECEIVE_BATCH_LIMIT):
            chunk = receipts[start : start + RECEIVE_BATCH_LIMIT]
            self._client.delete_message_batch(
                QueueUrl=self._queue_url,
                Entries=[{"Id": str(i), "ReceiptHandle": r} for i, r in enumerate(chunk)],
            )


def parse_mutations(bodies: list[str]) -> list[ThreadMutation]:
    """Parse message bodies, as a Lambda handler receives them."""
    parsed = []
    for body in bodies:
        try:
            parsed.append(ThreadMutation.model_validate_json(body))
        except ValueError:
            continue
    return parsed

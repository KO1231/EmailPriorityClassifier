"""The queue path. Fakes only — no AWS, no credentials."""

from datetime import UTC, datetime
from typing import Any, cast

from epc.actions.model import ThreadMutation
from epc.dispatch.sqs import SqsSink, SqsSource, parse_mutations
from epc.priority import Priority


def mutation(thread_id: str, *, add: list[str] | None = None) -> ThreadMutation:
    m = ThreadMutation(
        thread_id=thread_id,
        message_ids=[f"{thread_id}-m1"],
        add_label_ids=add if add is not None else ["Label_1"],
        priority=Priority.P1,
        classified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    m.idempotency_key = f"key-{thread_id}"
    return m


class FakeSqs:
    def __init__(self, *, fail_ids: set[str] | None = None, pages: list[list[dict[str, Any]]] | None = None):
        self.sent: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self._fail_ids = fail_ids or set()
        self._pages = pages or []

    def send_message_batch(self, *, QueueUrl: str, Entries: Any) -> Any:  # noqa: N803
        self.sent.append({"url": QueueUrl, "entries": list(Entries)})
        failed = [
            {"Id": e["Id"], "Code": "Throttled", "Message": "slow down"}
            for e in Entries
            if e["MessageGroupId"] in self._fail_ids
        ]
        return {"Successful": [], "Failed": failed}

    def receive_message(self, **kwargs: Any) -> Any:
        return {"Messages": self._pages.pop(0)} if self._pages else {}

    def delete_message_batch(self, *, QueueUrl: str, Entries: Any) -> Any:  # noqa: N803
        self.deleted.append({"url": QueueUrl, "entries": list(Entries)})
        return {}


def sink(client: FakeSqs, **kw: Any) -> SqsSink:
    return SqsSink(cast(Any, client), "https://sqs/q.fifo", **kw)


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


def test_messages_are_batched() -> None:
    """Ten per call rather than ten calls; a run produces them in bursts."""
    client = FakeSqs()
    s = sink(client)
    for i in range(10):
        s.emit(mutation(f"t{i}"))
    s.close()

    assert len(client.sent) == 1
    assert len(client.sent[0]["entries"]) == 10


def test_a_partial_batch_is_flushed_on_close() -> None:
    client = FakeSqs()
    s = sink(client)
    s.emit(mutation("t1"))
    assert client.sent == []
    s.close()
    assert len(client.sent[0]["entries"]) == 1


def test_the_group_is_the_thread() -> None:
    """Ordering within a thread, parallelism across threads. Once a rule can
    remove a label, an add and a remove racing on one thread is a real bug."""
    client = FakeSqs()
    s = sink(client)
    s.emit(mutation("thread-a"))
    s.close()
    assert client.sent[0]["entries"][0]["MessageGroupId"] == "thread-a"


def test_the_dedup_id_is_the_idempotency_key() -> None:
    """Thread plus the exact change: a replay dedupes, a different change does not."""
    client = FakeSqs()
    s = sink(client)
    s.emit(mutation("t1"))
    s.close()
    assert client.sent[0]["entries"][0]["MessageDeduplicationId"] == "key-t1"


def test_a_noop_is_never_enqueued() -> None:
    """It would cost a message and a consumer invocation to learn the same thing."""
    client = FakeSqs()
    s = sink(client)
    s.emit(mutation("t1", add=[]))
    s.close()
    assert client.sent == []


def test_failed_entries_are_reported_not_swallowed() -> None:
    client = FakeSqs(fail_ids={"t2"})
    s = sink(client)
    s.emit(mutation("t1"))
    s.emit(mutation("t2"))
    report = s.close()

    assert report.failed == 1
    assert report.had_failures
    assert "t2" in report.failures[0]


def test_enqueuing_is_not_reported_as_applied() -> None:
    """The consumer applies them. Claiming otherwise would overstate the run."""
    client = FakeSqs()
    s = sink(client)
    s.emit(mutation("t1"))
    report = s.close()

    assert report.applied == 0
    assert s.enqueued == 1


def test_the_body_round_trips() -> None:
    client = FakeSqs()
    s = sink(client)
    original = mutation("t1")
    s.emit(original)
    s.close()

    body = client.sent[0]["entries"][0]["MessageBody"]
    assert ThreadMutation.model_validate_json(body) == original


# --------------------------------------------------------------------------
# Receiving
# --------------------------------------------------------------------------


def message(m: ThreadMutation, receipt: str) -> dict[str, Any]:
    return {"Body": m.model_dump_json(), "ReceiptHandle": receipt}


def test_messages_are_yielded_with_their_receipts() -> None:
    client = FakeSqs(pages=[[message(mutation("t1"), "r1"), message(mutation("t2"), "r2")]])
    got = list(SqsSource(cast(Any, client), "https://sqs/q.fifo", wait_seconds=0))

    assert [m.thread_id for m, _ in got] == ["t1", "t2"]
    assert [r for _, r in got] == ["r1", "r2"]


def test_draining_stops_on_an_empty_receive() -> None:
    client = FakeSqs(pages=[[message(mutation("t1"), "r1")]])
    assert len(list(SqsSource(cast(Any, client), "url", wait_seconds=0))) == 1


def test_a_malformed_message_is_left_for_the_dead_letter_queue() -> None:
    """It will stay malformed. Not acknowledging it is how it gets looked at."""
    client = FakeSqs(pages=[[{"Body": "{ not json", "ReceiptHandle": "r1"}]])
    assert list(SqsSource(cast(Any, client), "url", wait_seconds=0)) == []


def test_acknowledging_deletes_in_batches() -> None:
    client = FakeSqs()
    SqsSource(cast(Any, client), "url").acknowledge([f"r{i}" for i in range(15)])

    assert len(client.deleted) == 2
    assert len(client.deleted[0]["entries"]) == 10
    assert len(client.deleted[1]["entries"]) == 5


def test_parse_mutations_skips_what_it_cannot_read() -> None:
    bodies = [mutation("t1").model_dump_json(), "{ not json", mutation("t2").model_dump_json()]
    assert [m.thread_id for m in parse_mutations(bodies)] == ["t1", "t2"]

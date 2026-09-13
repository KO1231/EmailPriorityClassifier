"""Sinks, the applier, and the batching that makes the write phase cheap."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from epc.actions.model import ThreadMutation
from epc.dispatch.applier import MutationApplier
from epc.dispatch.sink import DirectSink, JsonlSink, MutationSink, read_mutations
from epc.errors import GmailError
from epc.priority import Priority


def mutation(thread_id: str, *, add: list[str], remove: list[str] | None = None, ids: int = 2) -> ThreadMutation:
    return ThreadMutation(
        thread_id=thread_id,
        message_ids=[f"{thread_id}-m{i}" for i in range(ids)],
        add_label_ids=add,
        remove_label_ids=remove or [],
        priority=Priority.P1,
        classified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class FakeClient:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_on = fail_on

    def batch_modify(self, message_ids: Any, *, add_label_ids: Any = (), remove_label_ids: Any = ()) -> None:
        if self._fail_on and self._fail_on in list(add_label_ids):
            raise GmailError("nope")
        self.calls.append({"ids": list(message_ids), "add": list(add_label_ids), "remove": list(remove_label_ids)})


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------


def test_threads_wanting_the_same_change_share_one_call() -> None:
    """This is the 1500-round-trips-to-two difference."""
    client = FakeClient()
    report = MutationApplier(client).apply(  # type: ignore[arg-type]
        [mutation(f"t{i}", add=["Label_1"]) for i in range(50)]
    )

    assert report.applied == 50
    assert report.api_calls == 1
    assert len(client.calls[0]["ids"]) == 100


def test_different_changes_get_different_calls() -> None:
    client = FakeClient()
    report = MutationApplier(client).apply(  # type: ignore[arg-type]
        [
            mutation("a", add=["Label_1"]),
            mutation("b", add=["Label_2"]),
            mutation("c", add=["Label_1"]),
        ]
    )
    assert report.api_calls == 2
    assert report.applied == 3


def test_the_grouping_ignores_label_order() -> None:
    client = FakeClient()
    report = MutationApplier(client).apply(  # type: ignore[arg-type]
        [mutation("a", add=["X", "Y"]), mutation("b", add=["Y", "X"])]
    )
    assert report.api_calls == 1


def test_message_ids_are_chunked_to_the_api_limit() -> None:
    client = FakeClient()
    report = MutationApplier(client).apply(  # type: ignore[arg-type]
        [mutation(f"t{i}", add=["Label_1"], ids=3) for i in range(500)]
    )
    assert report.api_calls == 2  # 1500 message IDs -> two calls
    assert all(len(call["ids"]) <= 1000 for call in client.calls)


def test_a_noop_costs_no_api_call() -> None:
    report = MutationApplier(FakeClient()).apply([mutation("a", add=[])])  # type: ignore[arg-type]
    assert (report.applied, report.skipped_noop, report.api_calls) == (0, 1, 0)


def test_a_failed_group_does_not_stop_the_others() -> None:
    client = FakeClient(fail_on="Label_bad")
    report = MutationApplier(client).apply(  # type: ignore[arg-type]
        [mutation("a", add=["Label_bad"]), mutation("b", add=["Label_ok"])]
    )
    assert report.applied == 1
    assert report.failed == 1
    assert report.had_failures
    assert report.failures


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------


def test_the_direct_sink_buffers_then_writes_once() -> None:
    client = FakeClient()
    sink = DirectSink(MutationApplier(client))  # type: ignore[arg-type]
    for index in range(10):
        sink.emit(mutation(f"t{index}", add=["Label_1"]))

    assert client.calls == []  # nothing written yet
    report = sink.close()
    assert report.applied == 10
    assert report.api_calls == 1


def test_the_direct_sink_flushes_when_the_buffer_fills() -> None:
    client = FakeClient()
    sink = DirectSink(MutationApplier(client), batch_size=3)  # type: ignore[arg-type]
    for index in range(7):
        sink.emit(mutation(f"t{index}", add=["Label_1"]))

    assert len(client.calls) == 2  # two full batches flushed
    assert sink.close().applied == 7


def test_the_jsonl_sink_applies_nothing(tmp_path: Path) -> None:
    """Dry run as a destination, not a flag checked inside a loop."""
    path = tmp_path / "nested" / "mutations.jsonl"
    sink = JsonlSink(path)
    sink.emit(mutation("a", add=["Label_1"]))
    sink.emit(mutation("b", add=["Label_2"]))
    report = sink.close()

    assert report.applied == 0
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_the_jsonl_file_is_readable_after_an_interrupted_run(tmp_path: Path) -> None:
    """Written a line at a time, so a killed run still leaves a usable file."""
    path = tmp_path / "mutations.jsonl"
    sink = JsonlSink(path)
    sink.emit(mutation("a", add=["Label_1"]))
    # close() is never reached.
    assert "Label_1" in path.read_text(encoding="utf-8")


def test_a_dry_run_can_be_replayed_exactly(tmp_path: Path) -> None:
    """Review, then apply what was reviewed."""
    path = tmp_path / "mutations.jsonl"
    planned = [mutation("a", add=["Label_1"]), mutation("b", add=["Label_1"], remove=["INBOX"])]

    sink = JsonlSink(path)
    for item in planned:
        sink.emit(item)
    sink.close()

    replayed = list(read_mutations(path))
    assert replayed == planned

    client = FakeClient()
    assert MutationApplier(client).apply(replayed).applied == 2  # type: ignore[arg-type]


def test_a_corrupt_replay_file_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "mutations.jsonl"
    path.write_text('{"thread_id": "a"}\n{ not json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"mutations\.jsonl:1"):
        list(read_mutations(path))


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "mutations.jsonl"
    sink = JsonlSink(path)
    sink.emit(mutation("a", add=["Label_1"]))
    sink.close()
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(list(read_mutations(path))) == 1


def test_both_sinks_satisfy_the_protocol(tmp_path: Path) -> None:
    assert isinstance(DirectSink(MutationApplier(FakeClient())), MutationSink)  # type: ignore[arg-type]
    assert isinstance(JsonlSink(tmp_path / "x.jsonl"), MutationSink)

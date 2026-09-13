"""The classification history: enough to investigate, not enough to reconstruct."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from epc.actions.model import ThreadMutation
from epc.classify.base import Usage
from epc.priority import Priority
from epc.report import (
    JsonlHistorySink,
    NullHistorySink,
    estimate_cost_usd,
    read_records,
    record_for,
    subject_digest,
)

MUTATION = ThreadMutation(
    thread_id="t1",
    message_ids=["m1"],
    add_label_ids=["Label_1"],
    priority=Priority.P1,
    confidence=0.9,
    reason="Deadline tomorrow",
    classified_at=datetime(2026, 1, 1, tzinfo=UTC),
    backend="openai",
    model="test-model",
    prompt_version="1",
)


def test_the_subject_is_stored_as_a_digest() -> None:
    """Enough to match a record against a thread you are holding; not enough to
    read the subject back out of the file."""
    record = record_for(MUTATION, subject="Q3 layoffs", sender_domain="example.com", message_count=1, usage=Usage())
    assert record.subject_digest == subject_digest("Q3 layoffs")
    assert "layoffs" not in record.model_dump_json()


def test_the_digest_is_stable() -> None:
    assert subject_digest("hello") == subject_digest("hello")
    assert subject_digest("hello") != subject_digest("goodbye")


def test_only_the_sender_domain_is_kept() -> None:
    record = record_for(MUTATION, subject="s", sender_domain="example.com", message_count=1, usage=Usage())
    assert record.sender_domain == "example.com"
    assert "@" not in record.model_dump_json()


def test_provenance_travels_with_the_record() -> None:
    """So a change in accuracy can be attributed rather than guessed at."""
    record = record_for(MUTATION, subject="s", sender_domain="d", message_count=1, usage=Usage(input_tokens=100))
    assert (record.backend, record.model, record.prompt_version) == ("openai", "test-model", "1")
    assert record.input_tokens == 100


def test_records_round_trip_through_a_file(tmp_path: Path) -> None:
    sink = JsonlHistorySink(tmp_path, now=datetime(2026, 3, 4, tzinfo=UTC))
    record = record_for(MUTATION, subject="s", sender_domain="d", message_count=1, usage=Usage(), applied=True)
    sink.write(record)
    sink.close()

    assert sink.path.name == "classifications-20260304.jsonl"
    assert list(read_records(sink.path)) == [record]


def test_the_file_is_appended_so_an_interrupted_run_keeps_its_records(tmp_path: Path) -> None:
    stamp = datetime(2026, 3, 4, tzinfo=UTC)
    first = JsonlHistorySink(tmp_path, now=stamp)
    first.write(record_for(MUTATION, subject="a", sender_domain="d", message_count=1, usage=Usage()))
    # No close(): the run was killed.
    second = JsonlHistorySink(tmp_path, now=stamp)
    second.write(record_for(MUTATION, subject="b", sender_domain="d", message_count=1, usage=Usage()))
    second.close()

    assert len(list(read_records(second.path))) == 2


def test_a_corrupt_history_file_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text("{ not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"history\.jsonl:1"):
        list(read_records(path))


def test_keeping_no_history_is_the_default_behaviour() -> None:
    sink = NullHistorySink()
    sink.write(record_for(MUTATION, subject="s", sender_domain="d", message_count=1, usage=Usage()))
    sink.close()


def test_cost_is_computed_from_supplied_rates() -> None:
    """Rates are per model and per provider; a stale table in the source would
    be worse than no number."""
    cost = estimate_cost_usd(
        Usage(input_tokens=1_000_000, output_tokens=500_000),
        input_per_mtok=0.25,
        output_per_mtok=2.0,
    )
    assert cost == pytest.approx(1.25)

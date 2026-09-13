"""The checkpoint between runs."""

from pathlib import Path

from epc.state import LocalFileStateStore, NullStateStore, RunState


def test_a_missing_checkpoint_means_start_over(tmp_path: Path) -> None:
    """Never a crash: the cost of getting this wrong is one full scan, and the
    cost of raising is a run that does nothing at all."""
    assert LocalFileStateStore(tmp_path / "absent.json").load().history_id is None


def test_a_corrupt_checkpoint_means_start_over(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    assert LocalFileStateStore(path).load().history_id is None


def test_a_checkpoint_round_trips(tmp_path: Path) -> None:
    store = LocalFileStateStore(tmp_path / "nested" / "state.json")
    store.save(RunState(history_id="123", mailbox="me@example.com"))

    loaded = store.load()
    assert loaded.history_id == "123"
    assert loaded.usable_for("me@example.com")


def test_a_checkpoint_from_another_mailbox_is_not_usable() -> None:
    assert not RunState(history_id="1", mailbox="a@example.com").usable_for("b@example.com")


def test_a_checkpoint_with_no_mailbox_recorded_is_accepted() -> None:
    """Written before mailboxes were recorded; still better than a full scan."""
    assert RunState(history_id="1").usable_for("anyone@example.com")


def test_an_empty_checkpoint_is_never_usable() -> None:
    assert not RunState().usable_for("me@example.com")


def test_advancing_stamps_the_time_and_keeps_the_mailbox() -> None:
    advanced = RunState(mailbox="me@example.com").advanced_to("999")
    assert advanced.history_id == "999"
    assert advanced.mailbox == "me@example.com"
    assert advanced.last_run_at is not None


def test_an_interrupted_save_leaves_the_previous_checkpoint(tmp_path: Path) -> None:
    """Written to a temporary file and renamed, so a truncated file never
    reads back as "never run"."""
    path = tmp_path / "state.json"
    store = LocalFileStateStore(path)
    store.save(RunState(history_id="1"))
    store.save(RunState(history_id="2"))

    assert store.load().history_id == "2"
    assert not list(tmp_path.glob("*.tmp"))


def test_the_null_store_remembers_nothing() -> None:
    store = NullStateStore()
    store.save(RunState(history_id="1"))
    assert store.load().history_id is None

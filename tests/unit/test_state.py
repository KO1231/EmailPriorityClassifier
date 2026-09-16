"""Run state: the record of threads that keep failing for reasons of their own."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from epc.state import (
    FORGET_AFTER,
    MAX_RECORDED_FAILURES,
    SKIP_AFTER_ATTEMPTS,
    FailureRecord,
    LocalFileStateStore,
    NullStateStore,
    RunState,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def later(**delta: float) -> datetime:
    return T0 + timedelta(**delta)


def failed_twice_with_a_success(thread_id: str = "t1", history_id: str = "100") -> RunState:
    """The shape that earns a skip: two failed runs, and the classifier working
    on something else in between."""
    state = RunState().after_run(failed={thread_id: history_id}, succeeded={"other"}, now=T0)
    return state.after_run(failed={thread_id: history_id}, succeeded=set(), now=later(hours=1))


# --------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------


def test_missing_state_means_no_records(tmp_path: Path) -> None:
    """Never a crash: the cost of getting this wrong is retrying a few known
    failures, and the cost of raising is a run that does nothing at all."""
    assert LocalFileStateStore(tmp_path / "absent.json").load().failures == {}


def test_corrupt_state_means_no_records(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    assert LocalFileStateStore(path).load().failures == {}


def test_state_round_trips(tmp_path: Path) -> None:
    store = LocalFileStateStore(tmp_path / "nested" / "state.json")
    store.save(failed_twice_with_a_success())
    assert store.load().should_skip("t1", "100")


def test_a_checkpoint_left_by_the_previous_design_still_loads(tmp_path: Path) -> None:
    """State files and SSM parameters written before the checkpoint was removed
    must not stop a run."""
    path = tmp_path / "state.json"
    path.write_text('{"history_id": "4000", "mailbox": "me@example.com", "last_run_at": null}', encoding="utf-8")
    loaded = LocalFileStateStore(path).load()
    assert loaded.mailbox == "me@example.com"
    assert loaded.failures == {}


def test_an_interrupted_save_leaves_the_previous_state(tmp_path: Path) -> None:
    """Written to a temporary file and renamed, so a truncated file never reads
    back as "no records"."""
    path = tmp_path / "state.json"
    store = LocalFileStateStore(path)
    store.save(RunState(mailbox="a@example.com"))
    store.save(RunState(mailbox="b@example.com"))

    assert store.load().mailbox == "b@example.com"
    assert not list(tmp_path.glob("*.tmp"))


def test_the_null_store_remembers_nothing() -> None:
    store = NullStateStore()
    store.save(failed_twice_with_a_success())
    assert store.load().failures == {}


def test_the_worst_case_fits_an_ssm_standard_parameter() -> None:
    """4 KB. Every record at its longest, and every optional field filled."""
    failures = {
        f"{n:016x}": FailureRecord(history_id="9" * 20, attempts=999, first_failed_at=later(days=n))
        for n in range(MAX_RECORDED_FAILURES)
    }
    state = RunState(
        mailbox="a-rather-long-mailbox-address.for-testing@subdomain.example.co.jp",
        classifier="bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0/0123456789ab",
        last_run_at=T0,
        last_success_at=T0,
        failures=failures,
    )
    assert len(state.model_dump_json().encode("utf-8")) < 4096 * 0.9


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_a_first_failure_is_recorded_once() -> None:
    state = RunState().after_run(failed={"t1": "100"}, succeeded=set(), now=T0)
    record = state.failures["t1"]
    assert record.attempts == 1
    assert record.first_failed_at == T0


def test_failing_again_unchanged_counts_another_attempt() -> None:
    state = RunState().after_run(failed={"t1": "100"}, succeeded=set(), now=T0)
    state = state.after_run(failed={"t1": "100"}, succeeded=set(), now=later(hours=1))
    assert state.failures["t1"].attempts == 2
    assert state.failures["t1"].first_failed_at == T0


def test_failing_after_the_thread_changed_starts_over() -> None:
    state = failed_twice_with_a_success()
    state = state.after_run(failed={"t1": "200"}, succeeded=set(), now=later(hours=2))
    assert state.failures["t1"].attempts == 1
    assert state.failures["t1"].history_id == "200"


def test_a_success_clears_the_record() -> None:
    state = failed_twice_with_a_success()
    state = state.after_run(failed={}, succeeded={"t1"}, now=later(hours=2))
    assert "t1" not in state.failures


def test_only_a_run_with_a_success_moves_the_last_success() -> None:
    state = RunState().after_run(failed={}, succeeded={"t9"}, now=T0)
    state = state.after_run(failed={"t1": "1"}, succeeded=set(), now=later(hours=1))
    assert state.last_success_at == T0
    assert state.last_run_at == later(hours=1)


def test_past_the_cap_the_oldest_records_go_first() -> None:
    """A forgotten record costs a retry. Never a skip."""
    state = RunState()
    for n in range(MAX_RECORDED_FAILURES + 5):
        state = state.after_run(failed={f"t{n}": "1"}, succeeded=set(), now=later(minutes=n))
    assert len(state.failures) == MAX_RECORDED_FAILURES
    assert "t0" not in state.failures
    assert f"t{MAX_RECORDED_FAILURES + 4}" in state.failures


def test_past_the_cap_an_earned_skip_outlives_a_first_failure() -> None:
    """Dropping earned skips first let every new failure push one out; the thread
    pushed out failed again next run and pushed out another, every run, each a
    paid request refused the same way."""
    confirmed = {f"old{n}": "1" for n in range(MAX_RECORDED_FAILURES)}
    state = RunState().after_run(failed=confirmed, succeeded={"ok"}, now=T0)
    state = state.after_run(failed=confirmed, succeeded=set(), now=later(hours=1))
    assert len(state.skipped()) == MAX_RECORDED_FAILURES

    state = state.after_run(failed={"new": "1"}, succeeded=set(), now=later(hours=2))
    assert len(state.skipped()) == MAX_RECORDED_FAILURES
    assert "new" not in state.failures


# --------------------------------------------------------------------------
# Skipping
# --------------------------------------------------------------------------


def test_two_failures_and_a_success_since_earn_a_skip() -> None:
    assert SKIP_AFTER_ATTEMPTS == 2
    assert failed_twice_with_a_success().should_skip("t1", "100")


def test_one_failure_is_not_enough() -> None:
    state = RunState().after_run(failed={"t1": "100"}, succeeded={"other"}, now=T0)
    assert not state.should_skip("t1", "100")


def test_failures_with_no_success_since_are_never_skipped() -> None:
    """A fault that refuses every request looks thread-specific one thread at a
    time. Until something else has worked, nothing is skipped."""
    state = RunState(last_success_at=later(hours=-1))
    for hour in range(5):
        state = state.after_run(failed={"t1": "100", "t2": "100"}, succeeded=set(), now=later(hours=hour))
    assert not state.should_skip("t1", "100")
    assert state.skipped() == []


def test_a_success_in_the_same_run_as_the_first_failure_counts() -> None:
    """Otherwise a mailbox with one poisoned thread and little other mail would
    retry it — and report a failure — for as long as mail stays that quiet."""
    state = RunState().after_run(failed={"t1": "100"}, succeeded={"t2"}, now=T0)
    state = state.after_run(failed={"t1": "100"}, succeeded=set(), now=later(hours=1))
    assert state.should_skip("t1", "100")


def test_a_changed_thread_is_not_skipped() -> None:
    """A reply, a label, being read: any of them is worth another attempt."""
    assert not failed_twice_with_a_success().should_skip("t1", "101")


def test_records_from_another_mailbox_do_not_apply() -> None:
    state = failed_twice_with_a_success().model_copy(update={"mailbox": "a@example.com", "classifier": "c"})
    assert state.for_run(mailbox="b@example.com", classifier="c", now=later(hours=2)).failures == {}


def test_a_new_model_or_prompt_retries_everything() -> None:
    state = failed_twice_with_a_success().model_copy(update={"mailbox": "a@example.com", "classifier": "old"})
    assert state.for_run(mailbox="a@example.com", classifier="new", now=later(hours=2)).failures == {}


def test_records_are_kept_for_the_same_mailbox_and_classifier() -> None:
    state = failed_twice_with_a_success().model_copy(update={"mailbox": "a@example.com", "classifier": "c"})
    assert state.for_run(mailbox="a@example.com", classifier="c", now=later(hours=2)).should_skip("t1", "100")


def test_records_are_forgotten_after_a_while() -> None:
    state = failed_twice_with_a_success().model_copy(update={"mailbox": "a@example.com", "classifier": "c"})
    assert state.for_run(mailbox="a@example.com", classifier="c", now=T0 + FORGET_AFTER).failures == {}

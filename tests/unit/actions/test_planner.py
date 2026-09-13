"""Planning: priority in, label changes out. Nothing reads model free text."""

from datetime import UTC, datetime

import pytest

from epc.actions.model import ActionVerb, ThreadMutation
from epc.actions.planner import plan_mutation
from epc.actions.rules import ActionCondition, ActionRule, select_verbs
from epc.gmail.models import NON_PRIMARY_CATEGORIES
from epc.priority import Priority

LABELS = {Priority.P1: "Label_1", Priority.P2: "Label_2", Priority.P3: "Label_3"}
ALL_CATEGORIES = set(NON_PRIMARY_CATEGORIES)

P1_RULE = ActionRule(
    when=ActionCondition(priority=Priority.P1),
    unless=ActionCondition(any_label=["SPAM", "TRASH"]),
    do=[ActionVerb.ADD_STAR, ActionVerb.MOVE_TO_PRIMARY, ActionVerb.MARK_IMPORTANT],
)


def plan(**overrides: object) -> ThreadMutation:
    kwargs: dict[str, object] = {
        "thread_id": "t1",
        "message_ids": ["m1", "m2"],
        "thread_label_ids": {"INBOX", "UNREAD"},
        "priority": Priority.P1,
        "priority_label_ids": LABELS,
        "rules": [P1_RULE],
        "move_targets": ALL_CATEGORIES,
        "now": datetime(2026, 1, 1, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return plan_mutation(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The priority label
# --------------------------------------------------------------------------


def test_the_priority_label_is_always_added() -> None:
    """Not a rule — the point of the tool."""
    assert "Label_1" in plan().add_label_ids


def test_each_priority_gets_its_own_label() -> None:
    assert plan(priority=Priority.P3, rules=[]).add_label_ids == ["Label_3"]


def test_a_label_the_thread_already_has_is_not_re_added() -> None:
    mutation = plan(thread_label_ids={"INBOX", "Label_1"}, rules=[])
    assert mutation.add_label_ids == []
    assert mutation.is_noop


# --------------------------------------------------------------------------
# Verbs
# --------------------------------------------------------------------------


def test_a_matching_rule_adds_its_verbs() -> None:
    mutation = plan(thread_label_ids={"INBOX", "UNREAD", "CATEGORY_PROMOTIONS"})
    assert set(mutation.add_label_ids) == {"Label_1", "STARRED", "IMPORTANT"}
    assert mutation.remove_label_ids == ["CATEGORY_PROMOTIONS"]


def test_moving_to_primary_only_removes_categories_the_thread_has() -> None:
    mutation = plan(thread_label_ids={"INBOX", "CATEGORY_SOCIAL"})
    assert mutation.remove_label_ids == ["CATEGORY_SOCIAL"]


def test_moving_to_primary_is_a_noop_once_the_category_is_gone() -> None:
    """Naturally idempotent: a second run finds nothing to remove."""
    mutation = plan(thread_label_ids={"INBOX", "Label_1", "STARRED", "IMPORTANT"})
    assert mutation.remove_label_ids == []


def test_move_targets_restrict_which_tabs_are_left() -> None:
    mutation = plan(
        thread_label_ids={"INBOX", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"},
        move_targets={"CATEGORY_PROMOTIONS"},
    )
    assert mutation.remove_label_ids == ["CATEGORY_PROMOTIONS"]


def test_a_non_matching_priority_gets_only_its_label() -> None:
    assert plan(priority=Priority.P3).add_label_ids == ["Label_3"]


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def test_the_spam_exception_holds() -> None:
    """A P1 verdict on something Gmail filtered should not star it."""
    mutation = plan(thread_label_ids={"INBOX", "SPAM"})
    assert mutation.add_label_ids == ["Label_1"]
    assert "STARRED" not in mutation.add_label_ids


def test_destructive_verbs_need_opting_in() -> None:
    rules = [ActionRule(when=ActionCondition(priority=Priority.P3), do=[ActionVerb.ARCHIVE])]
    assert plan(priority=Priority.P3, rules=rules).remove_label_ids == []


def test_destructive_verbs_run_once_allowed() -> None:
    rules = [ActionRule(when=ActionCondition(priority=Priority.P3), do=[ActionVerb.ARCHIVE])]
    mutation = plan(priority=Priority.P3, rules=rules, allow_destructive=True)
    assert mutation.remove_label_ids == ["INBOX"]


def test_a_suspicious_thread_keeps_its_label_but_loses_the_privileges() -> None:
    """The classification stands. The attention-grabbing treatment — very
    possibly the objective — does not."""
    mutation = plan(thread_label_ids={"INBOX", "CATEGORY_PROMOTIONS"}, suspicious=True)
    assert mutation.add_label_ids == ["Label_1"]
    assert mutation.remove_label_ids == []
    assert mutation.suspicious is True


def test_marking_read_is_still_allowed_on_a_suspicious_thread() -> None:
    rules = [ActionRule(when=ActionCondition(priority=Priority.P3), do=[ActionVerb.MARK_READ])]
    mutation = plan(priority=Priority.P3, rules=rules, suspicious=True)
    assert mutation.remove_label_ids == ["UNREAD"]


def test_a_rule_can_target_suspicious_threads_directly() -> None:
    rules = [
        ActionRule(when=ActionCondition(suspicious=True), do=[ActionVerb.MARK_READ]),
        ActionRule(when=ActionCondition(priority=Priority.P1), do=[ActionVerb.ADD_STAR]),
    ]
    assert plan(rules=rules, suspicious=True).remove_label_ids == ["UNREAD"]


# --------------------------------------------------------------------------
# Rule selection
# --------------------------------------------------------------------------


def test_the_first_matching_rule_wins() -> None:
    """Union semantics would make a config's effective behaviour unreadable."""
    rules = [
        ActionRule(when=ActionCondition(priority=Priority.P1), do=[ActionVerb.ADD_STAR]),
        ActionRule(when=ActionCondition(priority=Priority.P1), do=[ActionVerb.MARK_IMPORTANT]),
    ]
    verbs = select_verbs(rules, priority=Priority.P1, label_ids=set(), suspicious=False)
    assert verbs == [ActionVerb.ADD_STAR]


def test_a_rule_can_name_several_priorities() -> None:
    rule = ActionRule(when=ActionCondition(priority=[Priority.P1, Priority.P2]), do=[ActionVerb.ADD_STAR])
    for priority in (Priority.P1, Priority.P2):
        assert select_verbs([rule], priority=priority, label_ids=set(), suspicious=False)
    assert not select_verbs([rule], priority=Priority.P3, label_ids=set(), suspicious=False)


def test_an_empty_unless_guard_is_rejected() -> None:
    """It would match nothing, so the rule would silently never fire."""
    with pytest.raises(ValueError, match="matches nothing"):
        ActionRule(when=ActionCondition(priority=Priority.P1), unless=ActionCondition())


def test_no_rules_means_only_the_priority_label() -> None:
    assert plan(rules=[]).add_label_ids == ["Label_1"]


# --------------------------------------------------------------------------
# The mutation as a wire contract
# --------------------------------------------------------------------------


def test_the_mutation_carries_its_own_provenance() -> None:
    """A mutation sitting in a file must still be explicable."""
    mutation = plan(backend="openai", model="test-model", prompt_version="3", confidence=0.9)
    assert (mutation.backend, mutation.model, mutation.prompt_version) == ("openai", "test-model", "3")
    assert mutation.confidence == 0.9
    assert mutation.schema_version == 1


def test_the_idempotency_key_is_stable_for_the_same_change() -> None:
    first = plan(now=datetime(2026, 1, 1, tzinfo=UTC))
    second = plan(now=datetime(2026, 6, 1, tzinfo=UTC), reason="different wording")
    assert first.idempotency_key == second.idempotency_key


def test_a_different_change_gets_a_different_key() -> None:
    assert plan().idempotency_key != plan(priority=Priority.P2).idempotency_key


def test_the_mutation_round_trips_through_json() -> None:
    original = plan()
    assert ThreadMutation.model_validate_json(original.model_dump_json()) == original


def test_message_ids_are_carried_because_batch_modify_needs_them() -> None:
    assert plan().message_ids == ["m1", "m2"]

"""Label resolution — the check that replaces the hand-maintained `labelID` map."""

import pytest

from epc.errors import ConfigError, LabelResolutionError
from epc.gmail.labels import GmailLabel, name_by_id, parse_labels, resolve_priority_labels
from epc.priority import Priority

MAILBOX = [
    GmailLabel(id="INBOX", name="INBOX", type="system"),
    GmailLabel(id="CATEGORY_PROMOTIONS", name="CATEGORY_PROMOTIONS", type="system"),
    GmailLabel(id="Label_1", name="#/P1"),
    GmailLabel(id="Label_2", name="#/P2"),
    GmailLabel(id="Label_3", name="#/P3"),
    GmailLabel(id="Label_9", name="Receipts"),
]

WANTED = {Priority.P1: "#/P1", Priority.P2: "#/P2", Priority.P3: "#/P3"}


def test_names_resolve_to_ids() -> None:
    assert resolve_priority_labels(MAILBOX, WANTED) == {
        Priority.P1: "Label_1",
        Priority.P2: "Label_2",
        Priority.P3: "Label_3",
    }


def test_a_missing_label_fails_before_anything_is_classified() -> None:
    wanted = {**WANTED, Priority.P2: "#/Nope"}
    with pytest.raises(LabelResolutionError) as exc:
        resolve_priority_labels(MAILBOX, wanted)
    assert "#/Nope" in str(exc.value)
    assert "P2" in str(exc.value)


def test_a_typo_gets_a_suggestion() -> None:
    """The whole point of resolving by name is that a typo is recoverable."""
    wanted = {**WANTED, Priority.P1: "#/p1 "}
    with pytest.raises(LabelResolutionError, match=r"did you mean.*#/P1"):
        resolve_priority_labels(MAILBOX, wanted)


def test_every_missing_label_is_reported_at_once() -> None:
    wanted = {Priority.P1: "a", Priority.P2: "b", Priority.P3: "#/P3"}
    with pytest.raises(LabelResolutionError) as exc:
        resolve_priority_labels(MAILBOX, wanted)
    message = str(exc.value)
    assert "'a'" in message
    assert "'b'" in message


def test_two_priorities_pointing_at_one_label_are_rejected() -> None:
    """Config validation catches identical *names*; this catches two names that
    happen to be the same label."""
    mailbox = [*MAILBOX, GmailLabel(id="Label_1", name="alias-of-p1")]
    wanted = {Priority.P1: "#/P1", Priority.P2: "alias-of-p1", Priority.P3: "#/P3"}
    with pytest.raises(LabelResolutionError, match="same Gmail label"):
        resolve_priority_labels(mailbox, wanted)


def test_resolution_errors_are_configuration_errors() -> None:
    """They are fatal at startup, not a per-thread failure."""
    assert issubclass(LabelResolutionError, ConfigError)


def test_label_names_are_matched_exactly() -> None:
    """Gmail label names are what the user typed; guessing at case would make
    which label gets written depend on a heuristic."""
    with pytest.raises(LabelResolutionError):
        resolve_priority_labels(MAILBOX, {**WANTED, Priority.P1: "#/p1"})


def test_name_by_id_covers_user_labels_only() -> None:
    assert name_by_id(MAILBOX) == {
        "Label_1": "#/P1",
        "Label_2": "#/P2",
        "Label_3": "#/P3",
        "Label_9": "Receipts",
    }


def test_system_labels_are_not_user_labels() -> None:
    assert GmailLabel(id="CATEGORY_PROMOTIONS", name="x").is_user_label is False
    assert GmailLabel(id="Label_7", name="x").is_user_label is True


def test_a_labels_list_response_parses() -> None:
    parsed = parse_labels(
        [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_5", "name": "Work"},
            {"id": "Label_6"},
        ]
    )
    assert [label.name for label in parsed] == ["INBOX", "Work", ""]
    assert parsed[1].type == "user"

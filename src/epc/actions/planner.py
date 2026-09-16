"""Turning a classification into a :class:`ThreadMutation`.

Two rules from the design hold here, and both are load-bearing:

**The model chose a priority, not an action.** Everything below reads the
priority and the user's configuration. Nothing reads free text the model
produced. That is why a successful injection tops out at one mislabelled thread.

**Only newly classified threads get planned.** The pipeline never sends an
already-labelled thread here. Once label *removal* exists, re-planning a thread
the user has since corrected by hand would undo that correction on every run —
quietly, and every single time.
"""

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from epc.actions.model import (
    DESTRUCTIVE_VERBS,
    LABEL_IMPORTANT,
    LABEL_INBOX,
    LABEL_STARRED,
    LABEL_UNREAD,
    ActionVerb,
    ThreadMutation,
    labels_to_leave_non_primary,
)
from epc.actions.rules import ActionRule, select_verbs
from epc.priority import Priority

# Verbs withheld from a thread whose content tried to address the model. It
# still gets its priority label — the classification stands — but it does not
# get the attention-grabbing treatment that was very possibly the objective.
HIGH_PRIVILEGE_VERBS = frozenset({ActionVerb.ADD_STAR, ActionVerb.MOVE_TO_PRIMARY, ActionVerb.MARK_IMPORTANT})


class PlannedActions:
    """Label changes accumulated from a set of verbs."""

    def __init__(self) -> None:
        self.add: set[str] = set()
        self.remove: set[str] = set()

    def apply(self, verb: ActionVerb, *, thread_label_ids: set[str], move_targets: set[str]) -> None:
        match verb:
            case ActionVerb.ADD_STAR:
                self.add.add(LABEL_STARRED)
            case ActionVerb.REMOVE_STAR:
                self.remove.add(LABEL_STARRED)
            case ActionVerb.MARK_IMPORTANT:
                self.add.add(LABEL_IMPORTANT)
            case ActionVerb.MARK_READ:
                self.remove.add(LABEL_UNREAD)
            case ActionVerb.ARCHIVE:
                self.remove.add(LABEL_INBOX)
            case ActionVerb.MOVE_TO_PRIMARY:
                self.remove.update(labels_to_leave_non_primary(thread_label_ids, move_targets))


def plan_mutation(
    *,
    thread_id: str,
    message_ids: Sequence[str],
    thread_label_ids: set[str],
    priority: Priority,
    priority_label_ids: dict[Priority, str],
    rules: Sequence[ActionRule],
    move_targets: set[str],
    suspicious: bool = False,
    withhold_privileged_when_suspicious: bool = True,
    allow_destructive: bool = False,
    confidence: float = 0.0,
    reason: str = "",
    signals: Sequence[str] = (),
    backend: str = "",
    model: str = "",
    prompt_version: str = "",
    now: datetime | None = None,
) -> ThreadMutation:
    """Build the mutation for one classified thread.

    `suspicious` is recorded on the mutation and visible to rules either way.
    Whether it also withholds the high-privilege verbs is a separate choice —
    `security.on_suspected_injection: flag` records without withholding — and
    defaults to withholding, so a caller that forgets to decide gets the safe
    one.
    """
    actions = PlannedActions()
    # The priority label is not a rule — it is the point of the tool.
    actions.add.add(priority_label_ids[priority])

    verbs = select_verbs(list(rules), priority=priority, label_ids=thread_label_ids, suspicious=suspicious)
    for verb in verbs:
        if verb in DESTRUCTIVE_VERBS and not allow_destructive:
            continue
        if suspicious and withhold_privileged_when_suspicious and verb in HIGH_PRIVILEGE_VERBS:
            continue
        actions.apply(verb, thread_label_ids=thread_label_ids, move_targets=move_targets)

    # Asking Gmail to add and remove the same label is a contradiction the
    # config can express; removal wins, because it is the explicit instruction.
    add = sorted(actions.add - actions.remove)
    remove = sorted(actions.remove)

    # Nothing is gained by asking Gmail to add a label a thread already has.
    add = [label for label in add if label not in thread_label_ids]
    remove = [label for label in remove if label in thread_label_ids]

    mutation = ThreadMutation(
        thread_id=thread_id,
        message_ids=list(message_ids),
        add_label_ids=add,
        remove_label_ids=remove,
        priority=priority,
        confidence=confidence,
        reason=reason,
        signals=list(signals),
        classified_at=now or datetime.now(UTC),
        backend=backend,
        model=model,
        prompt_version=prompt_version,
        suspicious=suspicious,
    )
    mutation.idempotency_key = idempotency_key(mutation)
    return mutation


def plan_carry_forward(
    *,
    thread_id: str,
    message_label_ids: Mapping[str, set[str]],
    priority_label_ids: dict[Priority, str],
    now: datetime | None = None,
) -> ThreadMutation | None:
    """Put a labelled thread's priority label on the messages that lack it.

    Labels belong to messages, not threads, and a reply arriving in a labelled
    thread does not inherit them. Gmail search matches messages, so without
    this the thread is listed again on every run, fetched, and skipped — and
    with no time limit on the query, such threads only ever accumulate.

    Rule B is intact. The thread is not classified and not planned: no priority
    is decided and no action runs. The label it already carries, whether this
    tool or a person put it there, is extended to its newer messages and
    nothing else changes. With more than one priority label on the thread there
    is no single answer to extend, so nothing is done.
    """
    priority_by_label = {label_id: priority for priority, label_id in priority_label_ids.items()}
    present = {label for labels in message_label_ids.values() for label in labels} & set(priority_by_label)
    if len(present) != 1:
        return None

    (label_id,) = present
    missing = [message_id for message_id, labels in message_label_ids.items() if label_id not in labels]
    if not missing:
        return None

    mutation = ThreadMutation(
        thread_id=thread_id,
        message_ids=missing,
        add_label_ids=[label_id],
        priority=priority_by_label[label_id],
        classified_at=now or datetime.now(UTC),
        origin="carried_forward",
    )
    mutation.idempotency_key = idempotency_key(mutation)
    return mutation


def idempotency_key(mutation: ThreadMutation) -> str:
    """A stable digest of thread plus the exact change requested.

    Deliberately excludes the timestamp and the reason: replaying the same
    change should look like the same change, while a *different* change to the
    same thread must not be mistaken for a duplicate.

    A carried-forward label names its messages too. Otherwise it can share a key
    with the classification that labelled the thread minutes earlier, and a
    FIFO queue would drop it as a duplicate of a change it is not.
    """
    add, remove = mutation.label_signature
    parts = [mutation.thread_id, ",".join(add), ",".join(remove)]
    if mutation.origin != "classified":
        parts += [mutation.origin, ",".join(sorted(mutation.message_ids))]
    material = "|".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

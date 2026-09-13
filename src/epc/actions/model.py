"""The action vocabulary, and the mutation that crosses process boundaries.

:class:`ThreadMutation` is a **wire contract**, not an internal struct. Once a
mutation can be written to a file or put on a queue and applied by a different
process — possibly a different deployment, possibly minutes later — its shape is
an API. It is versioned and pinned by a test for that reason.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from epc.gmail.models import NON_PRIMARY_CATEGORIES
from epc.priority import Priority

SCHEMA_VERSION = 1

# Gmail system labels the verbs below manipulate.
LABEL_STARRED = "STARRED"
LABEL_IMPORTANT = "IMPORTANT"
LABEL_UNREAD = "UNREAD"
LABEL_INBOX = "INBOX"


class ActionVerb(StrEnum):
    """What may be done to a thread once its priority is known.

    Parameterless on purpose. A verb that takes an argument — "add the label
    named X" — needs that name resolved to an ID at plan time and validated
    against the mailbox, which is plumbing without a use case yet. When one
    turns up, `add_label` / `remove_label` join this enum along with the
    resolution step; they are not silently missing.
    """

    ADD_STAR = "add_star"
    REMOVE_STAR = "remove_star"
    MOVE_TO_PRIMARY = "move_to_primary"
    MARK_IMPORTANT = "mark_important"
    MARK_READ = "mark_read"
    ARCHIVE = "archive"


DESTRUCTIVE_VERBS = frozenset({ActionVerb.ARCHIVE})
"""Verbs that take a thread out of the inbox. Opt-in, never a default."""


class ThreadMutation(BaseModel):
    """One thread's worth of changes, ready to apply.

    Carries its own provenance so that a mutation sitting in a queue is still
    explicable: which model decided it, under which prompt, and when.
    """

    schema_version: int = SCHEMA_VERSION

    thread_id: str
    # Every message in the thread. `messages.batchModify` operates on message
    # IDs, and labelling every message of a thread is what labels the thread.
    message_ids: list[str] = Field(default_factory=list)

    add_label_ids: list[str] = Field(default_factory=list)
    remove_label_ids: list[str] = Field(default_factory=list)

    priority: Priority
    confidence: float = 0.0
    reason: str = ""
    signals: list[str] = Field(default_factory=list)

    classified_at: datetime
    backend: str = ""
    model: str = ""
    prompt_version: str = ""

    # Thread plus the exact change requested. Two identical mutations dedupe;
    # a different change to the same thread does not.
    idempotency_key: str = ""

    # Recorded rather than acted on here: the planner has already withheld the
    # high-privilege verbs, and the applier has no business re-deciding.
    suspicious: bool = False

    @property
    def is_noop(self) -> bool:
        return not self.add_label_ids and not self.remove_label_ids

    @property
    def label_signature(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Groups mutations that can share one `batchModify` call."""
        return tuple(sorted(self.add_label_ids)), tuple(sorted(self.remove_label_ids))


def labels_to_leave_non_primary(thread_label_ids: set[str], targets: set[str]) -> list[str]:
    """Category labels to remove so a thread lands in Primary.

    Removing the non-primary category is what moves a thread; Gmail treats the
    absence of one as Primary. Naturally idempotent — once gone, later runs find
    nothing to do.
    """
    return sorted(thread_label_ids & NON_PRIMARY_CATEGORIES & targets)

"""Declarative rules: when a thread looks like this, do that.

The rules live in configuration rather than in the prompt because they are the
half of the decision that must not be negotiable. The model says how urgent a
thread is; what happens as a result is arithmetic over the user's own settings.
An email that talks its way to P1 still cannot talk its way into being starred
if the configuration does not say so.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from epc.actions.model import ActionVerb
from epc.priority import Priority


class ActionCondition(BaseModel):
    """What a thread has to look like. All stated fields must match."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    priority: Priority | list[Priority] | None = None
    # Raw Gmail label IDs — SPAM, TRASH, CATEGORY_PROMOTIONS, Label_123.
    any_label: list[str] = Field(default_factory=list)
    # Whether the thread tripped the injection heuristics.
    suspicious: bool | None = None

    @property
    def is_empty(self) -> bool:
        return self.priority is None and not self.any_label and self.suspicious is None

    def matches(self, *, priority: Priority, label_ids: set[str], suspicious: bool) -> bool:
        if self.priority is not None:
            wanted = self.priority if isinstance(self.priority, list) else [self.priority]
            if priority not in wanted:
                return False
        if self.any_label and not (set(self.any_label) & label_ids):
            return False
        return not (self.suspicious is not None and self.suspicious != suspicious)


class ActionRule(BaseModel):
    """One rule. The first matching rule wins; later rules are not consulted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    when: ActionCondition
    unless: ActionCondition | None = None
    do: list[ActionVerb] = Field(default_factory=list)

    @model_validator(mode="after")
    def _guards_must_guard_something(self) -> Self:
        if self.unless is not None and self.unless.is_empty:
            raise ValueError("an 'unless' guard with no conditions matches nothing; remove it")
        return self

    def applies_to(self, *, priority: Priority, label_ids: set[str], suspicious: bool) -> bool:
        if not self.when.matches(priority=priority, label_ids=label_ids, suspicious=suspicious):
            return False
        return self.unless is None or not self.unless.matches(
            priority=priority, label_ids=label_ids, suspicious=suspicious
        )


def select_verbs(
    rules: list[ActionRule],
    *,
    priority: Priority,
    label_ids: set[str],
    suspicious: bool,
) -> list[ActionVerb]:
    """Verbs from the first matching rule.

    First match rather than union: overlapping rules that each contribute a verb
    make the effective behaviour of a config impossible to read off the page.
    """
    for rule in rules:
        if rule.applies_to(priority=priority, label_ids=label_ids, suspicious=suspicious):
            return list(dict.fromkeys(rule.do))
    return []

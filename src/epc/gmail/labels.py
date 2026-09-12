"""Resolve configured label names to the internal IDs Gmail writes with.

Gmail's search syntax takes display names while `messages.modify` takes
`Label_...` IDs, which is why the old config carried both and asked the user to
keep them in agreement by hand. Nothing checked that they did, and a mismatch
mislabelled every thread from then on without a single error.

The mailbox already knows the mapping, and the tool already has to list labels,
so resolution happens at startup from names alone. A name that does not exist is
a configuration error raised **before** any thread is classified, rather than a
failure surfacing after 1500 LLM calls have been paid for.
"""

import difflib
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, Field

from epc.errors import LabelResolutionError
from epc.priority import Priority

# User-created labels carry this prefix; system labels (INBOX, CATEGORY_*, …)
# have stable, human-readable IDs instead.
USER_LABEL_ID_PREFIX = "Label_"


class GmailLabel(BaseModel):
    """One entry from `users.labels.list`."""

    id: str
    name: str
    type: str = "user"

    @property
    def is_user_label(self) -> bool:
        return self.id.startswith(USER_LABEL_ID_PREFIX)


def parse_labels(raw: Iterable[Mapping[str, object]]) -> list[GmailLabel]:
    """Parse the `labels` array of a `users.labels.list` response."""
    return [
        GmailLabel(
            id=str(entry.get("id") or ""),
            name=str(entry.get("name") or ""),
            type=str(entry.get("type") or "user"),
        )
        for entry in raw
    ]


def name_by_id(labels: Iterable[GmailLabel]) -> dict[str, str]:
    """User label ID to display name, for rendering a dry run legibly."""
    return {label.id: label.name for label in labels if label.is_user_label}


def resolve_priority_labels(
    labels: Iterable[GmailLabel],
    wanted: Mapping[Priority, str],
) -> dict[Priority, str]:
    """Map each priority to the label ID its configured name refers to.

    Raises:
        LabelResolutionError: if any configured name is absent from the mailbox,
            or if two priorities resolve to the same label.
    """
    available = list(labels)
    by_name = {label.name: label.id for label in available}

    missing = {priority: name for priority, name in wanted.items() if name not in by_name}
    if missing:
        raise LabelResolutionError(_missing_label_message(missing, by_name))

    resolved = {priority: by_name[name] for priority, name in wanted.items()}

    duplicates = {label_id for label_id in resolved.values() if list(resolved.values()).count(label_id) > 1}
    if duplicates:
        collisions = sorted(p.value for p, lid in resolved.items() if lid in duplicates)
        raise LabelResolutionError(
            f"priorities {collisions} resolve to the same Gmail label; "
            "each priority needs its own label or they will overwrite one another"
        )

    return resolved


def _missing_label_message(missing: Mapping[Priority, str], by_name: Mapping[str, str]) -> str:
    """An error a person can act on without going to look anything up."""
    lines = ["Gmail label(s) named in the config do not exist in this mailbox:"]
    for priority, name in sorted(missing.items()):
        suggestions = difflib.get_close_matches(name, by_name.keys(), n=3, cutoff=0.6)
        hint = f"  did you mean: {', '.join(repr(s) for s in suggestions)}" if suggestions else ""
        lines.append(f"  {priority.value}: {name!r} not found.{hint}")
    lines.append("")
    lines.append("Create the labels in Gmail, correct labels.* in the config, or set labels.create_if_missing.")
    return "\n".join(lines)


class ResolvedLabels(BaseModel):
    """Priority label IDs, plus the mailbox's full user-label map."""

    by_priority: dict[Priority, str]
    user_label_names: dict[str, str] = Field(default_factory=dict)

    @property
    def ids(self) -> set[str]:
        return set(self.by_priority.values())

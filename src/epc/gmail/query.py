"""Building the Gmail search query.

Label names are quoted. The previous implementation interpolated them raw, so a
label containing a space produced a query that silently matched something else —
and the failure mode was re-classifying threads that had already been done,
which costs money and looks like nothing at all.
"""

from collections.abc import Iterable

# Characters that end a bare Gmail search token.
_NEEDS_NO_QUOTES = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./#")


def quote_label(name: str) -> str:
    """Quote a label name for use in a search term."""
    if name and all(character in _NEEDS_NO_QUOTES for character in name):
        return name
    escaped = name.replace('"', '\\"')
    return f'"{escaped}"'


def build_search_query(*, base: str, extra: str = "", exclude_labels: Iterable[str] = ()) -> str:
    """The query that finds threads still needing a priority.

    Excluding already-labelled threads in the query is the first half of
    idempotency; the pipeline re-checks after fetching, because a label can
    appear between the two.
    """
    parts = [base.strip()]
    if extra.strip():
        parts.append(extra.strip())
    parts.extend(f"-label:{quote_label(name)}" for name in exclude_labels if name)
    return " ".join(part for part in parts if part)

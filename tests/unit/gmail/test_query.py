"""Search query construction."""

from epc.gmail.query import build_search_query, quote_label


def test_a_simple_label_needs_no_quotes() -> None:
    assert quote_label("#/P1") == "#/P1"


def test_a_label_with_a_space_is_quoted() -> None:
    """Interpolating this raw produced a query that matched something else, and
    the symptom was paying to re-classify threads that were already done."""
    assert quote_label("Needs Reply") == '"Needs Reply"'


def test_an_embedded_quote_is_escaped() -> None:
    assert quote_label('say "hi"') == '"say \\"hi\\""'


def test_non_ascii_labels_are_quoted() -> None:
    assert quote_label("重要") == '"重要"'


def test_priority_labels_are_excluded() -> None:
    query = build_search_query(base="in:inbox", exclude_labels=["#/P1", "#/P2"])
    assert query == "in:inbox -label:#/P1 -label:#/P2"


def test_the_extra_query_is_included() -> None:
    query = build_search_query(base="in:inbox", extra="newer_than:14d", exclude_labels=["#/P1"])
    assert query == "in:inbox newer_than:14d -label:#/P1"


def test_empty_parts_are_dropped() -> None:
    assert build_search_query(base="in:inbox", extra="   ") == "in:inbox"


def test_empty_label_names_are_ignored() -> None:
    assert build_search_query(base="in:inbox", exclude_labels=["", "#/P1"]) == "in:inbox -label:#/P1"

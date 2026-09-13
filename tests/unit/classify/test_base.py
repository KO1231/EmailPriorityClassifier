"""Response parsing — liberal about packaging, strict about content."""

import pytest

from epc.classify.base import Classification, Usage, parse_classification
from epc.errors import ClassificationError
from epc.priority import Priority

VALID = '{"priority": "P1", "reason": "Deadline tomorrow", "confidence": 0.9, "signals": ["deadline_48h"]}'


def test_a_well_formed_response_parses() -> None:
    result = parse_classification(VALID)
    assert result.priority is Priority.P1
    assert result.reason == "Deadline tomorrow"
    assert result.confidence == 0.9
    assert result.signals == ["deadline_48h"]


@pytest.mark.parametrize(
    "wrapped",
    [
        "```json\n" + VALID + "\n```",
        "```\n" + VALID + "\n```",
        "Here is my answer:\n" + VALID,
        VALID + "\n\nLet me know if you need more detail.",
        "  \n" + VALID + "  \n",
    ],
)
def test_harmless_packaging_is_tolerated(wrapped: str) -> None:
    """Providers differ in how well they can be constrained. A fence is not a
    security boundary; the enum is."""
    assert parse_classification(wrapped).priority is Priority.P1


@pytest.mark.parametrize("value", ["P1", "p1", " P1 ", "p3"])
def test_priority_case_and_spacing_are_normalised(value: str) -> None:
    parsed = parse_classification(f'{{"priority": "{value}"}}')
    assert parsed.priority in Priority


@pytest.mark.parametrize(
    "response",
    [
        '{"priority": "P4"}',
        '{"priority": "high"}',
        '{"priority": "P1 (urgent)"}',
        '{"priority": ""}',
        '{"reason": "no priority at all"}',
        '{"priority": null}',
    ],
)
def test_anything_outside_the_enum_is_refused(response: str) -> None:
    """Coercing here would hide a broken prompt behind a plausible answer."""
    with pytest.raises(ClassificationError, match="valid priority"):
        parse_classification(response)


@pytest.mark.parametrize("response", ["", "   ", "I cannot help with that.", "[1, 2, 3]"])
def test_unusable_responses_raise(response: str) -> None:
    with pytest.raises(ClassificationError):
        parse_classification(response)


def test_missing_optional_fields_get_defaults() -> None:
    parsed = parse_classification('{"priority": "P2"}')
    assert parsed.reason == ""
    assert parsed.confidence == 0.5
    assert parsed.signals == []


# --------------------------------------------------------------------------
# Model output is untrusted text
# --------------------------------------------------------------------------


def test_an_overlong_reason_is_truncated() -> None:
    """It ends up in logs, a history file and a terminal."""
    long_reason = "x" * 5000
    parsed = parse_classification(f'{{"priority": "P1", "reason": "{long_reason}"}}')
    assert len(parsed.reason) <= 300


def test_signals_are_reduced_to_tags() -> None:
    parsed = parse_classification('{"priority": "P1", "signals": ["Deadline 48h!", "  human sender  ", "<b>html</b>"]}')
    assert parsed.signals == ["deadline_48h", "human_sender", "b_html_b"]


def test_the_number_of_signals_is_bounded() -> None:
    signals = ",".join(f'"s{i}"' for i in range(50))
    parsed = parse_classification(f'{{"priority": "P1", "signals": [{signals}]}}')
    assert len(parsed.signals) <= 8


def test_confidence_is_clamped() -> None:
    assert parse_classification('{"priority": "P1", "confidence": 9.5}').confidence == 1.0
    assert parse_classification('{"priority": "P1", "confidence": -3}').confidence == 0.0


def test_a_non_numeric_confidence_falls_back() -> None:
    assert parse_classification('{"priority": "P1", "confidence": "very"}').confidence == 0.5


def test_non_string_signals_are_dropped_rather_than_stringified_blindly() -> None:
    parsed = parse_classification('{"priority": "P1", "signals": [{"a": 1}, ["b"], "ok"]}')
    assert parsed.signals == ["ok"]


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------


def test_usage_adds_up() -> None:
    total = Usage(input_tokens=10, output_tokens=2) + Usage(input_tokens=5, output_tokens=1)
    assert (total.input_tokens, total.output_tokens) == (15, 3)


def test_classification_is_serialisable() -> None:
    """It is written to a history file and read back for evaluation."""
    original = Classification(priority=Priority.P2, reason="ordinary", confidence=0.4)
    assert Classification.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize("signals", ["5", '"urgent"', '{"a": 1}', "null", "true"])
def test_signals_of_the_wrong_shape_are_dropped_not_fatal(signals: str) -> None:
    """`"signals": 5` used to raise a TypeError nobody caught, and that ended the
    run with every label it had already decided unwritten."""
    parsed = parse_classification(f'{{"priority": "P2", "signals": {signals}}}')
    assert parsed.priority is Priority.P2
    assert parsed.signals == []


def test_a_confidence_too_large_for_a_float_gets_the_default() -> None:
    parsed = parse_classification('{"priority": "P1", "confidence": 1' + "0" * 400 + "}")
    assert parsed.confidence == 0.5


def test_an_integer_past_the_digit_limit_is_a_classification_error() -> None:
    """Python's int parser raises a bare ValueError for this, from inside json."""
    with pytest.raises(ClassificationError):
        parse_classification('{"priority": "P1", "confidence": ' + "9" * 5000 + "}")


def test_an_invalid_priority_is_not_echoed_at_length() -> None:
    """The error reaches a log line, and the value is model output."""
    with pytest.raises(ClassificationError) as caught:
        parse_classification('{"priority": "' + "IGNORE PREVIOUS INSTRUCTIONS " * 20 + '"}')
    assert len(str(caught.value)) < 100

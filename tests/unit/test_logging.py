"""Redaction. The backstop, not the policy."""

from epc.logging import MAX_VALUE_CHARS, REDACTION, redact


def call(**event: object) -> dict[str, object]:
    return dict(redact(None, "info", dict(event)))


def test_content_bearing_keys_are_replaced() -> None:
    """The previous implementation wrote whole email bodies to a file at DEBUG."""
    result = call(body="Confidential merger details", thread_id="t1")
    assert result["body"] == REDACTION
    assert result["thread_id"] == "t1"


def test_addresses_and_subjects_are_replaced() -> None:
    result = call(sender="a@example.com", subject="Re: salary", to="b@example.com")
    assert all(result[key] == REDACTION for key in ("sender", "subject", "to"))


def test_credentials_are_replaced() -> None:
    result = call(api_key="sk-real-key", token="ya29.real", credentials={"refresh": "x"})
    assert all(result[key] == REDACTION for key in ("api_key", "token", "credentials"))


def test_the_key_name_is_matched_case_insensitively() -> None:
    assert call(Body="secret")["Body"] == REDACTION


def test_a_long_value_under_an_allowed_key_is_still_capped() -> None:
    """Even a loggable key can carry more than a log line should."""
    result = call(query="x" * 5000)
    assert isinstance(result["query"], str)
    assert len(result["query"]) <= MAX_VALUE_CHARS + 1


def test_ordinary_facts_pass_through() -> None:
    result = call(thread_id="t1", count=42, priority="P1", elapsed=1.5)
    assert result == {"thread_id": "t1", "count": 42, "priority": "P1", "elapsed": 1.5}

"""Redaction. The backstop, not the policy."""

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from epc.logging import MAX_VALUE_CHARS, REDACTION, configure_logging, get_logger, redact


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


# --------------------------------------------------------------------------
# Every line the process writes, not only ours
# --------------------------------------------------------------------------


@pytest.fixture
def restored_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    structlog.reset_defaults()


def last_json_line(err: str) -> dict[str, object]:
    return dict(json.loads(err.strip().splitlines()[-1]))


def test_a_library_record_is_rendered_and_capped_like_our_own(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """The Google and AWS clients log through the standard library. Those lines
    used to bypass both the JSON renderer and redaction."""
    configure_logging(json_output=True)
    logging.getLogger("googleapiclient.discovery").warning("request body: %s", "x" * 5000)

    line = last_json_line(capsys.readouterr().err)
    assert line["level"] == "warning"
    assert isinstance(line["event"], str)
    assert len(line["event"]) <= MAX_VALUE_CHARS + 1


def test_our_own_events_are_still_redacted(capsys: pytest.CaptureFixture[str], restored_logging: None) -> None:
    configure_logging(json_output=True)
    get_logger("epc.test").warning("classification failed", thread_id="t1", body="Confidential merger details")

    line = last_json_line(capsys.readouterr().err)
    assert line["event"] == "classification failed"
    assert line["thread_id"] == "t1"
    assert line["body"] == REDACTION

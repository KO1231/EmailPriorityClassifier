"""Structured logging, with redaction as a backstop rather than a policy.

The previous implementation wrote whole request and response payloads — that is,
complete email bodies — into a file on disk at DEBUG. Nothing was wrong with the
logging library; the content simply went in.

So there are two defences here and they are not the same defence:

* **Nothing is asked to log a body.** Call sites pass IDs, counts and enums.
* **A processor removes one anyway.** Keys that name content are replaced and
  every string is capped, so a future call site that gets it wrong leaks a
  truncated line instead of a mailbox.

Human-readable output locally, JSON when something is going to parse it — and
the same for records from the standard `logging` module. The Google and AWS
client libraries log that way, and they log request details; routing their
records through the same processors means redaction and JSON apply to every
line the process writes, not only to the lines this package writes.
"""

import logging
import logging.handlers
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog

# Keys that name message content rather than a fact about it. A value under one
# of these is replaced outright, whatever it holds.
REDACTED_KEYS = frozenset(
    {
        "body",
        "content",
        "payload",
        "text",
        "message",
        "snippet",
        "raw",
        "subject",
        "prompt",
        "system",
        "user",
        "email",
        "sender",
        "to",
        "cc",
        "token",
        "api_key",
        "credentials",
    }
)
REDACTION = "<redacted>"

# Even a key that is meant to be loggable can carry more than a log line should.
MAX_VALUE_CHARS = 200

# Client libraries that log every request at INFO. The lines carry a URL and a
# status, nothing of the mail, but a scheduled run of 1500 threads would write
# 1500 of them. Held at WARNING unless DEBUG is asked for.
QUIET_LIBRARIES = ("httpx", "httpx2", "httpcore", "openai", "googleapiclient", "urllib3", "botocore", "boto3")

LOG_FILE_BYTES = 5 * 2**20
LOG_FILE_BACKUPS = 5


def redact(_logger: Any, _method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Drop content-bearing values and cap the rest."""
    for key, value in list(event.items()):
        if key.lower() in REDACTED_KEYS:
            event[key] = REDACTION
        elif isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            event[key] = value[:MAX_VALUE_CHARS] + "…"
    return event


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = False,
    log_file: Path | None = None,
) -> None:
    """Set up logging for a run. Safe to call more than once."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    # One formatter for both kinds of record. `foreign_pre_chain` is what a
    # standard-library record goes through; a structlog event has been through
    # `shared` already.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        # `parents=True`: the old implementation could only create one level and
        # fell over on a nested path.
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=numeric_level, handlers=handlers, force=True)
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(numeric_level if numeric_level <= logging.DEBUG else logging.WARNING)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """A bound logger. Pass facts — IDs, counts, enums — never content."""
    return structlog.get_logger(name)

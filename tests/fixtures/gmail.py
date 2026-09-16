"""Builders that produce Gmail `format=full` JSON.

Every fixture in this suite is generated rather than captured. The remote is
public, so no real mail may enter the repository — and generating them is what
makes hostile shapes (nested multipart, legacy Japanese charsets, HTML-only
promotional mail) cheap to author in the first place.

Two levels are available:

* :func:`part`, :func:`text_part`, :func:`multipart` and friends build a payload
  tree directly, for tests that need exact control over charsets and headers;
* :func:`from_std_message` converts a :mod:`email` message, for tests that want
  a realistic structure without hand-assembling it.
"""

import base64
from collections.abc import Sequence
from email.message import Message
from typing import Any

GmailPayload = dict[str, Any]

_DEFAULT_INTERNAL_DATE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z


def _b64(data: bytes) -> str:
    """base64url with the padding stripped, exactly as Gmail returns body data."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def part(
    mime_type: str,
    *,
    content: bytes | None = None,
    charset: str | None = None,
    filename: str = "",
    headers: Sequence[tuple[str, str]] = (),
    parts: Sequence[GmailPayload] = (),
) -> GmailPayload:
    """One node of a payload tree."""
    all_headers = list(headers)
    if not any(name.lower() == "content-type" for name, _ in all_headers):
        content_type = f"{mime_type}; charset={charset}" if charset else mime_type
        all_headers.insert(0, ("Content-Type", content_type))

    node: GmailPayload = {
        "partId": "",
        "mimeType": mime_type,
        "filename": filename,
        "headers": [{"name": name, "value": value} for name, value in all_headers],
    }
    if parts:
        node["body"] = {"size": 0}
        node["parts"] = list(parts)
    else:
        payload = content or b""
        node["body"] = {"size": len(payload), "data": _b64(payload)}
    return node


def text_part(
    text: str,
    *,
    subtype: str = "plain",
    charset: str = "utf-8",
    headers: Sequence[tuple[str, str]] = (),
) -> GmailPayload:
    """A text part encoded in `charset` — the charset it also declares."""
    return part(f"text/{subtype}", content=text.encode(charset), charset=charset, headers=headers)


def mislabelled_text_part(text: str, *, encoded_as: str, declared_as: str) -> GmailPayload:
    """A part whose bytes do not match the charset it claims."""
    return part("text/plain", content=text.encode(encoded_as), charset=declared_as)


def attachment_part(
    filename: str,
    *,
    mime_type: str = "application/pdf",
    size: int = 2048,
) -> GmailPayload:
    """An attachment. Gmail returns an `attachmentId` here, never the bytes."""
    return {
        "partId": "",
        "mimeType": mime_type,
        "filename": filename,
        "headers": [
            {"name": "Content-Type", "value": f'{mime_type}; name="{filename}"'},
            {"name": "Content-Disposition", "value": f'attachment; filename="{filename}"'},
        ],
        "body": {"attachmentId": "att-1", "size": size},
    }


def multipart(subtype: str, *children: GmailPayload) -> GmailPayload:
    """A `multipart/<subtype>` container."""
    return part(f"multipart/{subtype}", parts=children)


def message(
    payload: GmailPayload,
    *,
    message_id: str = "msg-1",
    thread_id: str = "thr-1",
    label_ids: Sequence[str] = ("INBOX", "UNREAD"),
    internal_date_ms: int = _DEFAULT_INTERNAL_DATE_MS,
    size_estimate: int = 4096,
    headers: Sequence[tuple[str, str]] = (),
) -> GmailPayload:
    """A `users.messages` resource wrapping `payload`.

    `headers` are message-level headers (From, Subject, List-Id …); Gmail puts
    them on the root payload node alongside its Content-Type.
    """
    root = dict(payload)
    root["headers"] = [{"name": name, "value": value} for name, value in headers] + list(root.get("headers") or [])
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(label_ids),
        "sizeEstimate": size_estimate,
        "internalDate": str(internal_date_ms),
        "payload": root,
    }


def thread(*messages: GmailPayload, thread_id: str = "thr-1") -> GmailPayload:
    """A `users.threads` resource. Gmail returns messages oldest first."""
    return {"id": thread_id, "messages": list(messages)}


def from_std_message(msg: Message) -> GmailPayload:
    """Convert a :mod:`email` message into Gmail's payload shape.

    Gmail has already undone Content-Transfer-Encoding by the time it hands the
    payload over, so the part bytes here are the decoded ones.
    """
    node: GmailPayload = {
        "partId": "",
        "mimeType": msg.get_content_type(),
        "filename": msg.get_filename() or "",
        "headers": [{"name": name, "value": value} for name, value in msg.items()],
    }
    if msg.is_multipart():
        children = msg.get_payload()
        assert isinstance(children, list)
        node["body"] = {"size": 0}
        node["parts"] = [from_std_message(child) for child in children if isinstance(child, Message)]
    else:
        content = msg.get_payload(decode=True) or b""
        assert isinstance(content, bytes)
        node["body"] = {"size": len(content), "data": _b64(content)}
    return node

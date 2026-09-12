"""Turn Gmail's `format=full` JSON into an :class:`EmailMessage`.

`format=full` is used rather than `format=raw` because it omits attachment
bytes, and at 1500 threads a run that downloads every PDF is not viable.
The cost is that Gmail hands back a half-parsed MIME tree that this module has
to finish parsing properly:

* the tree is walked **recursively** — the body of a
  ``multipart/mixed > multipart/alternative > text/plain`` message is two levels
  down, and looking only at the top level's direct children finds nothing;
* part bytes are decoded using the **charset its own headers declare**, so
  ISO-2022-JP and Shift_JIS mail survives instead of raising part-way through;
* HTML is decoded **first and parsed second**, which is the only order in which
  tags actually come off.

Neutralising hostile content — zero-width characters, bidi overrides, text
hidden with CSS — is deliberately *not* done here. This module extracts
faithfully; `epc.security.sanitize` is what makes the result safe to show a
model.
"""

import base64
import binascii
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from typing import Any

from bs4 import BeautifulSoup, Comment

from epc.gmail.models import Attachment, AuthenticationResults, EmailMessage, EmailThread

# A node of Gmail's payload tree, or a whole message/thread resource.
GmailPayload = dict[str, Any]

# Body text is taken from the first of these that yields anything.
_TEXT_PREFERENCE = ("text/plain", "text/html")

# Markup whose text content is never shown to a reader. `get_text()` keeps the
# contents of these, so they have to be removed explicitly.
_NON_VISIBLE_TAGS = ("script", "style", "head", "noscript", "template", "title")

_CHARSET_RE = re.compile(r'charset\s*=\s*["\']?([\w\-.:+]+)', re.IGNORECASE)
_AUTH_RESULT_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*(\w+)", re.IGNORECASE)
_HORIZONTAL_WS = re.compile(r"[^\S\n]+")
_BLANK_LINE_RUN = re.compile(r"\n{3,}")


# --------------------------------------------------------------------------
# tree walking
# --------------------------------------------------------------------------


def walk_parts(payload: GmailPayload) -> Iterator[GmailPayload]:
    """Yield every node of the payload tree, depth first, the root included."""
    yield payload
    for part in payload.get("parts") or []:
        yield from walk_parts(part)


def header_map(part: GmailPayload) -> dict[str, str]:
    """Case-folded header name to raw value; the first occurrence wins.

    Values are returned undecoded. RFC 2047 decoding is applied at the call
    sites that want display text, because running it over a structured header
    such as ``Content-Type`` would corrupt it.
    """
    headers: dict[str, str] = {}
    for header in part.get("headers") or []:
        name = str(header.get("name") or "").lower()
        if name and name not in headers:
            headers[name] = str(header.get("value") or "")
    return headers


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


def decode_header_value(raw: str) -> str:
    """Decode an RFC 2047 encoded-word header, e.g. ``=?ISO-2022-JP?B?...?=``."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except UnicodeDecodeError, LookupError, ValueError:
        return raw


def charset_of(headers: dict[str, str]) -> str:
    """The charset a part declares, defaulting to UTF-8 when it declares none."""
    match = _CHARSET_RE.search(headers.get("content-type", ""))
    return match.group(1) if match else "utf-8"


def decode_body_data(data: str, charset: str) -> str:
    """base64url text to a string.

    Gmail has already undone ``Content-Transfer-Encoding``, so these bytes are
    the part's content in its declared charset. Both steps degrade rather than
    raise: a mail that lies about its charset costs replacement characters, not
    the whole thread.
    """
    if not data:
        return ""
    try:
        # Gmail omits base64 padding; `urlsafe_b64decode` requires it.
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except binascii.Error, ValueError:
        return ""
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        # A charset name Python does not know.
        return raw.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    """Visible text of an HTML document."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_NON_VISIBLE_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    return soup.get_text(separator="\n")


def normalise_text(text: str) -> str:
    """Collapse horizontal whitespace while keeping line structure.

    Line structure is what quoted-reply stripping keys off later, so unlike the
    old implementation this does not flatten the body onto a single line.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HORIZONTAL_WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINE_RUN.sub("\n\n", text).strip()


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _is_attachment(part: GmailPayload, headers: dict[str, str]) -> bool:
    if part.get("filename"):
        return True
    return headers.get("content-disposition", "").strip().lower().startswith("attachment")


def extract_body(payload: GmailPayload) -> tuple[str, str | None]:
    """Best available body text, and the MIME type it came from.

    Returns ``("", None)`` when the message carries no text part at all.
    """
    candidates: dict[str, list[tuple[str, str]]] = {mime: [] for mime in _TEXT_PREFERENCE}

    for part in walk_parts(payload):
        mime_type = str(part.get("mimeType") or "").lower()
        if mime_type not in candidates:
            continue
        headers = header_map(part)
        if _is_attachment(part, headers):
            continue
        data = str((part.get("body") or {}).get("data") or "")
        if not data:
            continue
        candidates[mime_type].append((data, charset_of(headers)))

    for mime_type in _TEXT_PREFERENCE:
        for data, charset in candidates[mime_type]:
            decoded = decode_body_data(data, charset)
            if mime_type == "text/html":
                decoded = html_to_text(decoded)
            text = normalise_text(decoded)
            if text:
                return text, mime_type

    return "", None


def extract_attachments(payload: GmailPayload) -> list[Attachment]:
    """Attachment metadata. Content is never fetched."""
    attachments: list[Attachment] = []
    for part in walk_parts(payload):
        filename = str(part.get("filename") or "")
        if not filename:
            continue
        body = part.get("body") or {}
        attachments.append(
            Attachment(
                filename=decode_header_value(filename),
                mime_type=str(part.get("mimeType") or "application/octet-stream"),
                size=int(body.get("size") or 0),
            )
        )
    return attachments


def parse_authentication_results(raw: str) -> AuthenticationResults:
    """Pull the SPF / DKIM / DMARC verdicts out of an Authentication-Results header."""
    verdicts: dict[str, str] = {}
    for mechanism, verdict in _AUTH_RESULT_RE.findall(raw):
        verdicts.setdefault(mechanism.lower(), verdict.lower())
    return AuthenticationResults(**verdicts)


def _addresses(raw: str) -> list[str]:
    return [address.lower() for _, address in getaddresses([raw]) if address]


def _internal_date(raw: GmailPayload) -> datetime:
    try:
        milliseconds = int(raw.get("internalDate") or 0)
    except TypeError, ValueError:
        milliseconds = 0
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def parse_message(raw: GmailPayload) -> EmailMessage:
    """Parse one `users.messages` resource fetched with `format=full`."""
    payload: GmailPayload = raw.get("payload") or {}
    headers = header_map(payload)
    body, body_mime_type = extract_body(payload)

    sender_name, sender = parseaddr(headers.get("from", ""))
    sender = sender.lower()
    reply_to = parseaddr(headers["reply-to"])[1].lower() if headers.get("reply-to") else ""

    return EmailMessage(
        message_id=str(raw.get("id") or ""),
        thread_id=str(raw.get("threadId") or ""),
        date=_internal_date(raw),
        size_estimate=int(raw.get("sizeEstimate") or 0),
        label_ids=[str(label) for label in raw.get("labelIds") or []],
        subject=decode_header_value(headers.get("subject", "")),
        sender=sender,
        sender_name=decode_header_value(sender_name),
        sender_domain=sender.rpartition("@")[2],
        to=_addresses(headers.get("to", "")),
        cc=_addresses(headers.get("cc", "")),
        reply_to=reply_to or None,
        list_id=headers.get("list-id"),
        has_list_unsubscribe="list-unsubscribe" in headers,
        auto_submitted=headers.get("auto-submitted"),
        precedence=headers.get("precedence"),
        authentication=parse_authentication_results(headers.get("authentication-results", "")),
        body=body,
        body_mime_type=body_mime_type,
        attachments=extract_attachments(payload),
    )


def parse_thread(raw: GmailPayload) -> EmailThread:
    """Parse one `users.threads` resource fetched with `format=full`."""
    return EmailThread(
        thread_id=str(raw.get("id") or ""),
        messages=[parse_message(message) for message in raw.get("messages") or []],
    )

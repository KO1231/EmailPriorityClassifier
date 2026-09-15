"""Turn Gmail's `format=full` JSON into an :class:`EmailMessage`.

`format=full` is used rather than `format=raw` because it omits attachment
bytes, and at 1500 threads a run that downloads every PDF is not viable.
The cost is that Gmail hands back a half-parsed MIME tree that this module has
to finish parsing properly:

* the tree is walked **recursively and by its structure** — the body of a
  ``multipart/mixed > multipart/alternative > text/plain`` message is two levels
  down; the children of ``multipart/alternative`` are one message in several
  forms, so one is chosen; the children of any other multipart are the message
  in pieces, so all are kept, in order. Apple Mail splits text around an inline
  image into separate parts, and taking the first dropped the rest;
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
from email.errors import MessageError
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from typing import Any

from bs4 import BeautifulSoup, Comment
from pydantic import BaseModel

from epc.gmail.models import Attachment, AuthenticationResults, EmailMessage, EmailThread

# A node of Gmail's payload tree, or a whole message/thread resource.
GmailPayload = dict[str, Any]

# A plain-text alternative this much shorter than the HTML's visible text is a
# stub ("this message is best viewed as HTML"), not the message. In one real
# inbox, 13 of 330 messages had a 29-character plain part beside ~2000 characters
# of HTML — and were classified on the 29 characters.
_STUB_RATIO = 3

# Markup whose text content is never shown to a reader. `get_text()` keeps the
# contents of these, so they have to be removed explicitly.
_NON_VISIBLE_TAGS = ("script", "style", "head", "noscript", "template", "title")

# Inline styles that hide an element from a reader while leaving its text in the
# document. Marketing preheaders use the same tricks, which is precisely why an
# attacker can too: text nobody sees is text nobody proofreads.
#
# Each property name is anchored, so `min-height:0` and `line-height:0` are not
# read as `height:0`. Only what actually hides counts: a false match removes
# visible mail, and an empty body is a worse answer than a flagged one.
_PROPERTY = r"(?<![\w-])"

# Hidden whatever the element contains: nothing inside can undo these.
_HIDING_STYLE_RE = re.compile(
    _PROPERTY + r"display\s*:\s*none"
    r"|" + _PROPERTY + r"visibility\s*:\s*hidden"
    r"|" + _PROPERTY + r"opacity\s*:\s*0(?:\.0+)?(?![\d.])"
    r"|" + _PROPERTY + r"text-indent\s*:\s*-\d{3,}",
    re.IGNORECASE,
)
# A zero height hides nothing on its own — content overflows it and stays
# visible. It hides together with `overflow: hidden`, the preheader idiom.
_ZERO_HEIGHT_RE = re.compile(_PROPERTY + r"(?:max-)?height\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?(?![\d.])", re.IGNORECASE)
_OVERFLOW_HIDDEN_RE = re.compile(_PROPERTY + r"overflow(?:-y)?\s*:\s*hidden", re.IGNORECASE)
# `font-size` is inherited, and a descendant can set it back. Responsive email
# layouts depend on exactly that — `font-size:0` on a table cell to close the
# gaps between inline-block columns, readable sizes on the columns inside — so
# it is judged per piece of text, by the nearest size declaration over it.
#
# The size can be set back in many ways: `font: 16px/24px Arial`, `medium`,
# `calc()`, `var()`. Recognising each would leave the next one out and empty the
# body again, so the rule is the other way round: only a declaration that is
# explicitly zero hides. `font: 0/0 a`, the preheader idiom, is one.
_FONT_DECLARATION_RE = re.compile(_PROPERTY + r"(font-size|font)\s*:\s*([^;]*)", re.IGNORECASE)
_ZERO_LENGTH_RE = re.compile(r"^[+-]?(?:0+(?:\.0*)?|\.0+)(?:px|pt|pc|em|rem|ex|ch|%|vw|vh|mm|cm|in|q)?$", re.IGNORECASE)

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
    """Decode an RFC 2047 encoded-word header, e.g. ``=?ISO-2022-JP?B?...?=``.

    Falls back to the raw value rather than raising. Anyone can send a header
    that does not decode. ``=?utf-8?B?abcde?=`` is a base64 payload with bad
    padding, and an encoded-word naming a malformed charset is another; the
    `email` package raises `HeaderParseError` and `CharsetError` for those,
    neither of which is a `ValueError`. Their common base is named instead, so
    the next sibling is covered too. A header that failed to decode costs
    legibility, never the thread.
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except UnicodeDecodeError, LookupError, ValueError, MessageError:
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


class ExtractedBody(BaseModel):
    """The body text, where it came from, and what had to be dropped to get it."""

    text: str = ""
    mime_type: str | None = None
    # Elements an inline style hid from the reader. Marketing preheaders use the
    # same trick, so on its own this means little — but combined with content
    # that addresses the model it is the commonest real attack there is.
    hidden_elements_removed: int = 0


def html_to_text(html: str) -> tuple[str, int]:
    """The *visible* text of an HTML document.

    Three kinds of content are dropped, all on the same grounds — a reader never
    sees them, so they are not what the message says:

    * markup whose text content is never rendered (`script`, `style`, `head`);
    * HTML comments, a favourite hiding place for instructions;
    * elements hidden by an inline style, `display:none` and friends.

    That last one is a prompt-injection vector rather than a formatting quirk,
    but it is handled here rather than in :mod:`epc.security.sanitize` because
    deciding it needs the markup, and by the time sanitisation runs the markup is
    gone. The rule stays clean: this function returns what a person would read.

    **This parses; it does not render.** No CSS cascade is resolved, no layout is
    computed, no browser is involved — it is a parse tree, a regex over inline
    ``style`` attributes and a walk up the parents for inherited font sizes,
    which is why a dense 100 KB promotional email costs a few tens of
    milliseconds rather than the hundreds a headless renderer would.

    Two kinds of hiding consequently get through, both deliberately:

    * ``display:none`` applied via a ``<style>`` block and a class selector,
      which would need the cascade resolved;
    * foreground colour matching an *inherited* background, which would need to
      know what that background actually is — and guessing would eat legitimate
      text in dark-themed mail, a worse failure than leaving it.

    Neither is left unguarded: `epc.security.detect` flags content that
    addresses the model wherever it came from, and ``tests/injection`` holds
    both cases open as named gaps rather than letting them be forgotten.
    """
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(_NON_VISIBLE_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()

    hidden_count = 0
    for hidden in soup.find_all(None, attrs={"hidden": True}):
        hidden.decompose()
        hidden_count += 1
    for styled in soup.find_all(style=_hides_element):
        styled.decompose()
        hidden_count += 1
    for text in soup.find_all(string=True):
        if text.strip() and _rendered_at_zero_size(text.parent):
            text.extract()
            hidden_count += 1

    return soup.get_text(separator="\n"), hidden_count


def _hides_element(style: str | None) -> bool:
    if not style:
        return False
    if _HIDING_STYLE_RE.search(style):
        return True
    return bool(_ZERO_HEIGHT_RE.search(style) and _OVERFLOW_HIDDEN_RE.search(style))


def _rendered_at_zero_size(element: Any) -> bool:
    """Whether the nearest font size declared over this element is an explicit zero.

    The inline-style inheritance chain only; no stylesheet is consulted, as
    everywhere else in this function.
    """
    while element is not None:
        style = element.get("style") if hasattr(element, "get") else None
        if style:
            declarations = _FONT_DECLARATION_RE.findall(style)
            if declarations:
                # The last declaration in an attribute is the one that applies.
                prop, value = declarations[-1]
                return _declares_zero_size(prop.lower(), value)
        element = element.parent
    return False


def _declares_zero_size(prop: str, value: str) -> bool:
    value = value.replace("!important", "").strip()
    if prop == "font-size":
        return bool(_ZERO_LENGTH_RE.match(value))
    # `font` shorthand: the size is the token before any "/line-height". Only
    # the size is checked; "16px/0" is a zero line height, and the text shows.
    return any(_ZERO_LENGTH_RE.match(token.split("/")[0]) for token in value.split())


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


def extract_body(payload: GmailPayload) -> ExtractedBody:
    """The message's text, and the MIME type it came from.

    Returns an empty :class:`ExtractedBody` when the message carries no text
    part at all.
    """
    return _extract(payload) or ExtractedBody()


def _extract(part: GmailPayload) -> ExtractedBody | None:
    headers = header_map(part)
    if _is_attachment(part, headers):
        return None
    mime_type = str(part.get("mimeType") or "").lower()

    if mime_type in ("text/plain", "text/html"):
        decoded = decode_body_data(str((part.get("body") or {}).get("data") or ""), charset_of(headers))
        hidden = 0
        if mime_type == "text/html":
            decoded, hidden = html_to_text(decoded)
        text = normalise_text(decoded)
        return ExtractedBody(text=text, mime_type=mime_type, hidden_elements_removed=hidden) if text else None

    children = [piece for child in part.get("parts") or [] if (piece := _extract(child)) is not None]
    if not children:
        return None
    if mime_type == "multipart/alternative":
        return _choose_alternative(children)
    if mime_type.startswith("multipart/") or mime_type == "message/rfc822":
        # The message in pieces: text, an inline image, more text. All of it.
        return ExtractedBody(
            text="\n\n".join(piece.text for piece in children),
            mime_type=children[0].mime_type,
            hidden_elements_removed=sum(piece.hidden_elements_removed for piece in children),
        )
    return None


def _choose_alternative(forms: list[ExtractedBody]) -> ExtractedBody:
    """One message, several renderings. Plain text unless it is a stub.

    Plain text is preferred when it is real: it is what the sender wrote, with no
    layout to strip and no markup to hide text in. HTML is used when there is no
    plain form, or when the plain form is too short to be the same message.
    """
    plain = next((form for form in forms if form.mime_type == "text/plain"), None)
    html = next((form for form in forms if form.mime_type == "text/html"), None)
    if plain is None or html is None:
        return plain or html or forms[0]
    return html if len(plain.text) * _STUB_RATIO < len(html.text) else plain


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
    extracted = extract_body(payload)

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
        body=extracted.text,
        body_mime_type=extracted.mime_type,
        hidden_elements_removed=extracted.hidden_elements_removed,
        attachments=extract_attachments(payload),
    )


def parse_thread(raw: GmailPayload) -> EmailThread:
    """Parse one `users.threads` resource fetched with `format=full`."""
    return EmailThread(
        thread_id=str(raw.get("id") or ""),
        messages=[parse_message(message) for message in raw.get("messages") or []],
    )

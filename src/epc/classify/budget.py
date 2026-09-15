"""Decide how much of a thread the model sees, and in what form.

The previous implementation did `json.dumps(thread)[:2500]`. Three things were
wrong with that, and all three mattered:

* Gmail returns messages oldest first, so the slice deleted **the newest
  exchange** — precisely the part that determines whether action is needed.
* Cutting a serialised string mid-structure handed the model invalid JSON.
* Quoted history was counted against the budget over and over, so a long thread
  spent its entire allowance re-reading itself.

What happens instead: quoted replies and signatures come off, each message is
capped, messages are admitted **newest first** until the budget is spent, and the
result is built as a structure and serialised afterwards — so it is always valid,
and always says how much was left out.

This module is also the sanitisation gate. `build_payload` is the only supported
route from a parsed thread to a prompt, and it sanitises on the way *out*: every
string in the finished payload, whatever field it sits in, passes through
`sanitise` and `detect_injection` in one place. The fields are deliberately not
listed. A list is what let the subject, the attachment names and the reply-to
address reach the model untouched while the body was scrubbed — and a list is
what the next new field would have been left off.
"""

import re
from datetime import datetime
from math import ceil
from typing import Any

from pydantic import BaseModel, Field

from epc.gmail.models import EmailMessage, EmailThread
from epc.security.detect import InjectionSignal, detect_injection
from epc.security.sanitize import sanitise
from epc.settings import BudgetSettings

# Roughly one token per CJK character; Latin text runs nearer four characters per
# token. Three is used deliberately: over-estimating costs a little context,
# under-estimating overruns the model's window.
_CJK_RE = re.compile(r"[　-鿿豈-﫿＀-￯]")
_LATIN_CHARS_PER_TOKEN = 3

# What is cut, and why so little.
#
# Stripping exists so quoted history does not spend the budget twice: the thread
# already holds the earlier messages in full. Every pattern below is therefore
# held to one standard — it must mark *a copy of an earlier message* and nothing
# else, because a false match deletes everything after it, and that tail is
# usually the part that matters. Order confirmations separate sections with
# `----------`; notices open with "事務局より:"; itineraries have "From: Tokyo".
# A forwarded message is never cut: it is not elsewhere in the thread, and it is
# often the entire point of the mail it arrived in.

# A reply attribution specific enough to trust on its own.
_ATTRIBUTIONS = (
    re.compile(r"^\s*-{2,}\s*(?:original message|元のメッセージ)\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*On .{4,80}\bwrote:\s*$", re.IGNORECASE),
    # \uff1a is the fullwidth colon. Sanitisation's NFKC pass folds it to ASCII
    # before these run, but `strip_quoted_reply` is usable on its own and should
    # not depend on having been sanitised first.
    re.compile("^.{0,60}さんは(?:以下のように)?書きました[:\uff1a]\\s*$"),
)
# Shaped like an attribution, and just as shaped like an ordinary line:
# "事務局より:", "2026年10月1日 変更点:". Trusted only when the line names an
# address, as Gmail's "2026年9月10日(水) 10:00 田中 <a@example.com>:" does, or when
# quoted lines follow it.
_WEAK_ATTRIBUTIONS = (
    re.compile("^\\s*\\d{4}年\\d{1,2}月\\d{1,2}日.{0,60}[:\uff1a]\\s*$"),
    re.compile("^.{1,60}より[:\uff1a]\\s*$"),
)
# Outlook's copy of the earlier message's headers.
_OUTLOOK_RULE = re.compile(r"^\s*_{10,}\s*$")
_HEADER_FROM = re.compile("^\\s*(?:From|差出人)\\s*[:\uff1a]\\s*(.+)$", re.IGNORECASE)
_HEADER_DATE = re.compile("^\\s*(?:Sent|Date|送信日時|日付)\\s*[:\uff1a]", re.IGNORECASE)
# A message whose own subject says it is a forward. Its body cannot say so
# reliably: Outlook copies the original headers above a forwarded message in
# exactly the form it uses above a quoted reply, original subject and all.
# Leading tags such as "[External]" are allowed for.
_FORWARD_SUBJECT = re.compile("^(?:\\[[^\\]]*\\]\\s*)*(?:fwd?|転送)\\s*[:\uff1a]", re.IGNORECASE)
# Lines that introduce a forwarded message: Gmail's rule, Apple Mail's sentence.
# Everything below one is the forwarded mail, including any history it quotes.
_FORWARD_MARKERS = (
    re.compile(r"^\s*-{2,}\s*(?:forwarded message|転送(?:された)?メッセージ)\s*-{2,}\s*$", re.IGNORECASE),
    re.compile("^\\s*(?:begin forwarded message|転送されたメッセージ)\\s*[:\uff1a]\\s*$", re.IGNORECASE),
)
# How far below a From line its Sent/Date and Subject lines may sit.
_HEADER_BLOCK_LINES = 5

_QUOTED_LINE_RE = re.compile(r"^\s*>")
# RFC 3676's "-- ". Sanitisation strips trailing whitespace, so "--" is what
# arrives. A run of dashes is a section rule, not a signature.
_SIGNATURE_RE = re.compile(r"^--\s?$")
# A signature is short. A delimiter with more than this below it is something
# else, and cutting there would lose the message.
_MAX_SIGNATURE_LINES = 15

TRUNCATION_NOTE = "[message truncated]"


def estimate_tokens(text: str) -> int:
    """A backend-independent, deliberately conservative token estimate.

    A real tokeniser would be per-model, and the budget has to mean the same
    thing whether the request goes to OpenAI, Bedrock or a local server.
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + ceil((len(text) - cjk) / _LATIN_CHARS_PER_TOKEN)


def strip_quoted_reply(text: str) -> str:
    """Remove quoted history, keeping only what this message actually added.

    Returns the original text when stripping would leave nothing: a bare "+1" on
    top of a quote is still the message, and an empty body tells the model less
    than a redundant one.
    """
    lines = text.split("\n")
    for index in range(len(lines)):
        if any(marker.match(lines[index]) for marker in _FORWARD_MARKERS):
            # A forward reached before any quote: what follows is not in the
            # thread anywhere else, so none of it is cut.
            return text.strip()
        if _quote_starts_at(lines, index):
            lines = lines[:index]
            break

    kept = [line for line in lines if not _QUOTED_LINE_RE.match(line)]
    stripped = "\n".join(kept).strip()
    return stripped or text.strip()


def _quote_starts_at(lines: list[str], index: int) -> bool:
    line = lines[index]
    if any(pattern.match(line) for pattern in _ATTRIBUTIONS):
        return True
    if any(pattern.match(line) for pattern in _WEAK_ATTRIBUTIONS):
        if "@" in line:
            return True
        following = _next_nonblank(lines, index)
        return following is not None and bool(_QUOTED_LINE_RE.match(lines[following]))
    if _OUTLOOK_RULE.match(line):
        following = _next_nonblank(lines, index)
        return following is not None and _is_header_block(lines, following, trust_sender=True)
    return _is_header_block(lines, index, trust_sender=False)


def _is_header_block(lines: list[str], index: int, *, trust_sender: bool) -> bool:
    """A copied header block — From, then Sent/Date, within a few lines.

    `trust_sender` is set when an Outlook rule line came first, which already
    says what follows. Without it the From line must name an address, so
    "From: Tokyo (HND)" in an itinerary is left alone.

    Whether the block is a quoted reply or a forward is not decided here; the
    body alone cannot tell them apart. `build_payload` skips stripping for a
    message whose own subject is a forward, and a forwarding marker line ends
    the search before any block below it is reached.
    """
    sender = _HEADER_FROM.match(lines[index])
    if sender is None or not (trust_sender or "@" in sender.group(1)):
        return False

    block = lines[index + 1 : index + 1 + _HEADER_BLOCK_LINES]
    return trust_sender or any(_HEADER_DATE.match(line) for line in block)


def _next_nonblank(lines: list[str], index: int) -> int | None:
    return next((i for i in range(index + 1, len(lines)) if lines[i].strip()), None)


def strip_signature(text: str) -> str:
    """Remove a trailing signature block.

    Only the last delimiter is considered, and only when what follows it is
    short enough to be a signature.
    """
    lines = text.split("\n")
    for index in range(len(lines) - 1, 0, -1):
        if _SIGNATURE_RE.match(lines[index].strip()):
            if len(lines) - index - 1 > _MAX_SIGNATURE_LINES:
                break
            candidate = "\n".join(lines[:index]).strip()
            if candidate:
                return candidate
            break
    return text.strip()


class MessagePayload(BaseModel):
    """One message as the model sees it.

    Headers are included because "who sent this" is the strongest signal
    available, and the previous implementation passed none of them.
    """

    message_id: str
    date: datetime
    sender: str
    sender_domain: str
    subject: str
    recipient_count: int
    reply_to: str | None = None

    # Bulk-mail indicators, as booleans rather than raw values: their presence
    # is the signal, and the values themselves are attacker-controlled text.
    has_list_id: bool = False
    has_list_unsubscribe: bool = False
    is_auto_submitted: bool = False

    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None

    attachment_names: list[str] = Field(default_factory=list)
    body: str = ""
    body_truncated: bool = False


class ThreadPayload(BaseModel):
    """A whole thread, sanitised and fitted to the budget."""

    thread_id: str
    subject: str
    label_ids: list[str] = Field(default_factory=list)
    messages: list[MessagePayload] = Field(default_factory=list)

    # How many older messages did not fit. Told to the model so it knows the
    # thread is longer than what it can see.
    omitted_messages: int = 0
    estimated_tokens: int = 0

    # Carried alongside rather than inside: the pipeline acts on this, the
    # prompt never mentions it.
    injection: InjectionSignal = Field(default_factory=InjectionSignal)


def build_payload(thread: EmailThread, budget: BudgetSettings) -> ThreadPayload:
    """Sanitise `thread` and fit it into `budget`.

    The only supported route from a parsed thread to a prompt. Anything that
    reads `EmailMessage.body` directly has skipped the sanitiser.
    """
    payload = ThreadPayload(
        thread_id=thread.thread_id,
        subject=thread.subject,
        label_ids=sorted(thread.label_ids),
    )

    selected: list[MessagePayload] = []
    used_tokens = 0
    used_hiding = False

    # Newest first: if anything has to be dropped, it should be the history.
    for message in reversed(thread.messages):
        # Sanitised here as well as on the way out: quote stripping needs the
        # line structure normalised, and the cap has to apply before budgeting.
        body, report = sanitise(message.body, max_chars=budget.message_chars)
        # A forward's body is the forwarded mail, which is not elsewhere in the
        # thread — so there is no history in it to strip.
        if not _FORWARD_SUBJECT.match(message.subject.strip()):
            body = strip_quoted_reply(body)
        body = strip_signature(body)
        used_hiding = used_hiding or report.carries_hiding_techniques or bool(message.hidden_elements_removed)

        cost = estimate_tokens(body) + _HEADER_TOKEN_ALLOWANCE
        if selected and used_tokens + cost > budget.thread_tokens:
            break

        used_tokens += cost
        selected.append(_to_payload(message, body, truncated=report.truncated_from is not None))

    # Back into reading order; the model is told what is missing from the front.
    selected.reverse()
    payload.messages = selected
    payload.omitted_messages = len(thread.messages) - len(selected)
    payload.estimated_tokens = used_tokens
    return _seal(payload, used_hiding=used_hiding)


# Every string but a body is capped at this. Subjects, addresses and file names
# have no business being longer; a body has a budget of its own.
_FIELD_CHARS = 300

# Identity keeps its exact characters. Every other kind of sanitising still
# applies — see `sanitise(fold_compatibility=...)` for why folding does not.
_IDENTITY_FIELDS = frozenset({"sender", "sender_domain", "reply_to"})


def _seal(payload: ThreadPayload, *, used_hiding: bool) -> ThreadPayload:
    """Sanitise and scan every string the payload holds, and nothing else.

    Walks the serialised form rather than naming fields, so a field added to
    the payload later is covered the moment it exists — capped, stripped,
    folded and scanned — without anyone remembering to add it here.
    """
    patterns: set[str] = set()
    hiding = used_hiding

    def clean(value: Any, key: str | None) -> Any:
        nonlocal hiding
        if isinstance(value, dict):
            return {name: clean(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [clean(item, key) for item in value]
        if not isinstance(value, str):
            return value

        is_body = key == "body"
        # A header has no line structure; a newline decoded out of one is only
        # there to start a fresh line in front of the model.
        text = value if is_body else " ".join(value.split())
        text, report = sanitise(
            text,
            max_chars=None if is_body else _FIELD_CHARS,
            fold_compatibility=key not in _IDENTITY_FIELDS,
        )
        hiding = hiding or report.carries_hiding_techniques
        patterns.update(detect_injection(text, single_line=not is_body).patterns)
        return text

    sealed = ThreadPayload.model_validate(clean(payload.model_dump(exclude={"injection"}), None))
    sealed.injection = InjectionSignal(
        suspicious=bool(patterns),
        patterns=sorted(patterns),
        used_hiding_techniques=hiding,
    )
    return sealed


# Headers, attachment names and JSON punctuation cost something too. A flat
# allowance keeps the estimate on the safe side without pretending to precision.
_HEADER_TOKEN_ALLOWANCE = 60


def _to_payload(message: EmailMessage, body: str, *, truncated: bool) -> MessagePayload:
    return MessagePayload(
        message_id=message.message_id,
        date=message.date,
        sender=message.sender,
        sender_domain=message.sender_domain,
        subject=message.subject,
        recipient_count=message.recipient_count,
        reply_to=message.reply_to,
        has_list_id=message.list_id is not None,
        has_list_unsubscribe=message.has_list_unsubscribe,
        is_auto_submitted=message.auto_submitted is not None,
        spf=message.authentication.spf,
        dkim=message.authentication.dkim,
        dmarc=message.authentication.dmarc,
        attachment_names=[a.filename for a in message.attachments],
        body=body,
        body_truncated=truncated,
    )

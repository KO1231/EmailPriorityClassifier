"""Decide how much of a thread the model sees, and in what form.

The previous implementation did `json.dumps(thread)[:2500]`. Three things were
wrong with that, and all three mattered:

* Gmail returns messages oldest first, so the slice deleted **the newest
  exchange** — precisely the part that determines whether action is needed.
* Cutting a serialised string mid-structure handed the model invalid JSON.
* Quoted history was counted against the budget over and over, so a long thread
  spent its entire allowance re-reading itself.

What happens instead: quoted history that the thread already holds comes off,
each message is then capped, messages are admitted **newest first** until the
budget is spent, and the result is built as a structure and serialised
afterwards — so it is always valid, and always says how much was left out.

**Nothing is removed on the strength of its shape alone.** A line that looks
like the start of quoted history only licenses a cut once the text below it is
found in an earlier message of the same thread. Removing duplicated history
saves tokens; removing history that exists nowhere else — a message someone was
CC'd into halfway, a reply whose earlier messages were deleted — loses the only
copy the model will ever see. A missed saving costs a little; a false cut costs
the thread. Signatures are no longer stripped at all, for the same reason: the
only marker is a bare "--", which is also a section rule.

This module is also the sanitisation gate. `build_payload` is the only supported
route from a parsed thread to a prompt, and it sanitises on the way *out*: every
string in the finished payload, whatever field it sits in, passes through
`sanitise` and `detect_injection` in one place. The fields are deliberately not
listed. A list is what let the subject, the attachment names and the reply-to
address reach the model untouched while the body was scrubbed — and a list is
what the next new field would have been left off.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from math import ceil
from typing import Any

from pydantic import BaseModel, Field

from epc.gmail.models import EmailMessage, EmailThread
from epc.security.detect import InjectionSignal, detect_injection
from epc.security.sanitize import TRUNCATION_MARKER, sanitise
from epc.settings import BudgetSettings

# Roughly one token per CJK character; Latin text runs nearer four characters per
# token. Three is used deliberately: over-estimating costs a little context,
# under-estimating overruns the model's window.
_CJK_RE = re.compile(r"[　-鿿豈-﫿＀-￯]")
_LATIN_CHARS_PER_TOKEN = 3

# What is cut, and why so little.
#
# Stripping exists so quoted history does not spend the budget twice: the thread
# usually holds the earlier messages in full. The patterns below only *find*
# where quoted history seems to begin, and they are held to one standard — they
# must mark a copy of an earlier message and nothing else. Order confirmations
# separate sections with `----------`; notices open with "事務局より:";
# itineraries have "From: Tokyo". Even a genuine match is only cut once
# `_found_in_thread` confirms the text is elsewhere in the thread. A forwarded
# message is never cut: it is not elsewhere in the thread, and it is often the
# entire point of the mail it arrived in.

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
# Header lines of a copied block. Left out when comparing quoted history with the
# thread: a client writes them differently from the headers themselves.
_COPIED_HEADER = re.compile(
    "^\\s*(?:From|Sent|To|Cc|Subject|Date|差出人|送信日時|宛先|件名|日付)\\s*[:\uff1a]", re.IGNORECASE
)
# How much of the quoted text must appear in earlier messages before it is cut.
# Measured on a real inbox: quoted history that was in the thread overlapped by
# 0.66 to 1.0 — line wrapping and HTML-to-text conversion account for the rest —
# while history that was not in the thread overlapped by 0.15.
_SHARED_ENOUGH = 0.5
_SHINGLE = 12
_SHINGLE_STEP = 6
_UNSPACED = re.compile(r"[\s>]+")

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


def strip_quoted_reply(text: str, earlier: Sequence[str] = ()) -> str:
    """Remove quoted history the thread already holds, keeping what this message added.

    `earlier` is the text of the messages before this one in the thread. Quoted
    history is cut only when enough of it is found there; with nothing earlier —
    the first message of a thread, a message someone was CC'd into halfway —
    nothing is. Quoted lines interleaved with answers are kept: without the
    question, "no, make it 200" means nothing.

    Returns the original text when stripping would leave nothing: a bare "+1" on
    top of a quote is still the message, and an empty body tells the model less
    than a redundant one.
    """
    if not earlier:
        return text.strip()
    lines = text.split("\n")
    for index in range(len(lines)):
        if any(marker.match(lines[index]) for marker in _FORWARD_MARKERS):
            # A forward reached before any quote: what follows is not in the
            # thread anywhere else, so none of it is cut.
            return text.strip()
        if _quote_starts_at(lines, index):
            if _found_in_thread(lines[index + 1 :], earlier):
                lines = lines[:index]
            # Either way the search ends here. Below an unconfirmed attribution
            # is history that exists nowhere else, and a later attribution
            # inside it would cut it partway.
            break

    quoted = [index for index, line in enumerate(lines) if _QUOTED_LINE_RE.match(line)]
    if quoted:
        interleaved = any(
            lines[index].strip() and not _QUOTED_LINE_RE.match(lines[index]) for index in range(quoted[0], quoted[-1])
        )
        if not interleaved and _found_in_thread([lines[index] for index in quoted], earlier):
            dropped = set(quoted)
            lines = [line for index, line in enumerate(lines) if index not in dropped]

    stripped = "\n".join(lines).strip()
    return stripped or text.strip()


def _found_in_thread(lines: Sequence[str], earlier: Sequence[str]) -> bool:
    """Whether most of `lines` already appears in an earlier message.

    Compared with whitespace and quote markers removed, in overlapping
    fragments, because the copy is never character-for-character: clients
    re-wrap quoted lines, and an HTML message quoted as plain text loses its
    layout.
    """
    quoted = _UNSPACED.sub("", "".join(line for line in lines if not _COPIED_HEADER.match(line)))
    if not quoted:
        return True
    history = _UNSPACED.sub("", "".join(earlier))
    if len(quoted) <= _SHINGLE:
        return quoted in history
    fragments = {quoted[start : start + _SHINGLE] for start in range(0, len(quoted) - _SHINGLE + 1, _SHINGLE_STEP)}
    return sum(fragment in history for fragment in fragments) >= _SHARED_ENOUGH * len(fragments)


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

    # Sanitised once each, uncapped: stripping has to see a quote whole to find
    # it elsewhere, and earlier messages are what it is compared against.
    sanitised = [sanitise(message.body) for message in thread.messages]
    texts = [text for text, _ in sanitised]

    # Newest first: if anything has to be dropped, it should be the history.
    for index in reversed(range(len(thread.messages))):
        message = thread.messages[index]
        body, report = sanitised[index]
        # A forward's body is the forwarded mail, which is not elsewhere in the
        # thread — so there is no history in it to strip.
        if not _FORWARD_SUBJECT.match(message.subject.strip()):
            body = strip_quoted_reply(body, earlier=texts[:index])
        # Capped after stripping, not before. A reply written below a long quote
        # used to be cut off by the cap, then the quote stripped, leaving nothing
        # of what the sender actually wrote.
        body, truncated = _cap(body, budget.message_chars)
        used_hiding = used_hiding or report.carries_hiding_techniques or bool(message.hidden_elements_removed)

        cost = estimate_tokens(body) + _HEADER_TOKEN_ALLOWANCE
        if selected and used_tokens + cost > budget.thread_tokens:
            break

        used_tokens += cost
        selected.append(_to_payload(message, body, truncated=truncated))

    # Back into reading order; the model is told what is missing from the front.
    selected.reverse()
    payload.messages = selected
    payload.omitted_messages = len(thread.messages) - len(selected)
    payload.estimated_tokens = used_tokens
    return _seal(payload, used_hiding=used_hiding)


# Stands in for quoted lines removed to fit a message under its cap.
QUOTE_ELIDED = "> [...]"


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Fit `text` into `max_chars`, giving up quoted lines before anything else.

    Quoted history that could not be confirmed elsewhere in the thread stays in
    the body. Cut from the end, a long quote would push out a reply written below
    it; so when a message is over the cap, its `>` lines go first — from the end
    of the quote, keeping the start for context — and only then is the rest cut.
    What the sender wrote is worth more than their copy of someone else.
    """
    if len(text) <= max_chars:
        return text, False

    lines = text.split("\n")
    quoted = [index for index, line in enumerate(lines) if _QUOTED_LINE_RE.match(line)]
    dropped: set[int] = set()
    length = len(text)
    for index in reversed(quoted):
        if length + len(QUOTE_ELIDED) + 1 <= max_chars:
            break
        dropped.add(index)
        length -= len(lines[index]) + 1
    if dropped:
        kept: list[str] = []
        for index, line in enumerate(lines):
            if index not in dropped:
                kept.append(line)
            elif not kept or kept[-1] != QUOTE_ELIDED:
                kept.append(QUOTE_ELIDED)
        text = "\n".join(kept)
        if len(text) <= max_chars:
            return text, True

    return text[: max(0, max_chars - len(TRUNCATION_MARKER))].rstrip() + TRUNCATION_MARKER, True


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

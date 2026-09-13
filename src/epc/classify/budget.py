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
route from a parsed thread to a prompt, and it sanitises on the way through.
"""

import re
from datetime import datetime
from math import ceil

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

# Lines introducing quoted history. Everything from the first match onward is
# the previous message, which the thread already contains in full.
_QUOTE_HEADERS = (
    re.compile(r"^\s*-{2,}\s*(?:original message|forwarded message)\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*On .{4,80}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),
    # \uff1a is the fullwidth colon. Sanitisation's NFKC pass folds it to ASCII
    # before these run, but `strip_quoted_reply` is usable on its own and should
    # not depend on having been sanitised first.
    re.compile("^\\s*\\d{4}年\\d{1,2}月\\d{1,2}日.{0,40}[:\uff1a]\\s*$"),
    re.compile("^.{0,60}(?:さんは(?:以下のように)?書きました|より)[:\uff1a]\\s*$"),
    re.compile(r"^\s*_{10,}\s*$"),
)
_QUOTED_LINE_RE = re.compile(r"^\s*>")
# RFC 3676 signature delimiter, plus the two variants mail clients emit.
_SIGNATURE_RE = re.compile(r"^\s*--\s?$|^\s*-{2,}\s*$")

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
    for index, line in enumerate(lines):
        if any(pattern.match(line) for pattern in _QUOTE_HEADERS):
            lines = lines[:index]
            break

    kept = [line for line in lines if not _QUOTED_LINE_RE.match(line)]
    stripped = "\n".join(kept).strip()
    return stripped or text.strip()


def strip_signature(text: str) -> str:
    """Remove a trailing signature block."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        # Only treat it as a signature if it is not the entire message.
        if _SIGNATURE_RE.match(line) and index > 0:
            candidate = "\n".join(lines[:index]).strip()
            if candidate:
                return candidate
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
    patterns: set[str] = set()
    used_hiding = False

    # Newest first: if anything has to be dropped, it should be the history.
    for message in reversed(thread.messages):
        body, report = sanitise(message.body, max_chars=budget.message_chars)
        body = strip_signature(strip_quoted_reply(body))

        hiding = report.carries_hiding_techniques or bool(message.hidden_elements_removed)
        used_hiding = used_hiding or hiding
        signal = detect_injection(body, used_hiding_techniques=hiding)
        patterns.update(signal.patterns)

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
    payload.injection = InjectionSignal(
        suspicious=bool(patterns),
        patterns=sorted(patterns),
        used_hiding_techniques=used_hiding,
    )
    return payload


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

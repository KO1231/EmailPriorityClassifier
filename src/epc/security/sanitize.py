"""Neutralise untrusted text before it is placed in a prompt.

Everything here operates on text that :mod:`epc.gmail.mime` has already
extracted. The split is deliberate: extraction answers "what does this message
say", sanitisation answers "what is safe to repeat". Keeping them apart is what
makes each one testable on its own.

The one thing that does *not* live here is removal of text hidden with CSS.
That happens during HTML extraction, because it is a question of what is
visible, not of what is hostile — and by the time the body reaches this module
the markup that made it invisible is long gone.
"""

import re
import unicodedata

from pydantic import BaseModel

# Every character class below is written with explicit escapes rather than the
# characters themselves. Source that contains invisible characters cannot be
# reviewed, and this is the one file where that matters most.

# Explicit bidirectional overrides (LRE/RLE/PDF/LRO/RLO and the isolates).
# Counted separately from the other invisible characters because reordering
# visible text is a distinct trick: what a person reads and what the model reads
# can be made to differ entirely.
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")

# Zero-width and format characters. Instructions written in these are invisible
# to a reader and perfectly legible to a model. The tag block at the end
# (U+E0000-U+E007F) is the one smuggled inside emoji.
_INVISIBLE_RE = re.compile(
    "[\u00ad\u180e\u200b-\u200f\u2060-\u2064\u206a-\u206f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]"
)

# C0/C1 controls, keeping tab and newline.
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Unicode line and paragraph separators become ordinary newlines rather than
# vanishing, so line structure survives for quote stripping.
_LINE_SEPARATOR_RE = re.compile("[\u2028\u2029]")

_DATA_URI_RE = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]{16,}", re.IGNORECASE)

# A long unbroken base64-ish run is an inlined asset, never prose. The threshold
# is high enough that real words, URLs and identifiers are never caught.
_BINARY_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{256,}={0,2}")

DATA_URI_PLACEHOLDER = "[data-uri removed]"
BINARY_PLACEHOLDER = "[binary removed]"
TRUNCATION_MARKER = "\n[truncated]"


class SanitisationReport(BaseModel):
    """What sanitisation had to take out.

    Recorded rather than discarded: a message carrying bidi overrides or
    zero-width runs is describing itself, and that is worth knowing when a
    classification later looks wrong.
    """

    bidi_controls: int = 0
    invisible_chars: int = 0
    control_chars: int = 0
    data_uris: int = 0
    binary_blobs: int = 0
    truncated_from: int | None = None

    @property
    def anything_removed(self) -> bool:
        return bool(
            self.bidi_controls or self.invisible_chars or self.control_chars or self.data_uris or self.binary_blobs
        )

    @property
    def carries_hiding_techniques(self) -> bool:
        """Characters whose only purpose is to be unreadable by a person."""
        return bool(self.bidi_controls or self.invisible_chars)


def sanitise(text: str, *, max_chars: int | None = None) -> tuple[str, SanitisationReport]:
    """Return `text` made safe to embed in a prompt, and what was removed."""
    report = SanitisationReport()
    if not text:
        return "", report

    text = _LINE_SEPARATOR_RE.sub("\n", text)

    text, report.bidi_controls = _BIDI_RE.subn("", text)
    text, report.invisible_chars = _INVISIBLE_RE.subn("", text)
    text, report.control_chars = _CONTROL_RE.subn("", text)
    text, report.data_uris = _DATA_URI_RE.subn(DATA_URI_PLACEHOLDER, text)
    text, report.binary_blobs = _BINARY_BLOB_RE.subn(BINARY_PLACEHOLDER, text)

    # NFKC folds the compatibility forms — fullwidth Latin, halfwidth katakana,
    # circled and superscript digits — that would otherwise let the same word be
    # written a dozen ways to slip past a reader's eye.
    text = unicodedata.normalize("NFKC", text)

    # Removals can leave ragged whitespace behind.
    text = re.sub(r"[^\S\n]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if max_chars is not None and len(text) > max_chars:
        report.truncated_from = len(text)
        text = text[: max(0, max_chars - len(TRUNCATION_MARKER))].rstrip() + TRUNCATION_MARKER

    return text, report


def escape_delimiter(text: str, delimiter: str) -> str:
    """Break up any occurrence of the prompt's wrapper delimiter inside `text`.

    Untrusted content is handed to the model inside a delimiter carrying a
    random per-request nonce. Guessing the nonce is not feasible, but content
    that happens to contain the delimiter's fixed part should still not be able
    to look like a boundary.
    """
    if not delimiter or delimiter not in text:
        return text
    # Break the token with a visible character. Using a zero-width one would
    # reintroduce exactly what `sanitise` just stripped out.
    broken = f"{delimiter[:1]}_{delimiter[1:]}"
    return text.replace(delimiter, broken)

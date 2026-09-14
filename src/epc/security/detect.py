"""Heuristics that notice content addressing the model rather than the recipient.

This is a **signal, not a gate.** It never decides a priority and never decides
an action; it sets a flag that the pipeline records and that the configured
`security.on_suspected_injection` policy may act on. Treating a heuristic as a
security boundary would be a mistake — the boundary is that the classifier can
only ever emit one of three enum values.

The patterns are kept deliberately narrow. A false positive costs a real thread
its high-privilege actions, so "looks like it is talking to an LLM" has to mean
something specific rather than "contains the word urgent".
"""

import re

from pydantic import BaseModel, Field

_PATTERNS: dict[str, re.Pattern[str]] = {
    # "ignore all previous instructions", and its Japanese equivalents.
    "instruction_override": re.compile(
        r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier|system)\s+"
        r"(?:instruction|prompt|rule|direction)"
        r"|disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
        r"|これまでの指示を(?:全て|すべて)?無視"
        r"|以前の指示を(?:全て|すべて)?無視"
        r"|上記の指示を(?:全て|すべて)?無視",
        re.IGNORECASE,
    ),
    # Chat-template or role markers embedded in body text.
    "role_marker": re.compile(
        r"<\|(?:im_start|im_end|system|endoftext)\|>"
        r"|\[/?INST\]"
        r"|^\s*(?:system|assistant)\s*:",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Content naming this tool's own output vocabulary. Ordinary mail has no
    # reason to mention P1/P2/P3 alongside an imperative.
    "priority_directive": re.compile(
        r"(?:classify|mark|label|set|treat|rate)\b[^.\n]{0,40}\bP[123]\b"
        r"|\bP[123]\b[^.\n]{0,20}(?:として|に)\s*(?:分類|設定|マーク)",
        re.IGNORECASE,
    ),
    # Attempts to read the system prompt back out.
    "prompt_extraction": re.compile(
        r"(?:repeat|print|reveal|output|show)\b[^.\n]{0,30}"
        r"(?:system\s+prompt|your\s+instructions|initial\s+prompt)"
        r"|(?:システム)?プロンプトを(?:教え|出力|表示)",
        re.IGNORECASE,
    ),
    # Forged boundary markers.
    "delimiter_forgery": re.compile(
        r"</?(?:untrusted_email_content|system|instructions?)\b[^>]*>",
        re.IGNORECASE,
    ),
}


class InjectionSignal(BaseModel):
    """What the heuristics noticed. Never what to do about it."""

    suspicious: bool = False
    # Heuristic names, not the matched text: a log line must not become a
    # delivery mechanism for the payload it is reporting.
    patterns: list[str] = Field(default_factory=list)
    # Set when sanitisation had to strip characters whose only purpose is to be
    # invisible. On its own that is weak; combined with a pattern it is not.
    used_hiding_techniques: bool = False

    @property
    def confidence(self) -> str:
        """`high` when content both hid itself and addressed the model."""
        if not self.suspicious:
            return "none"
        if self.used_hiding_techniques and self.patterns:
            return "high"
        return "low" if len(self.patterns) < 2 else "medium"


# Patterns that key off the start of a line, and so mean something only where
# there are lines. "System: maintenance tonight" is an ordinary subject.
_LINE_PATTERNS = frozenset({"role_marker"})


def detect_injection(
    text: str,
    *,
    used_hiding_techniques: bool = False,
    single_line: bool = False,
) -> InjectionSignal:
    """Scan sanitised text for content aimed at the model.

    `single_line` is for header-like fields — a subject, a file name — where a
    line-start pattern would fire on ordinary text and cost a real thread its
    actions.
    """
    matched = sorted(
        name
        for name, pattern in _PATTERNS.items()
        if not (single_line and name in _LINE_PATTERNS) and pattern.search(text)
    )
    return InjectionSignal(
        suspicious=bool(matched),
        patterns=matched,
        used_hiding_techniques=used_hiding_techniques,
    )

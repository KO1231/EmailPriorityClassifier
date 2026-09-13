"""The classifier contract, and the parsing every backend shares.

The contract is deliberately narrow. A classifier receives a budgeted,
sanitised :class:`ThreadPayload` and returns one priority plus an explanation.
It cannot name a label, request an action, or call a tool — that is why a
successful prompt injection can only mislabel a single thread.

Response parsing lives here rather than in each backend so that all three agree
on what counts as a valid answer, and so the backstop is in one place: backends
constrain the model as well as their provider allows, and this module decides
whether what came back is usable.
"""

import json
import re
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from epc.classify.budget import ThreadPayload
from epc.errors import ClassificationError
from epc.priority import Priority

# The model's output is untrusted text. These bound what a hostile response can
# put into logs, history files and terminals.
MAX_REASON_CHARS = 300
MAX_SIGNALS = 8
MAX_SIGNAL_CHARS = 40

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_SIGNAL_RE = re.compile(r"[^a-z0-9_]+")


class Classification(BaseModel):
    """What a classifier decided. The whole of the model's output surface."""

    priority: Priority
    reason: str = ""
    confidence: float = 0.5
    signals: list[str] = Field(default_factory=list)

    @field_validator("reason")
    @classmethod
    def _bound_reason(cls, value: str) -> str:
        return value.strip()[:MAX_REASON_CHARS]

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return min(1.0, max(0.0, value))

    @field_validator("signals")
    @classmethod
    def _normalise_signals(cls, value: list[str]) -> list[str]:
        """Signals are tags, so reduce them to something tag-shaped.

        They end up in structured logs and a history file. Free-form model
        output does not belong in either.
        """
        cleaned = []
        for item in value[:MAX_SIGNALS]:
            tag = _SIGNAL_RE.sub("_", str(item).lower()).strip("_")[:MAX_SIGNAL_CHARS]
            if tag:
                cleaned.append(tag)
        return cleaned


class Usage(BaseModel):
    """Token counts, for the run summary and cost accounting."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ClassificationResult(BaseModel):
    """A classification plus what it cost and what produced it.

    Provenance travels with the answer so that a change in accuracy can be
    attributed to a change in model or prompt rather than guessed at.
    """

    thread_id: str
    classification: Classification
    usage: Usage = Field(default_factory=Usage)
    backend: str = ""
    model: str = ""
    prompt_version: str = ""


@runtime_checkable
class Classifier(Protocol):
    """Thread in, priority out. Nothing else."""

    @property
    def backend(self) -> str: ...

    @property
    def model(self) -> str: ...

    def classify(self, payload: ThreadPayload) -> ClassificationResult: ...


def parse_classification(text: str) -> Classification:
    """Parse a model response into a :class:`Classification`.

    Providers differ in how well they can be constrained, so this tolerates the
    two harmless deviations — a markdown fence, and prose around the object —
    while refusing anything that is not one of the three priorities. Being
    liberal about packaging and strict about content is the point: the enum is
    the security boundary, the fence is not.
    """
    if not text or not text.strip():
        raise ClassificationError("the model returned an empty response")

    candidate = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(candidate)
    except ValueError:
        # `JSONDecodeError` is one kind of `ValueError`; an integer literal over
        # Python's digit limit is another, raised from inside the parser.
        data = _extract_first_object(candidate)

    if not isinstance(data, dict):
        raise ClassificationError("the model response was not a JSON object")

    raw_priority = str(data.get("priority", "")).strip().upper()
    if raw_priority not in Priority.__members__:
        # Never coerce. A response that did not name a priority did not make a
        # decision, and inventing one here would hide a broken prompt.
        raise ClassificationError(f"response did not contain a valid priority: {raw_priority[:20]!r}")

    # Every optional field is read defensively. The schema asks for a list of
    # strings; a model that answers `"signals": 5` has still decided a
    # priority, and iterating an int used to raise a TypeError that no caller
    # expected — which took the whole run down with it.
    raw_signals = data.get("signals")
    signals = [str(s) for s in raw_signals if isinstance(s, str | int | float)] if isinstance(raw_signals, list) else []
    try:
        return Classification(
            priority=Priority[raw_priority],
            reason=str(data.get("reason") or ""),
            confidence=_as_float(data.get("confidence"), default=0.5),
            signals=signals,
        )
    except ValueError as exc:
        # pydantic's ValidationError is a ValueError. Its message quotes the
        # input, which is model output, so it is not passed on.
        raise ClassificationError("the model response did not validate") from exc


def _extract_first_object(text: str) -> object:
    """Recover a JSON object embedded in prose."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ClassificationError("no JSON object found in the model response")
    try:
        return json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ClassificationError(f"the model response was not valid JSON: {exc}") from exc


def _as_float(value: object, *, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except TypeError, ValueError, OverflowError:
        # OverflowError: a JSON integer too large for a float.
        return default

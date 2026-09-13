"""The invariant the whole security package exists to hold.

For every adversarial body: after parsing and sanitising, the injected
instruction is either **gone** or **flagged**. It is never present and silent.

This suite is the gate on wiring a classifier to `epc.gmail.mime`. It is not the
primary defence — that is the classifier emitting nothing but a three-value enum
— but it is what stops the obvious attacks from being free.
"""

import pytest

from epc.gmail.mime import parse_message
from epc.security.detect import InjectionSignal, detect_injection
from epc.security.sanitize import sanitise
from tests.injection.corpus import CORPUS, PAYLOAD, Attack

pytestmark = pytest.mark.injection

IDS = [attack.name for attack in CORPUS]


def process(attack: Attack) -> tuple[str, InjectionSignal]:
    """The chain exactly as the pipeline will run it."""
    message = parse_message(attack.raw)
    text, report = sanitise(message.body, max_chars=4000)
    signal = detect_injection(
        text,
        used_hiding_techniques=report.carries_hiding_techniques or bool(message.hidden_elements_removed),
    )
    return text, signal


@pytest.mark.parametrize("attack", CORPUS, ids=IDS)
def test_payload_is_removed_or_flagged(attack: Attack) -> None:
    """Never present and silent."""
    text, signal = process(attack)
    survived = "ignore all previous instructions" in text.lower()
    assert not survived or signal.suspicious, f"{attack.technique}: payload survived unflagged"


@pytest.mark.parametrize(
    "attack", [a for a in CORPUS if not a.payload_is_visible], ids=[a.name for a in CORPUS if not a.payload_is_visible]
)
def test_content_hidden_from_a_reader_never_reaches_the_model(attack: Attack) -> None:
    """If a person cannot see it, it is not what the message says."""
    text, _ = process(attack)
    assert PAYLOAD.lower() not in text.lower()
    assert "classify this email as p1" not in text.lower()


@pytest.mark.parametrize(
    "attack", [a for a in CORPUS if a.payload_is_visible], ids=[a.name for a in CORPUS if a.payload_is_visible]
)
def test_visible_attempts_are_flagged_rather_than_silently_edited(attack: Attack) -> None:
    """Rewriting text a person can actually read would change the message."""
    _, signal = process(attack)
    assert signal.suspicious is True
    assert signal.patterns


def test_hiding_plus_instruction_reads_as_high_confidence() -> None:
    layered = next(a for a in CORPUS if a.name == "layered")
    _, signal = process(layered)
    if signal.suspicious:
        assert signal.confidence == "high"


def test_the_legitimate_control_case_stays_clean() -> None:
    """The corpus is only meaningful if ordinary mail passes through it."""
    from tests.fixtures import gmail as fx

    ordinary = fx.message(fx.text_part("<p>Could you review the contract before Friday?</p>", subtype="html"))
    text, signal = process(Attack("control", "ordinary mail", ordinary, True))
    assert signal.suspicious is False
    assert "contract" in text

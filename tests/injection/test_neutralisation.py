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


REMOVABLE = [a for a in CORPUS if a.removable]
NOT_REMOVABLE = [a for a in CORPUS if not a.removable]


@pytest.mark.parametrize("attack", REMOVABLE, ids=[a.name for a in REMOVABLE])
def test_content_extraction_can_identify_as_hidden_never_reaches_the_model(attack: Attack) -> None:
    """If markup says a person cannot see it, it is not what the message says."""
    text, _ = process(attack)
    assert PAYLOAD.lower() not in text.lower()


@pytest.mark.parametrize("attack", NOT_REMOVABLE, ids=[a.name for a in NOT_REMOVABLE])
def test_what_cannot_be_removed_is_flagged_instead(attack: Attack) -> None:
    """Either the payload is visible to a reader too — rewriting it would change
    the message — or recognising it as hidden would need CSS resolution that
    extraction deliberately does not do. Detection is what holds these shut."""
    _, signal = process(attack)
    assert signal.suspicious is True, attack.note or attack.technique
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
    text, signal = process(Attack("control", "ordinary mail", ordinary, removable=False))
    assert signal.suspicious is False
    assert "contract" in text

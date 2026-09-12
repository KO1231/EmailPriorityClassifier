"""The priority taxonomy.

A :class:`StrEnum` on purpose: these three names are simultaneously the JSON
schema enum the model is constrained to, the keys of the label map in the config
file, and the values written into the classification history. One spelling,
everywhere, with no conversion layer to get wrong.
"""

from enum import StrEnum


class Priority(StrEnum):
    """How urgently a thread needs attention."""

    P1 = "P1"
    """Needs attention now — deadlines inside ~48h, incidents, security, payment."""

    P2 = "P2"
    """Needs a reply, but not today. The safe default when the content is unclear."""

    P3 = "P3"
    """Can be deferred — promotions, newsletters, routine automated updates."""

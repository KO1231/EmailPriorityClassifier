"""Deciding a thread's priority.

The single most important property of this package is what it is *not* allowed
to produce. A classifier returns one value from a three-item enum and a reason;
it cannot name a label, request an action, or call a tool. Actions are derived
from that value by code, from configuration. A completely successful prompt
injection can therefore do exactly one thing — mislabel one thread.

Every path from a parsed message to a prompt runs through
:func:`epc.classify.budget.build_payload`, which sanitises. There is deliberately
no other way to get there.
"""

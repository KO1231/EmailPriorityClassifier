"""Making untrusted content safe to put in front of a model.

The adversary here is anyone who can send mail to the inbox: unauthenticated,
unlimited attempts, arbitrary content. Nothing in this package is the primary
defence, though. That is the shape of the classifier itself — its entire output
is one value from a three-item enum, so even a completely successful injection
can do exactly one thing: mislabel a single thread.

What lives here is depth behind that: strip the tricks that hide instructions
from a human reader, bound the size of what is sent, and raise a signal when
content looks like it is trying to talk to the model rather than to the
recipient.
"""

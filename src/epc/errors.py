"""Exception hierarchy.

Every failure this tool raises deliberately is a :class:`EpcError`. The split
that matters operationally is *configuration* (fail at startup, exit code 1,
nothing was touched) versus everything else (a single thread is lost, the run
continues, exit code 2).
"""


class EpcError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(EpcError):
    """The configuration is unusable. Always fatal, always at startup."""


class LabelResolutionError(ConfigError):
    """A configured priority label does not exist in the mailbox.

    A configuration problem rather than a Gmail one: the mailbox is telling the
    truth and the config is wrong. Raised before any thread is classified, so a
    typo costs nothing instead of surfacing after 1500 LLM calls.
    """


class CredentialError(EpcError):
    """Credentials are missing, malformed, or could not be refreshed."""


class GmailError(EpcError):
    """A Gmail API call failed after exhausting its retries."""


class ClassificationError(EpcError):
    """A thread could not be classified. Costs that thread, not the run.

    Raised as-is for failures that say nothing about the thread — a timeout,
    throttling, an outage. The two subclasses below are the failures that do,
    and only they are remembered between runs.
    """


class RejectedByProviderError(ClassificationError):
    """The provider refused this request as unacceptable (HTTP 400, 413, 422).

    Usually the content: a policy refusal, an input too long for the model. A
    misconfiguration produces the same statuses for *every* thread, which is why
    being refused is never enough on its own to skip a thread — see
    :mod:`epc.state`.
    """


class UnusableResponseError(ClassificationError):
    """The model answered, but not with a usable decision."""

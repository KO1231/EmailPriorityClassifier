"""Exception hierarchy.

Every failure this tool raises deliberately is a :class:`EpcError`. The split
that matters operationally is *configuration* (fail at startup, exit code 1,
nothing was touched) versus everything else (a single thread is lost, the run
continues, exit code 2).
"""

from typing import Any


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

    def __init__(self, message: str = "", *, status: int | None = None) -> None:
        super().__init__(message)
        # The HTTP status, when there was a response. This, and the exception's
        # type, are what a log line says about a failure; the message carries
        # request details and is not logged.
        self.status = status


class ClassificationError(EpcError):
    """A thread could not be classified. Costs that thread, not the run.

    Raised as-is for failures that say nothing about the thread — a timeout,
    throttling, an outage. The two subclasses below are the failures that do,
    and only they are remembered between runs.
    """

    def __init__(self, message: str = "", *, status: int | str | None = None, usage: Any = None) -> None:
        super().__init__(message)
        # HTTP status or provider error code. Logged in place of the message,
        # which can quote model output.
        self.status = status
        # Tokens the failed request still cost (`epc.classify.base.Usage`), when
        # the provider answered at all. A refused answer is billed like any other.
        self.usage = usage


class RejectedByProviderError(ClassificationError):
    """The provider refused this request as unacceptable (HTTP 400, 413, 422).

    Usually the content: a policy refusal, an input too long for the model. A
    misconfiguration produces the same statuses for *every* thread, which is why
    being refused is never enough on its own to skip a thread — see
    :mod:`epc.state`.
    """


class UnusableResponseError(ClassificationError):
    """The model answered, but not with a usable decision."""

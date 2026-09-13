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
    """A thread could not be classified. Costs that thread, not the run."""


class HistoryExpiredError(GmailError):
    """The stored `historyId` is older than Gmail's retention window.

    Recoverable rather than fatal: the caller falls back to a full scan and
    stores a fresh checkpoint. Gmail keeps roughly a week of history, so a tool
    that has not run for a while lands here as a matter of course.
    """

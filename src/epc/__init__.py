"""EmailPriorityClassifier — LLM-based Gmail thread triage."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("email-priority-classifier")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]

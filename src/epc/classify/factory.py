"""Selecting a classifier backend.

Imports are deferred on purpose. Each backend pulls in a different dependency —
the OpenAI SDK, boto3 — and the dependency split in `pyproject.toml` exists so
that the apply worker can install neither. Importing all three eagerly would
make the choice meaningless and the deployment fat.
"""

from epc.classify.base import Classifier
from epc.classify.prompt import PromptRenderer
from epc.errors import ConfigError
from epc.settings import Settings


def build_classifier(settings: Settings, renderer: PromptRenderer) -> Classifier:
    """Construct the classifier named by `settings.llm.backend`."""
    llm = settings.llm
    if not llm.model:
        raise ConfigError(
            f"llm.model is required for the {llm.backend!r} backend. Set it in the config file or pass EPC__LLM__MODEL."
        )

    match llm.backend:
        case "openai":
            from epc.classify.openai_backend import OpenAIClassifier

            return OpenAIClassifier(model=llm.model, renderer=renderer)

        case "local":
            from epc.classify.openai_backend import LocalClassifier

            # Validated by LlmSettings, restated so the type checker agrees.
            if not llm.base_url:  # pragma: no cover - unreachable via settings
                raise ConfigError("llm.base_url is required when llm.backend is 'local'")
            return LocalClassifier(model=llm.model, renderer=renderer, base_url=llm.base_url)

        case "bedrock":
            try:
                from epc.classify.bedrock_backend import BedrockClassifier
            except ImportError as exc:
                raise ConfigError("the bedrock backend needs boto3; install the 'aws' extra") from exc

            return BedrockClassifier(model=llm.model, renderer=renderer, region=llm.region)

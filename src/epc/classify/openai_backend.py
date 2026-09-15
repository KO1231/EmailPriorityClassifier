"""OpenAI, and OpenAI-compatible local servers.

These used to be one class. They are two, because they speak different APIs and
pretending otherwise would cost something real at each end:

* **OpenAI** goes through the **Responses API**. It is the current surface, and
  an API key scoped to `api.responses.write` can reach it without also being
  granted `model.request` — which is "call any model, any endpoint". Narrower is
  better for a key that lives in a secret store and runs unattended.
* **A local server** goes through **chat completions**. LM Studio, llama.cpp and
  vLLM implement that; most do not implement Responses.

Both constrain the reply with a strict JSON schema, and both hand the result to
the shared parser, so the enum stays the boundary either way.
"""

from typing import Any, cast

from openai import APIError, Omit, OpenAI, omit
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.completion_create_params import ResponseFormat
from openai.types.responses.response_text_config_param import ResponseTextConfigParam
from openai.types.shared_params import Reasoning

from epc.classify.base import ClassificationResult, Usage, parse_classification
from epc.classify.budget import ThreadPayload
from epc.classify.prompt import PromptRenderer, RenderedPrompt, response_json_schema
from epc.errors import ClassificationError, RejectedByProviderError

SCHEMA_NAME = "email_priority"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_MAX_OUTPUT_TOKENS = 2048

# Never ask the provider to retain the request. The payload is somebody's mail,
# and a 30-day retention window is not something to opt out of afterwards.
STORE_ON_PROVIDER = False


class _OpenAICompatibleClassifier:
    """Shared shape: render, call, parse. Subclasses supply the call."""

    backend_name = "openai"

    def __init__(
        self,
        *,
        model: str,
        renderer: PromptRenderer,
        client: OpenAI | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        # Retries live on the client rather than being hand-rolled: the SDK
        # already backs off on 429 and 5xx, and the previous implementation's
        # single attempt lost a thread to every transient error.
        self._client = client if client is not None else self._default_client(timeout, max_retries)
        self._model = model
        self._renderer = renderer
        self._reasoning_effort = reasoning_effort
        self._verbosity = verbosity
        self._max_output_tokens = max_output_tokens

    def _default_client(self, timeout: float, max_retries: int) -> OpenAI:
        return OpenAI(timeout=timeout, max_retries=max_retries)

    @property
    def backend(self) -> str:
        return self.backend_name

    @property
    def model(self) -> str:
        return self._model

    def _complete(self, rendered: RenderedPrompt) -> tuple[str, Usage]:
        raise NotImplementedError

    def classify(self, payload: ThreadPayload) -> ClassificationResult:
        rendered = self._renderer.render(payload)
        try:
            text, usage = self._complete(rendered)
        except APIError as exc:
            raise _as_classification_error(self.backend, exc) from exc

        try:
            classification = parse_classification(text)
        except ClassificationError as exc:
            # The answer was unusable, but it was an answer, and it was billed.
            exc.usage = usage
            raise

        return ClassificationResult(
            thread_id=payload.thread_id,
            classification=classification,
            usage=usage,
            backend=self.backend,
            model=self._model,
            prompt_version=rendered.prompt_version,
        )


class OpenAIClassifier(_OpenAICompatibleClassifier):
    """Classify through the OpenAI Responses API."""

    backend_name = "openai"

    def _text_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "name": SCHEMA_NAME,
                "strict": True,
                "schema": response_json_schema(),
            }
        }
        if self._verbosity is not None:
            config["verbosity"] = self._verbosity
        return config

    def _complete(self, rendered: RenderedPrompt) -> tuple[str, Usage]:
        # `omit` rather than None: a model that does not know the parameter
        # should not be handed it at all.
        reasoning: Reasoning | Omit = (
            Reasoning(effort=cast("Any", self._reasoning_effort)) if self._reasoning_effort is not None else omit
        )
        response = self._client.responses.create(
            model=self._model,
            instructions=rendered.system,
            input=rendered.user,
            text=cast("ResponseTextConfigParam", self._text_config()),
            max_output_tokens=self._max_output_tokens,
            store=STORE_ON_PROVIDER,
            reasoning=reasoning,
        )
        usage = response.usage
        return response.output_text or "", Usage(
            # Reasoning tokens are already counted inside output_tokens.
            input_tokens=int(usage.input_tokens) if usage else 0,
            output_tokens=int(usage.output_tokens) if usage else 0,
        )


class LocalClassifier(_OpenAICompatibleClassifier):
    """Classify through a local OpenAI-compatible server.

    The privacy-maximising option: nothing leaves the machine. Schema support
    varies between servers, so a rejected `json_schema` falls back to plain JSON
    mode rather than costing the thread.
    """

    backend_name = "local"

    def __init__(self, *, base_url: str, **options: Any) -> None:
        self._base_url = base_url
        super().__init__(**options)

    def _default_client(self, timeout: float, max_retries: int) -> OpenAI:
        # A local server needs no credential, but the SDK insists on one.
        return OpenAI(base_url=self._base_url, api_key="not-used", timeout=timeout, max_retries=max_retries)

    def _request(self, rendered: RenderedPrompt, response_format: dict[str, Any]) -> tuple[str, Usage]:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": rendered.system},
            {"role": "user", "content": rendered.user},
        ]
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            response_format=cast("ResponseFormat", response_format),
            max_completion_tokens=self._max_output_tokens,
            reasoning_effort=cast("Any", self._reasoning_effort or omit),
            verbosity=cast("Any", self._verbosity or omit),
        )
        choices = getattr(response, "choices", None) or []
        content = choices[0].message.content if choices else ""
        usage = getattr(response, "usage", None)
        return content or "", Usage(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )

    def _schema_format(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": SCHEMA_NAME,
                "strict": True,
                "schema": response_json_schema(),
            },
        }

    def _complete(self, rendered: RenderedPrompt) -> tuple[str, Usage]:
        try:
            return self._request(rendered, self._schema_format())
        except APIError:
            # The server does not implement schema enforcement. Ask for JSON and
            # let the shared parser carry the weight.
            return self._request(rendered, {"type": "json_object"})


# Statuses that describe the request rather than the service. Everything else —
# 401/403 credentials, 404 an unknown model, 408/429/5xx transient — says
# nothing about the thread and must not be remembered against it.
_REJECTED_STATUSES = frozenset({400, 413, 422})


def _as_classification_error(backend: str, exc: APIError) -> ClassificationError:
    status = getattr(exc, "status_code", None)
    kind = RejectedByProviderError if status in _REJECTED_STATUSES else ClassificationError
    return kind(f"{backend} request failed: {exc}", status=status)

"""OpenAI and OpenAI-compatible backends.

Structured Outputs do the constraining here: a `json_schema` with `strict: true`
means the response is schema-valid by construction, and the failure mode the old
implementation had — a markdown fence or a stray preamble losing the thread —
stops existing.

A local server behind the same API may or may not implement schema enforcement,
so :class:`LocalClassifier` asks for it and degrades to plain JSON mode when the
server rejects it. The shared parser in :mod:`epc.classify.base` is the backstop
either way.
"""

from openai import APIError, OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from openai.types.chat.completion_create_params import ResponseFormat

from epc.classify.base import Classification, ClassificationResult, Usage, parse_classification
from epc.classify.budget import ThreadPayload
from epc.classify.prompt import PromptRenderer, response_json_schema
from epc.errors import ClassificationError

SCHEMA_NAME = "email_priority"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 5
# The answer is four short fields; anything longer is the model going astray.
MAX_OUTPUT_TOKENS = 512


class OpenAIClassifier:
    """Classify via the OpenAI API."""

    backend_name = "openai"

    def __init__(
        self,
        *,
        model: str,
        renderer: PromptRenderer,
        client: OpenAI | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        # Retries are configured on the client rather than hand-rolled: the SDK
        # already backs off on 429 and 5xx, and the old implementation's single
        # attempt lost a thread to every transient error.
        self._client = client or OpenAI(timeout=timeout, max_retries=max_retries)
        self._model = model
        self._renderer = renderer

    @property
    def backend(self) -> str:
        return self.backend_name

    @property
    def model(self) -> str:
        return self._model

    def _response_format(self) -> ResponseFormat:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": SCHEMA_NAME,
                "strict": True,
                "schema": response_json_schema(),
            },
        }

    def _request(self, system: str, user: str, response_format: ResponseFormat) -> ChatCompletion:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            response_format=response_format,
            max_completion_tokens=MAX_OUTPUT_TOKENS,
        )

    def _complete(self, system: str, user: str) -> ChatCompletion:
        return self._request(system, user, self._response_format())

    def classify(self, payload: ThreadPayload) -> ClassificationResult:
        rendered = self._renderer.render(payload)
        try:
            response = self._complete(rendered.system, rendered.user)
        except APIError as exc:
            raise ClassificationError(f"{self.backend} request failed: {exc}") from exc

        return ClassificationResult(
            thread_id=payload.thread_id,
            classification=_parse_choice(response),
            usage=_usage_of(response),
            backend=self.backend,
            model=self._model,
            prompt_version=rendered.prompt_version,
        )


class LocalClassifier(OpenAIClassifier):
    """Classify via a local OpenAI-compatible server.

    The privacy-maximising option: nothing leaves the machine. Schema support
    varies across local servers, so a rejected `json_schema` falls back to plain
    JSON mode rather than failing the thread.
    """

    backend_name = "local"

    def __init__(
        self,
        *,
        model: str,
        renderer: PromptRenderer,
        base_url: str,
        client: OpenAI | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        super().__init__(
            model=model,
            renderer=renderer,
            # A local server needs no credential, but the SDK insists on one.
            client=client or OpenAI(base_url=base_url, api_key="not-used", timeout=timeout, max_retries=max_retries),
        )
        self._base_url = base_url

    def _complete(self, system: str, user: str) -> ChatCompletion:
        try:
            return self._request(system, user, self._response_format())
        except APIError:
            # The server does not implement schema enforcement. Ask for JSON and
            # let the shared parser and its validation carry the weight.
            return self._request(system, user, {"type": "json_object"})


def _parse_choice(response: ChatCompletion) -> Classification:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ClassificationError("the model returned no choices")
    content = getattr(choices[0].message, "content", None)
    return parse_classification(content or "")


def _usage_of(response: ChatCompletion) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )

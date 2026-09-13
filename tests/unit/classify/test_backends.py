"""Backend behaviour, against fakes. No network, no credentials, no cost."""

from pathlib import Path
from typing import Any, cast

import pytest
from openai import APIError

from epc.classify.base import Classifier
from epc.classify.bedrock_backend import TOOL_NAME, BedrockClassifier
from epc.classify.budget import build_payload
from epc.classify.factory import build_classifier
from epc.classify.openai_backend import LocalClassifier, OpenAIClassifier
from epc.classify.prompt import PromptRenderer
from epc.errors import ClassificationError, ConfigError
from epc.gmail.mime import parse_thread
from epc.priority import Priority
from epc.settings import BudgetSettings, load_settings
from tests.fixtures import gmail as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
ANSWER = '{"priority": "P1", "reason": "Deadline tomorrow", "confidence": 0.9, "signals": ["deadline"]}'


@pytest.fixture
def renderer() -> PromptRenderer:
    return PromptRenderer.load(REPO_ROOT / "prompts", Path("no-such-policy.yml"))


@pytest.fixture
def payload():  # type: ignore[no-untyped-def]
    raw = fx.thread(fx.message(fx.text_part("Please review the contract by tomorrow.")))
    return build_payload(parse_thread(raw), BudgetSettings(thread_tokens=4000, message_chars=2000))


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeResponses:
    """Enough of the Responses API to record the request and return a reply."""

    def __init__(self, content: str = ANSWER, error: Exception | None = None) -> None:
        self._content = content
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.responses = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None and len(self.calls) == 1:
            raise self._error
        usage = type("Usage", (), {"input_tokens": 120, "output_tokens": 30})()
        return type("Response", (), {"output_text": self._content, "usage": usage})()


class FakeChatCompletions:
    """Enough of chat completions for the local backend."""

    def __init__(self, content: str = ANSWER, error: Exception | None = None) -> None:
        self._content = content
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.chat = self

    @property
    def completions(self) -> FakeChatCompletions:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None and len(self.calls) == 1:
            raise self._error
        message = type("Message", (), {"content": self._content})()
        choice = type("Choice", (), {"message": message})()
        usage = type("Usage", (), {"prompt_tokens": 120, "completion_tokens": 30})()
        return type("Completion", (), {"choices": [choice], "usage": usage})()


def api_error(message: str) -> APIError:
    return APIError(message, request=None, body=None)  # type: ignore[arg-type]


class FakeBedrock:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def tool_response(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": {"message": {"content": [{"toolUse": {"name": TOOL_NAME, "input": arguments}}]}},
        "usage": {"inputTokens": 200, "outputTokens": 40},
    }


def text_response(text: str) -> dict[str, Any]:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": 200, "outputTokens": 40},
    }


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------


def test_openai_classifies_and_reports_provenance(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeResponses()
    result = OpenAIClassifier(model="test-model", renderer=renderer, client=cast(Any, client)).classify(payload)

    assert result.classification.priority is Priority.P1
    assert result.thread_id == payload.thread_id
    assert (result.backend, result.model) == ("openai", "test-model")
    assert result.prompt_version == renderer.version
    assert (result.usage.input_tokens, result.usage.output_tokens) == (120, 30)


def test_openai_constrains_the_response_to_the_schema(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeResponses()
    OpenAIClassifier(model="m", renderer=renderer, client=cast(Any, client)).classify(payload)

    text_config = client.calls[0]["text"]
    assert text_config["format"]["type"] == "json_schema"
    assert text_config["format"]["strict"] is True
    assert text_config["format"]["schema"]["properties"]["priority"]["enum"] == ["P1", "P2", "P3"]


def test_openai_sends_the_rendered_prompts(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeResponses()
    OpenAIClassifier(model="m", renderer=renderer, client=cast(Any, client)).classify(payload)

    call = client.calls[0]
    assert "classify email threads" in call["instructions"].lower()
    assert "untrusted_email_content" in call["input"]
    # The payload is somebody's mail; the provider is not asked to keep it.
    assert call["store"] is False


def test_an_api_error_becomes_a_classification_error(renderer: PromptRenderer, payload: Any) -> None:
    """One thread is lost, not the run."""
    client = FakeResponses(error=api_error("boom"))
    with pytest.raises(ClassificationError, match="openai request failed"):
        OpenAIClassifier(model="m", renderer=renderer, client=cast(Any, client)).classify(payload)


def test_an_unusable_reply_is_a_classification_error(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeResponses(content="I would rather not.")
    with pytest.raises(ClassificationError):
        OpenAIClassifier(model="m", renderer=renderer, client=cast(Any, client)).classify(payload)


# --------------------------------------------------------------------------
# Local
# --------------------------------------------------------------------------


def test_local_falls_back_when_the_server_rejects_the_schema(renderer: PromptRenderer, payload: Any) -> None:
    """Schema support varies across local servers; a thread should not be lost
    because one of them has not implemented it."""
    client = FakeChatCompletions(error=api_error("unknown response_format"))
    result = LocalClassifier(
        model="m", renderer=renderer, base_url="http://localhost:1234/v1", client=cast(Any, client)
    ).classify(payload)

    assert result.classification.priority is Priority.P1
    assert result.backend == "local"
    assert client.calls[0]["response_format"]["type"] == "json_schema"
    assert client.calls[1]["response_format"] == {"type": "json_object"}


def test_local_asks_for_the_schema_first(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeChatCompletions()
    LocalClassifier(
        model="m", renderer=renderer, base_url="http://localhost:1234/v1", client=cast(Any, client)
    ).classify(payload)
    assert len(client.calls) == 1


# --------------------------------------------------------------------------
# Bedrock
# --------------------------------------------------------------------------


def test_bedrock_reads_the_answer_out_of_a_tool_call(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeBedrock(tool_response({"priority": "P1", "reason": "urgent", "confidence": 0.8}))
    result = BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client)).classify(payload)

    assert result.classification.priority is Priority.P1
    assert result.backend == "bedrock"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (200, 40)


def test_bedrock_forces_the_tool_when_it_can(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeBedrock(tool_response({"priority": "P2"}))
    BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client)).classify(payload)
    assert client.calls[0]["toolConfig"]["toolChoice"] == {"tool": {"name": TOOL_NAME}}


def test_bedrock_degrades_when_forcing_a_tool_is_unsupported(renderer: PromptRenderer, payload: Any) -> None:
    """Some models reject a forced toolChoice outright. Ask, do not insist."""
    rejection = Exception("ValidationException: toolChoice is not supported for this model")
    client = FakeBedrock(rejection, tool_response({"priority": "P3"}))
    classifier = BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client))

    result = classifier.classify(payload)
    assert result.classification.priority is Priority.P3
    assert client.calls[1]["toolConfig"]["toolChoice"] == {"auto": {}}


def test_bedrock_remembers_that_forcing_is_unsupported(renderer: PromptRenderer, payload: Any) -> None:
    """Otherwise every one of 1500 threads pays for a doomed first attempt."""
    rejection = Exception("ValidationException: toolChoice is not supported for this model")
    client = FakeBedrock(rejection, tool_response({"priority": "P3"}), tool_response({"priority": "P2"}))
    classifier = BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client))

    classifier.classify(payload)
    classifier.classify(payload)
    assert len(client.calls) == 3  # not four
    assert client.calls[2]["toolConfig"]["toolChoice"] == {"auto": {}}


def test_a_failed_bedrock_retry_is_a_classification_error(renderer: PromptRenderer, payload: Any) -> None:
    """The degraded retry is a request like any other. Its failure used to
    escape as a raw ClientError and take the run with it."""
    rejection = Exception("ValidationException: toolChoice is not supported for this model")
    client = FakeBedrock(rejection, Exception("ThrottlingException: slow down"))
    classifier = BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client))

    with pytest.raises(ClassificationError, match="ThrottlingException"):
        classifier.classify(payload)
    assert len(client.calls) == 2


def test_bedrock_falls_back_to_reading_plain_text(renderer: PromptRenderer, payload: Any) -> None:
    """A model that answers in prose instead of calling the tool still counts."""
    client = FakeBedrock(text_response(ANSWER))
    result = BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client)).classify(payload)
    assert result.classification.priority is Priority.P1


def test_bedrock_validates_tool_arguments_like_any_other_output(renderer: PromptRenderer, payload: Any) -> None:
    """A schema on the request is a request, not a guarantee."""
    client = FakeBedrock(tool_response({"priority": "URGENT"}))
    with pytest.raises(ClassificationError, match="valid priority"):
        BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client)).classify(payload)


def test_bedrock_reports_an_empty_reply(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeBedrock({"output": {"message": {"content": []}}, "usage": {}})
    with pytest.raises(ClassificationError, match="neither a tool call nor any text"):
        BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client)).classify(payload)


def test_a_non_toolchoice_error_is_not_retried(renderer: PromptRenderer, payload: Any) -> None:
    client = FakeBedrock(Exception("AccessDeniedException: not authorised"))
    with pytest.raises(ClassificationError, match="bedrock request failed"):
        BedrockClassifier(model="any.model", renderer=renderer, client=cast(Any, client)).classify(payload)
    assert len(client.calls) == 1


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def settings_for(tmp_path: Path, llm: str) -> Any:
    path = tmp_path / "config.yml"
    path.write_text(f'labels: {{p1: "a", p2: "b", p3: "c"}}\nllm: {{{llm}}}\n', encoding="utf-8")
    return load_settings(path)


def test_every_backend_satisfies_the_protocol(renderer: PromptRenderer) -> None:
    assert isinstance(OpenAIClassifier(model="m", renderer=renderer, client=cast(Any, FakeResponses())), Classifier)
    assert isinstance(BedrockClassifier(model="m", renderer=renderer, client=cast(Any, FakeBedrock())), Classifier)


def test_the_factory_requires_a_model(tmp_path: Path, renderer: PromptRenderer) -> None:
    with pytest.raises(ConfigError, match=r"llm\.model is required"):
        build_classifier(settings_for(tmp_path, "backend: openai"), renderer)


def test_the_local_backend_requires_a_base_url(tmp_path: Path) -> None:
    """Caught by config validation, before any command runs."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="base_url"):
        settings_for(tmp_path, "backend: local, model: m")

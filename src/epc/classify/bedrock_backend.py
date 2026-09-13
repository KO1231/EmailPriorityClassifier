"""Amazon Bedrock, via the Converse API.

Converse is used rather than an Anthropic-specific client on purpose. Converse
is the provider-agnostic surface: the same request shape reaches Claude, Nova,
Llama, Mistral and the OpenAI open-weight models on Bedrock. A vendor-specific
client would be a better fit for one family and a dead end for the rest, which
defeats the point of having a swappable backend at all.

Structured output is requested through a tool definition, because that is how
Converse expresses a schema. It is requested, not depended upon: support for
*forcing* a tool varies by model and some newer ones reject it outright, so a
rejection degrades to letting the model choose, and then to reading plain text.
The shared parser in :mod:`epc.classify.base` decides what is acceptable, so the
enum stays the boundary regardless of how the answer arrived.

`boto3` lives in the `aws` extra, so this module is imported only when the
backend is selected.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from epc.classify.base import Classification, ClassificationResult, Usage, parse_classification
from epc.classify.budget import ThreadPayload
from epc.classify.prompt import PromptRenderer, response_json_schema
from epc.errors import ClassificationError, RejectedByProviderError, UnusableResponseError

if TYPE_CHECKING:  # pragma: no cover - stubs are a dev dependency, not a runtime one
    from mypy_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from mypy_boto3_bedrock_runtime.type_defs import (
        ConverseResponseTypeDef,
        ToolConfigurationTypeDef,
    )
else:
    BedrockRuntimeClient = Any

TOOL_NAME = "record_priority"
TOOL_DESCRIPTION = "Record the priority decided for this email thread."
MAX_OUTPUT_TOKENS = 512
# Classification wants the same answer for the same thread, every time.
TEMPERATURE = 0.0


class BedrockClassifier:
    """Classify via Bedrock's Converse API."""

    backend_name = "bedrock"

    def __init__(
        self,
        *,
        model: str,
        renderer: PromptRenderer,
        region: str | None = None,
        client: BedrockRuntimeClient | None = None,
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=region)
        self._client = client
        self._model = model
        self._renderer = renderer
        # Latched after the first rejection so a model that cannot be forced is
        # not asked again on every one of 1500 threads.
        self._can_force_tool = True

    @property
    def backend(self) -> str:
        return self.backend_name

    @property
    def model(self) -> str:
        return self._model

    def _tool_config(self, *, force: bool) -> ToolConfigurationTypeDef:
        config: dict[str, Any] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": {"json": response_json_schema()},
                    }
                }
            ]
        }
        config["toolChoice"] = {"tool": {"name": TOOL_NAME}} if force else {"auto": {}}
        return cast("ToolConfigurationTypeDef", config)

    def _converse(self, system: str, user: str, *, force: bool) -> ConverseResponseTypeDef:
        return self._client.converse(
            modelId=self._model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": TEMPERATURE},
            toolConfig=self._tool_config(force=force),
        )

    def classify(self, payload: ThreadPayload) -> ClassificationResult:
        rendered = self._renderer.render(payload)

        try:
            response = self._converse(rendered.system, rendered.user, force=self._can_force_tool)
        except Exception as exc:
            if not (self._can_force_tool and _is_unsupported_tool_choice(exc)):
                raise _as_classification_error(self.backend, exc) from exc
            # This model does not allow a forced tool. Ask, don't insist.
            self._can_force_tool = False
            try:
                response = self._converse(rendered.system, rendered.user, force=False)
            except Exception as retry_exc:
                # The retry is a request like any other, and fails like one:
                # throttling, a timeout, a model that is not enabled.
                raise _as_classification_error(self.backend, retry_exc) from retry_exc

        return ClassificationResult(
            thread_id=payload.thread_id,
            classification=_parse_converse(response),
            usage=_usage_of(response),
            backend=self.backend,
            model=self._model,
            prompt_version=rendered.prompt_version,
        )


def _is_unsupported_tool_choice(exc: Exception) -> bool:
    """Whether Bedrock refused the request because of `toolChoice`.

    Matched on the message rather than an exception class: botocore raises the
    same `ClientError` for every validation failure, and the distinction that
    matters is only in the text.
    """
    message = str(exc).lower()
    return "validationexception" in message and "toolchoice" in message


def _as_classification_error(backend: str, exc: Exception) -> ClassificationError:
    """Name the failure for what it says about the thread.

    `ValidationException` is Bedrock's refusal of the request itself: content
    it will not process, an input too long for the model. Throttling, access
    and availability errors say nothing about the thread.
    """
    kind = RejectedByProviderError if "validationexception" in str(exc).lower() else ClassificationError
    return kind(f"{backend} request failed: {exc}")


def _parse_converse(response: Mapping[str, Any]) -> Classification:
    """Read the answer out of whichever shape the model chose to reply in."""
    content = response.get("output", {}).get("message", {}).get("content") or []

    for block in content:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == TOOL_NAME:
            arguments = tool_use.get("input")
            if isinstance(arguments, dict):
                return _from_arguments(arguments)

    text = "\n".join(block["text"] for block in content if "text" in block)
    if not text.strip():
        raise UnusableResponseError("the model returned neither a tool call nor any text")
    return parse_classification(text)


def _from_arguments(arguments: dict[str, Any]) -> Classification:
    """Validate tool arguments through the same path as raw text.

    A schema on the request is a request, not a guarantee — the arguments are
    still model output, so they go through the same check.
    """
    import json

    return parse_classification(json.dumps(arguments))


def _usage_of(response: Mapping[str, Any]) -> Usage:
    usage = response.get("usage") or {}
    return Usage(
        input_tokens=int(usage.get("inputTokens") or 0),
        output_tokens=int(usage.get("outputTokens") or 0),
    )

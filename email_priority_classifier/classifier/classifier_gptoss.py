import json
import logging
import os
from pathlib import Path
from typing import NamedTuple

from openai import OpenAI

from email_priority_classifier.classifier import EmailPriorityClassifier
from email_priority_classifier.exception import EmailPriorityClassifierOpenAIException
from email_priority_classifier.type import ClassifiedEmailData, EmailPriority
from email_priority_classifier.util.logger_util import setup_logger

_CLIENT = OpenAI(
    base_url=f"http://localhost:{os.environ['LOCAL_LM_PORT']}/v1",
    api_key="dummy"
)
logger = setup_logger("classifier_gptoss", logging.DEBUG)


class GPTOSSPromptInfo(NamedTuple):
    system_template: str
    user_template: str


class ClassifierGPTOSS(EmailPriorityClassifier):
    def __init__(self, system_prompt_template_file: Path, user_prompt_template_file: Path):
        if (not system_prompt_template_file.is_file()) or (not user_prompt_template_file.is_file()):
            raise FileNotFoundError("Prompt template file not found.")

        self._prompt_info = GPTOSSPromptInfo(
            system_template=system_prompt_template_file.read_text(encoding='utf-8'),
            user_template=user_prompt_template_file.read_text(encoding='utf-8')
        )
        self._model = "openai/gpt-oss-20b"

    @staticmethod
    def _create_request_prompt(self, prompt_info: GPTOSSPromptInfo, subject: str, messages: str) -> dict[str, str]:
        return {
            "system_template": prompt_info.system_template.replace("{{thread_subject}}", subject).replace("{{thread_messages}}", messages),
            "user_template": prompt_info.user_template.replace("{{thread_subject}}", subject).replace("{{thread_messages}}", messages),
        }

    def calc(self, thread_messages: list[ClassifiedEmailData]) -> EmailPriority:
        logger.debug("GPTOSS ------")
        try:
            request_prompt = self._create_request_prompt(self, self._prompt_info,
                                                         thread_messages[0].subject, super()._encode_thread_messages(thread_messages)[:1000])
            request_input = [
                {
                    "role": "developer",
                    "content": request_prompt["system_template"]
                },
                {
                    "role": "user",
                    "content": request_prompt["user_template"]
                }
            ]
            response = _CLIENT.responses.create(
                model=self._model,
                # reasoning={"effort": "minimal"},
                # text={"verbosity": "low"},
                input=request_input
            )

            logger.debug(f"GPTOSS request_data: {json.dumps(request_input, indent=2)}")
        except Exception as e:
            raise EmailPriorityClassifierOpenAIException("Some error occurred while calling the GPTOSS API.") from e

        try:
            raw_response = json.loads(response.output_text)
            result = raw_response["priority"]
            logger.debug(f"GPTOSS response: {json.dumps(raw_response, indent=2)}")
            return EmailPriority[result]
        except Exception:
            raise EmailPriorityClassifierOpenAIException("Failed to parse the response from GPTOSS API.")

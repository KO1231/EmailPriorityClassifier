import json
import logging
import os
from typing import NamedTuple

from openai import OpenAI

from email_priority_classifier.classifier import EmailPriorityClassifier
from email_priority_classifier.exception import EmailPriorityClassifierOpenAIException
from email_priority_classifier.type import ClassifiedEmailData, EmailPriority
from email_priority_classifier.util.logger_util import setup_logger

_CLIENT = OpenAI()
logger = setup_logger("classifier_openai", logging.INFO)


class OpenAIPromptInfo(NamedTuple):
    id: str
    version: str


class ClassifierOpenAI(EmailPriorityClassifier):
    def __init__(self):
        self._prompt_info = OpenAIPromptInfo(str(os.environ['OPENAI_PROMPT_ID']), str(os.environ['OPENAI_PROMPT_VERSION']))

    def calc(self, thread_messages: list[ClassifiedEmailData]) -> EmailPriority:
        logger.debug("OpenAI ------")
        try:
            data = {
                "thread_subject": thread_messages[0].subject,
                "thread_messages": super()._encode_thread_messages(thread_messages)[:1500],  # 大体4000前後input-tokenくらい
            }
            response = _CLIENT.responses.create(
                # model="gpt-5-nano",
                # reasoning={"effort": "minimal"},
                # text={"verbosity": "low"},
                prompt={
                    "id": self._prompt_info.id,
                    "version": self._prompt_info.version,
                    "variables": data,
                }
            )

            logger.debug(f"OpenAPI request_data: {json.dumps(data, indent=2, ensure_ascii=False)}")
        except Exception as e:
            raise EmailPriorityClassifierOpenAIException("Some error occurred while calling the OpenAI API.") from e

        try:
            raw_response = json.loads(response.output_text)
            result = raw_response["priority"]
            logger.debug(f"OpenAPI response: {json.dumps(raw_response, indent=2)}")
            return EmailPriority[result]
        except Exception:
            raise EmailPriorityClassifierOpenAIException("Failed to parse the response from OpenAI API.")

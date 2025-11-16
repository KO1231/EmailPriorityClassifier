import json
from abc import ABC, abstractmethod

from email_priority_classifier.type import ClassifiedEmailData, EmailPriority
from email_priority_classifier.type.classified_email_data import ClassifiedEmailDataEncoder


class EmailPriorityClassifier(ABC):
    @staticmethod
    def _encode_thread_messages(thread_messages: list[ClassifiedEmailData]) -> str:
        labels = set(sum([m.labels for m in thread_messages], []))
        return json.dumps({
            "labels": list(labels),
            "messages": thread_messages
        }, cls=ClassifiedEmailDataEncoder)

    @abstractmethod
    def calc(self, thread_messages: list[ClassifiedEmailData]) -> EmailPriority:
        pass

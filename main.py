import os
import time
from pathlib import Path

from googleapiclient.discovery import build, Resource

from email_priority_classifier.classifier import EmailPriorityClassifier
from email_priority_classifier.classifier.classifier_openai import ClassifierOpenAI
from email_priority_classifier.config import load_config
from email_priority_classifier.exception import EmailPriorityClassifierGmailAPIException
from email_priority_classifier.gmail_credentials import get_credential
from email_priority_classifier.type.classified_email_data import ClassifiedEmailData
from email_priority_classifier.type.priority import EmailPriority
from email_priority_classifier.util.logger_util import setup_logger

CLIENT_SECRETS_FILE = Path(__file__).resolve().parent / "secrets" / "client_secrets.json"
TOKEN_FILE = Path(__file__).resolve().parent / "secrets" / "token.pickle"
CONFIG_FILE = Path(__file__).resolve().parent / "config.yml"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

logger = setup_logger("main")


def get_thread_messages(service: Resource, thread_id: str) -> list[dict]:
    try:
        thread = service.users().threads().get(
            userId="me",
            id=thread_id,
            format="full",
            fields="id,messages(id,threadId,labelIds,payload,sizeEstimate,internalDate)"
        ).execute()

        if "id" not in thread or thread["id"] != thread_id:
            raise ValueError(f"Thread ID mismatch. Expected: {thread_id}, Actual: {thread.get('id', "null")}")
        if "messages" not in thread:
            raise ValueError(f"No messages found in thread. (threadId: {thread_id})")
        return thread.get("messages", [])
    except Exception as e:
        raise EmailPriorityClassifierGmailAPIException(
            f"Some error occurred while getting thread messages. (threadId: {thread_id})") from e


def classify(service: Resource, classifier: EmailPriorityClassifier, parameter_label_names: list[str], personal_label_info: dict[str, str], *,
             max_threads: int | None = None, rate_limit_in_min: int | None = None) -> dict[
    EmailPriority, list[str]]:
    result = {EmailPriority.P1: [], EmailPriority.P2: [], EmailPriority.P3: []}

    try:
        request = service.users().threads().list(
            userId="me",
            fields="threads(id),nextPageToken",
            includeSpamTrash=False,
            q=f"(in:inbox) AND NOT({" OR ".join([f"label:{label_name}" for label_name in parameter_label_names])})"
        )

        processed_threads = 0
        while request is not None:
            # Fetch Threads
            response = request.execute()
            # Loop thread and classify
            threads = response.get("threads", [])
            for thread in threads:
                thread_id = thread["id"]
                thread_messages = [ClassifiedEmailData.init(m, personal_label_info) for m in get_thread_messages(service, thread_id)]
                priority = classifier.calc(thread_messages)
                result[priority].append(thread_id)

                processed_threads += 1
                if max_threads is not None and processed_threads >= max_threads:
                    return result
                if rate_limit_in_min:
                    time.sleep(60 / rate_limit_in_min)
            # Get next page
            request = service.users().threads().list_next(previous_request=request, previous_response=response)

    except Exception as e:
        raise EmailPriorityClassifierGmailAPIException("Some error occurred while listing threads.") from e

    return result


def modify_thread_label(service: Resource, thread_id: str, priority_label_id: str):
    try:
        service.users().threads().modify(
            userId="me",
            id=thread_id,
            body={
                "addLabelIds": [priority_label_id],
                "removeLabelIds": []
            }
        ).execute()
    except Exception as e:
        raise EmailPriorityClassifierGmailAPIException(
            f"Some error occurred while modifying thread label. (threadId: {thread_id}, labelId: {priority_label_id})") from e


def fetch_personal_label_info(service: Resource) -> dict[str, str]:
    return {label["id"]: label["name"] for label in
            service.users().labels().list(userId="me").execute()["labels"] if label["id"].startswith("Label_")}


def main():
    config = load_config(str(CONFIG_FILE))
    service = build("gmail", "v1",
                    credentials=get_credential(str(CLIENT_SECRETS_FILE), str(TOKEN_FILE)),
                    cache_discovery=False)

    # ユーザー作成のラベル情報を取得
    personal_labels_info = fetch_personal_label_info(service)

    # Classifierのインスタンスを生成
    classifier: EmailPriorityClassifier = ClassifierOpenAI()

    # PriorityがないメールをP1, P2, P3に分類
    max_threads = int(os.environ["MAX_THREADS"]) if "MAX_THREADS" in os.environ else None
    if max_threads is not None and max_threads <= 0:
        raise ValueError(f"max_threads must be positive, got {max_threads}")
    rate_limit_in_min = int(os.environ["RATE_LIMIT_REQUESTS_PER_MINUTE"]) if "RATE_LIMIT_REQUESTS_PER_MINUTE" in os.environ else None
    if rate_limit_in_min is not None and rate_limit_in_min <= 0:
        raise ValueError(f"rate_limit_in_min must be positive, got {rate_limit_in_min}")
    classify_result = classify(service, classifier, config.parameter_labels, personal_labels_info,
                               max_threads=max_threads, rate_limit_in_min=rate_limit_in_min)

    # Priorityごとにスレッドにラベルを付与
    for priority, thread_ids in classify_result.items():
        label_id = config.label_id[priority]
        for thread_id in thread_ids:
            if os.environ.get("DEV_NOT_MODIFY", "false").lower() == "true":
                logger.info(f"(DEV MODE) Would modify thread label: {label_id} (Priority: {priority.name})")
                continue
            modify_thread_label(service, thread_id, label_id)
            logger.info(f"Modified thread label: {thread_id} (Priority: {priority.name})")


if __name__ == "__main__":
    # Classifierのインスタンスを生成
    classifier: EmailPriorityClassifier = ClassifierOpenAI()
    # classifier: EmailPriorityClassifier = ClassifierGPTOSS(PROMPTS_DIR / "gptoss_system_prompt.txt", PROMPTS_DIR / "gptoss_user_prompt.txt")
    main(classifier)

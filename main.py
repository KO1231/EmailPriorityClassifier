import os
import time
from functools import partial
from multiprocessing import Pool
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


def _classify_worker(thread_data: tuple[str, list[ClassifiedEmailData]], classifier: EmailPriorityClassifier) -> tuple[str, EmailPriority]:
    """マルチプロセスで実行されるワーカー関数"""
    thread_id, thread_messages = thread_data
    priority = classifier.calc(thread_messages)
    return thread_id, priority


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
             max_threads: int | None = None, rate_limit_in_min: int | None = None, concurrency: int = 1) -> dict[
    EmailPriority, list[str]]:
    result = {EmailPriority.P1: [], EmailPriority.P2: [], EmailPriority.P3: []}

    try:
        request = service.users().threads().list(
            userId="me",
            fields="threads(id),nextPageToken",
            includeSpamTrash=False,
            maxResults=min(500, max_threads),
            q=f"(in:inbox) AND NOT({" OR ".join([f"label:{label_name}" for label_name in parameter_label_names])})"
        )

        processed_threads = 0

        # マルチプロセスのためのプールを作成
        use_multiprocessing = concurrency > 1
        pool = Pool(processes=concurrency) if use_multiprocessing else None

        try:
            while request is not None:
                # Fetch Threads
                response = request.execute()
                threads = response.get("threads", [])

                # スレッドデータを準備
                thread_data_list = []
                for thread in threads:
                    thread_id = thread["id"]
                    thread_messages = [ClassifiedEmailData.init(m, personal_label_info) for m in get_thread_messages(service, thread_id)]
                    thread_data_list.append((thread_id, thread_messages))

                # 分類を実行
                if use_multiprocessing:
                    # マルチプロセスで並行処理
                    worker_func = partial(_classify_worker, classifier=classifier)

                    # レート制限を考慮してバッチサイズを調整
                    batch_size = concurrency
                    for i in range(0, len(thread_data_list), batch_size):
                        batch = thread_data_list[i:i + batch_size]
                        batch_results = pool.map(worker_func, batch)

                        for thread_id, priority in batch_results:
                            result[priority].append(thread_id)
                            processed_threads += 1

                            if max_threads is not None and processed_threads >= max_threads:
                                return result

                        # レート制限: バッチ処理後に待機
                        # concurrency個のリクエストを並行実行したので、その分の時間を待つ
                        if rate_limit_in_min and i + batch_size < len(thread_data_list):
                            time.sleep(60 * len(batch) / rate_limit_in_min)
                else:
                    # シングルプロセスで順次処理
                    for thread_id, thread_messages in thread_data_list:
                        priority = classifier.calc(thread_messages)
                        result[priority].append(thread_id)
                        processed_threads += 1

                        if max_threads is not None and processed_threads >= max_threads:
                            return result
                        if rate_limit_in_min:
                            time.sleep(60 / rate_limit_in_min)

                # Get next page
                request = service.users().threads().list_next(previous_request=request, previous_response=response)
        finally:
            if pool is not None:
                pool.close()
                pool.join()

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


def main(classifier: EmailPriorityClassifier):
    config = load_config(str(CONFIG_FILE))
    service = build("gmail", "v1",
                    credentials=get_credential(str(CLIENT_SECRETS_FILE), str(TOKEN_FILE)),
                    cache_discovery=False)

    # ユーザー作成のラベル情報を取得
    personal_labels_info = fetch_personal_label_info(service)

    # PriorityがないメールをP1, P2, P3に分類
    max_threads = int(os.environ["MAX_THREADS"]) if "MAX_THREADS" in os.environ else None
    if max_threads is not None and max_threads <= 0:
        raise ValueError(f"max_threads must be positive, got {max_threads}")
    rate_limit_in_min = int(os.environ["RATE_LIMIT_REQUESTS_PER_MINUTE"]) if "RATE_LIMIT_REQUESTS_PER_MINUTE" in os.environ else None
    if rate_limit_in_min is not None and rate_limit_in_min <= 0:
        raise ValueError(f"rate_limit_in_min must be positive, got {rate_limit_in_min}")
    concurrency = int(os.environ.get("CONCURRENCY", "1"))
    if concurrency <= 0:
        raise ValueError(f"concurrency must be positive, got {concurrency}")
    classify_result = classify(service, classifier, config.parameter_labels, personal_labels_info,
                               max_threads=max_threads, rate_limit_in_min=rate_limit_in_min, concurrency=concurrency)

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

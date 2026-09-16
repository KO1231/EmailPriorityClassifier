"""A thin, typed wrapper over the Gmail API.

Only the calls that have a caller are here; the thread and message operations
arrive with the pipeline.

Two properties this wrapper is responsible for:

* **Retries.** `google-api-python-client` implements exponential backoff behind
  `execute(num_retries=…)`. The previous implementation passed nothing, so a
  single 429 during a 1500-thread run cost that thread outright.
* **Errors that name the operation.** An `HttpError` becomes a
  :class:`~epc.errors.GmailError` that says what was being attempted — and so
  does a failure below HTTP. `execute(num_retries=…)` retries a dropped
  connection or a socket timeout, then re-raises it as whatever the transport
  raised: `TimeoutError`, `ssl.SSLError`, `httplib2.ServerNotFoundError`,
  `http.client.IncompleteRead`, a token refresh that could not reach Google. Callers catch `GmailError` to lose
  one thread rather than the run, so every one of those has to arrive as one.

.. warning::
   The ``http`` object inside a built service is **not thread-safe**. Build one
   :class:`GmailClient` per worker thread rather than sharing one.
"""

import http.client
from collections.abc import Iterator, Sequence
from typing import Any

import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from epc.errors import GmailError
from epc.gmail.labels import GmailLabel, parse_labels
from epc.gmail.models import ThreadRef

DEFAULT_RETRIES = 5
# `messages.batchModify` accepts up to 1000 IDs per call. The previous
# implementation issued one `threads.modify` per thread, so a 1500-thread run
# made 1500 round trips where this makes two.
BATCH_MODIFY_LIMIT = 1000
# `threads.list` caps a page at 500.
THREAD_PAGE_LIMIT = 500

# What the transport raises once `execute`'s own retries are spent. `OSError`
# covers sockets, timeouts and TLS. `http.client.HTTPException` covers a response
# cut short or malformed (`IncompleteRead`, `BadStatusLine`), which httplib2
# re-raises as it found it and which is not an `OSError`. The other two are the
# HTTP library's and google-auth's own hierarchies.
_TRANSPORT_ERRORS = (OSError, http.client.HTTPException, httplib2.HttpLib2Error, GoogleAuthError)


class GmailClient:
    """Gmail operations for a single authenticated user."""

    def __init__(self, credentials: Credentials, *, num_retries: int = DEFAULT_RETRIES) -> None:
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        self._num_retries = num_retries

    def _execute(self, request: Any, *, operation: str) -> dict[str, Any]:
        try:
            result = request.execute(num_retries=self._num_retries)
        except HttpError as exc:
            raise GmailError(f"{operation} failed: {exc}", status=exc.resp.status) from exc
        except _TRANSPORT_ERRORS as exc:
            raise GmailError(f"{operation} failed: {type(exc).__name__}: {exc}") from exc
        return result if isinstance(result, dict) else {}

    def list_labels(self) -> list[GmailLabel]:
        """Every label in the mailbox, system and user alike."""
        response = self._execute(
            self._service.users().labels().list(userId="me"),
            operation="listing labels",
        )
        return parse_labels(response.get("labels") or [])

    def create_label(self, name: str) -> GmailLabel:
        """Create a visible user label and return it."""
        response = self._execute(
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ),
            operation=f"creating label {name!r}",
        )
        return GmailLabel(
            id=str(response.get("id") or ""),
            name=str(response.get("name") or name),
            type=str(response.get("type") or "user"),
        )

    def list_threads(self, query: str, *, limit: int) -> Iterator[ThreadRef]:
        """Threads matching `query`, newest first, up to `limit`.

        Only IDs and history IDs are requested. Fetching full threads during
        listing would pay for every thread on the page, including the ones
        `limit` cuts off.
        """
        request: Any | None = (
            self._service.users()
            .threads()
            .list(
                userId="me",
                q=query,
                fields="threads(id,historyId),nextPageToken",
                includeSpamTrash=False,
                maxResults=min(THREAD_PAGE_LIMIT, limit),
            )
        )
        yielded = 0
        while request is not None and yielded < limit:
            response = self._execute(request, operation="listing threads")
            for thread in response.get("threads") or []:
                identifier = str(thread.get("id") or "")
                if identifier:
                    yield ThreadRef(id=identifier, history_id=str(thread.get("historyId") or ""))
                    yielded += 1
                    if yielded >= limit:
                        return
            request = self._service.users().threads().list_next(request, response)

    def thread_label_ids(self, thread_id: str) -> set[str]:
        """The labels on any message of a thread, without fetching the messages."""
        response = self._execute(
            self._service.users()
            .threads()
            .get(userId="me", id=thread_id, format="minimal", fields="messages(labelIds)"),
            operation=f"reading the labels of thread {thread_id}",
        )
        return {str(label) for message in response.get("messages") or [] for label in message.get("labelIds") or []}

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        """One thread with full message payloads.

        `format="full"` rather than `"raw"`: it omits attachment bytes, and at
        1500 threads a run that downloads every PDF is not viable.
        """
        return self._execute(
            self._service.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="full",
                fields="id,messages(id,threadId,labelIds,payload,sizeEstimate,internalDate)",
            ),
            operation=f"fetching thread {thread_id}",
        )

    def batch_modify(
        self,
        message_ids: Sequence[str],
        *,
        add_label_ids: Sequence[str] = (),
        remove_label_ids: Sequence[str] = (),
    ) -> None:
        """Apply one label change to up to `BATCH_MODIFY_LIMIT` messages.

        Returns nothing on success — Gmail's response body is empty, so there is
        no per-message outcome to report. Chunking is the caller's job.
        """
        if not message_ids or (not add_label_ids and not remove_label_ids):
            return
        if len(message_ids) > BATCH_MODIFY_LIMIT:
            raise ValueError(f"batch_modify takes at most {BATCH_MODIFY_LIMIT} message IDs")

        self._execute(
            self._service.users()
            .messages()
            .batchModify(
                userId="me",
                body={
                    "ids": list(message_ids),
                    "addLabelIds": list(add_label_ids),
                    "removeLabelIds": list(remove_label_ids),
                },
            ),
            operation=f"modifying labels on {len(message_ids)} messages",
        )

    def mailbox_address(self) -> str:
        """The address of the authenticated mailbox."""
        response = self._execute(
            self._service.users().getProfile(userId="me", fields="emailAddress"),
            operation="reading the mailbox profile",
        )
        return str(response.get("emailAddress") or "")

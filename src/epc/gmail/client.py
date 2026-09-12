"""A thin, typed wrapper over the Gmail API.

Only the calls that have a caller are here; the thread and message operations
arrive with the pipeline.

Two properties this wrapper is responsible for:

* **Retries.** `google-api-python-client` implements exponential backoff behind
  `execute(num_retries=…)`. The previous implementation passed nothing, so a
  single 429 during a 1500-thread run cost that thread outright.
* **Errors that name the operation.** An `HttpError` becomes a
  :class:`~epc.errors.GmailError` that says what was being attempted.

.. warning::
   The ``http`` object inside a built service is **not thread-safe**. Build one
   :class:`GmailClient` per worker thread rather than sharing one.
"""

from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from epc.errors import GmailError
from epc.gmail.labels import GmailLabel, parse_labels

DEFAULT_RETRIES = 5


class GmailClient:
    """Gmail operations for a single authenticated user."""

    def __init__(self, credentials: Credentials, *, num_retries: int = DEFAULT_RETRIES) -> None:
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        self._num_retries = num_retries

    def _execute(self, request: Any, *, operation: str) -> dict[str, Any]:
        try:
            result = request.execute(num_retries=self._num_retries)
        except HttpError as exc:
            raise GmailError(f"{operation} failed: {exc}") from exc
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

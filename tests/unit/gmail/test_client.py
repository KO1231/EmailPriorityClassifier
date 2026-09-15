"""The Gmail wrapper's two responsibilities: retries, and errors that say what failed."""

import http.client
from typing import Any

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from epc.errors import GmailError
from epc.gmail.client import DEFAULT_RETRIES, GmailClient
from epc.gmail.models import ThreadRef


class FakeRequest:
    """Stands in for an `HttpRequest`, recording how it was executed."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.num_retries: int | None = None

    def execute(self, num_retries: int = 0) -> Any:
        self.num_retries = num_retries
        if self._error is not None:
            raise self._error
        return self._result


class FakeLabels:
    def __init__(self, request: FakeRequest) -> None:
        self._request = request
        self.list_kwargs: dict[str, Any] | None = None
        self.create_kwargs: dict[str, Any] | None = None

    def list(self, **kwargs: Any) -> FakeRequest:
        self.list_kwargs = kwargs
        return self._request

    def create(self, **kwargs: Any) -> FakeRequest:
        self.create_kwargs = kwargs
        return self._request


class FakeThreads:
    def __init__(self, request: FakeRequest) -> None:
        self._request = request
        self.list_kwargs: dict[str, Any] | None = None
        self.get_kwargs: dict[str, Any] | None = None

    def list(self, **kwargs: Any) -> FakeRequest:
        self.list_kwargs = kwargs
        return self._request

    def list_next(self, _request: Any, _response: Any) -> None:
        return None

    def get(self, **kwargs: Any) -> FakeRequest:
        self.get_kwargs = kwargs
        return self._request


class FakeService:
    def __init__(self, request: FakeRequest) -> None:
        self.labels_resource = FakeLabels(request)
        self.threads_resource = FakeThreads(request)

    def users(self) -> FakeService:
        return self

    def labels(self) -> FakeLabels:
        return self.labels_resource

    def threads(self) -> FakeThreads:
        return self.threads_resource


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def factory(result: Any = None, error: Exception | None = None) -> tuple[GmailClient, FakeService]:
        request = FakeRequest(result=result, error=error)
        service = FakeService(request)
        monkeypatch.setattr("epc.gmail.client.build", lambda *a, **k: service)
        return GmailClient(credentials=None), service  # type: ignore[arg-type]

    return factory


def http_error(status: int, reason: str) -> HttpError:
    response = type("Response", (), {"status": status, "reason": reason})()
    return HttpError(resp=response, content=b"{}")


def test_list_labels_parses_the_response(make_client: Any) -> None:
    client, service = make_client(
        {"labels": [{"id": "Label_1", "name": "#/P1"}, {"id": "INBOX", "name": "INBOX", "type": "system"}]}
    )
    labels = client.list_labels()

    assert [label.name for label in labels] == ["#/P1", "INBOX"]
    assert service.labels_resource.list_kwargs == {"userId": "me"}


def test_an_empty_mailbox_is_not_an_error(make_client: Any) -> None:
    client, _ = make_client({})
    assert client.list_labels() == []


def test_requests_are_retried(make_client: Any) -> None:
    """A single 429 during a long run used to cost that thread outright."""
    client, service = make_client({"labels": []})
    client.list_labels()
    assert service.labels_resource._request.num_retries == DEFAULT_RETRIES


def test_the_retry_count_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(result={"labels": []})
    monkeypatch.setattr("epc.gmail.client.build", lambda *a, **k: FakeService(request))
    GmailClient(credentials=None, num_retries=2).list_labels()  # type: ignore[arg-type]
    assert request.num_retries == 2


def test_an_http_error_names_the_operation(make_client: Any) -> None:
    client, _ = make_client(error=http_error(429, "Too Many Requests"))
    with pytest.raises(GmailError, match="listing labels failed"):
        client.list_labels()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        ConnectionResetError("reset by peer"),
        httplib2.ServerNotFoundError("Unable to find the server at gmail.googleapis.com"),
        RefreshError("token refresh failed"),
        http.client.IncompleteRead(b"partial"),
        http.client.BadStatusLine("garbage"),
    ],
    ids=["timeout", "reset", "dns", "refresh", "incomplete-read", "bad-status-line"],
)
def test_a_failure_below_http_is_a_gmail_error_too(make_client: Any, error: Exception) -> None:
    """`execute` retries these and then re-raises them raw. Callers catch
    `GmailError` to lose one thread instead of the run, so a raw one ended it."""
    client, _ = make_client(error=error)
    with pytest.raises(GmailError, match=f"listing labels failed: {type(error).__name__}"):
        client.list_labels()


def test_create_label_sends_a_visible_label(make_client: Any) -> None:
    client, service = make_client({"id": "Label_7", "name": "#/P1", "type": "user"})
    created = client.create_label("#/P1")

    assert created.id == "Label_7"
    body = (service.labels_resource.create_kwargs or {})["body"]
    assert body["name"] == "#/P1"
    assert body["labelListVisibility"] == "labelShow"


def test_a_failed_creation_names_the_label(make_client: Any) -> None:
    client, _ = make_client(error=http_error(409, "Conflict"))
    with pytest.raises(GmailError, match=r"creating label '#/P1' failed"):
        client.create_label("#/P1")


def test_listing_carries_each_threads_history_id(make_client: Any) -> None:
    """Enough to tell a thread that failed before from one that has changed,
    without fetching it."""
    client, service = make_client({"threads": [{"id": "t1", "historyId": "42"}, {"id": "t2"}]})
    refs = list(client.list_threads("in:inbox", limit=10))

    assert refs == [ThreadRef(id="t1", history_id="42"), ThreadRef(id="t2", history_id="")]
    assert "historyId" in service.threads_resource.list_kwargs["fields"]


def test_a_threads_labels_are_read_without_its_messages(make_client: Any) -> None:
    client, service = make_client({"messages": [{"labelIds": ["INBOX", "Label_1"]}, {"labelIds": ["UNREAD"]}]})
    assert client.thread_label_ids("t1") == {"INBOX", "Label_1", "UNREAD"}
    assert service.threads_resource.get_kwargs["format"] == "minimal"

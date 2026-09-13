"""Credential serialisation, loading, and the split between login and a run.

The browser flow itself is faked; what is tested is what happens around it.
"""

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from google.oauth2.credentials import Credentials

from epc.errors import CredentialError
from epc.gmail.auth import (
    SCOPES,
    LocalFileCredentialStore,
    authorise,
    deserialise,
    load_credentials,
    serialise,
)

# Values shaped like credentials, with nothing real in them.
FAKE = Credentials(
    token="ya29.short-lived-access-token",
    refresh_token="1//fake-refresh-token",
    token_uri="https://oauth2.googleapis.com/token",
    client_id="1234.apps.googleusercontent.com",
    client_secret="GOCSPX-fake",
    scopes=SCOPES,
)


def test_only_one_scope_is_requested() -> None:
    """`gmail.modify` subsumes readonly and labels; asking for all three widened
    the blast radius of a stolen token for nothing."""
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]


def test_the_access_token_is_not_stored() -> None:
    """Storing it would mean a new secret version on every run, against a
    bounded version history."""
    stored = json.loads(serialise(FAKE))
    assert "token" not in stored
    assert stored["refresh_token"] == "1//fake-refresh-token"
    assert stored["client_id"] == "1234.apps.googleusercontent.com"


def test_credentials_round_trip() -> None:
    restored = deserialise(serialise(FAKE))
    assert restored.refresh_token == FAKE.refresh_token
    assert restored.client_id == FAKE.client_id


def test_malformed_json_is_a_credential_error() -> None:
    with pytest.raises(CredentialError, match="not valid JSON"):
        deserialise("{not json")


def test_json_without_credential_fields_is_a_credential_error() -> None:
    with pytest.raises(CredentialError, match="unusable"):
        deserialise('{"hello": "world"}')


def test_the_local_store_round_trips(tmp_path: Path) -> None:
    store = LocalFileCredentialStore(tmp_path / "nested" / "token.json")
    assert store.load() is None

    store.store(serialise(FAKE))
    assert deserialise(store.load() or "").refresh_token == FAKE.refresh_token


def test_the_stored_file_is_not_group_or_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    LocalFileCredentialStore(path).store(serialise(FAKE))
    assert path.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------
# A run loads; only login authorises
# --------------------------------------------------------------------------


class MemoryStore:
    def __init__(self, payload: str | None = None) -> None:
        self.payload = payload
        self.loads = 0

    def load(self) -> str | None:
        self.loads += 1
        return self.payload

    def store(self, payload: str) -> None:
        self.payload = payload


class FakeFlow:
    """Stands in for `InstalledAppFlow`, handing back whatever it was given."""

    last_kwargs: ClassVar[dict[str, Any]] = {}

    def __init__(self, granted: Credentials) -> None:
        self._granted = granted

    def run_local_server(self, **kwargs: Any) -> Credentials:
        FakeFlow.last_kwargs = kwargs
        return self._granted


@pytest.fixture
def client_secrets(tmp_path: Path) -> Path:
    path = tmp_path / "client_secrets.json"
    path.write_text("{}", encoding="utf-8")
    return path


def grant(monkeypatch: pytest.MonkeyPatch, credentials: Credentials) -> None:
    monkeypatch.setattr(
        "epc.gmail.auth.InstalledAppFlow.from_client_secrets_file",
        lambda *_args, **_kwargs: FakeFlow(credentials),
    )


def refresh_raises(monkeypatch: pytest.MonkeyPatch, error: Exception | None) -> None:
    def refresh(_self: Credentials, _request: Any) -> None:
        if error is not None:
            raise error

    monkeypatch.setattr(Credentials, "refresh", refresh)


@pytest.mark.parametrize(
    "stored",
    [
        "placeholder",  # what Terraform creates the SSM parameter with
        "{not json",
        '"a string"',
        '{"hello": "world"}',
        serialise(FAKE),  # well-formed, but its refresh token is dead
    ],
    ids=["placeholder", "truncated", "not-an-object", "wrong-fields", "expired"],
)
def test_login_replaces_whatever_is_stored_without_reading_it(
    stored: str, client_secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every broken-credential error says "run `epc login`". Login used to go
    through the same load-and-refresh path, and fail on the thing it was run to
    replace — leaving deleting the token by hand as the only way out."""
    refresh_raises(monkeypatch, RuntimeError("invalid_grant"))
    fresh = Credentials(
        token="ya29.new",
        refresh_token="1//new-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="1234.apps.googleusercontent.com",
        client_secret="GOCSPX-fake",
        scopes=SCOPES,
    )
    grant(monkeypatch, fresh)
    store = MemoryStore(stored)

    authorise(store, client_secrets_file=client_secrets)

    assert store.loads == 0
    assert json.loads(store.payload or "")["refresh_token"] == "1//new-refresh-token"


def test_login_always_shows_the_consent_screen(client_secrets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Google only issues a refresh token when consent is actually shown."""
    grant(monkeypatch, FAKE)
    authorise(MemoryStore(), client_secrets_file=client_secrets)
    assert FakeFlow.last_kwargs.get("prompt") == "consent"


def test_login_stores_nothing_without_a_refresh_token(client_secrets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """That would look like success and fail on the first scheduled run."""
    grant(monkeypatch, Credentials(token="ya29.only-access", scopes=SCOPES))
    store = MemoryStore("previous")
    with pytest.raises(CredentialError, match="did not issue a refresh token"):
        authorise(store, client_secrets_file=client_secrets)
    assert store.payload == "previous"


def test_login_needs_the_client_secrets_file(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="client secrets file not found"):
        authorise(MemoryStore(), client_secrets_file=tmp_path / "missing.json")


def test_a_run_never_authorises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nobody is at a scheduled task to click through a browser."""

    def no_browser(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a run must not start the consent flow")

    monkeypatch.setattr("epc.gmail.auth.InstalledAppFlow.from_client_secrets_file", no_browser)
    with pytest.raises(CredentialError, match="run `epc login`"):
        load_credentials(MemoryStore())


@pytest.mark.parametrize("stored", ["placeholder", '{"hello": "world"}'])
def test_an_unusable_stored_value_names_the_fix(stored: str) -> None:
    with pytest.raises(CredentialError, match="run `epc login`"):
        load_credentials(MemoryStore(stored))


def test_a_refresh_failure_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_raises(monkeypatch, RuntimeError("invalid_grant"))
    with pytest.raises(CredentialError, match=r"run `epc login`.*7 days"):
        load_credentials(MemoryStore(serialise(FAKE)))


def test_a_run_refreshes_what_is_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_raises(monkeypatch, None)
    credentials = load_credentials(MemoryStore(serialise(FAKE)))
    assert credentials.refresh_token == FAKE.refresh_token

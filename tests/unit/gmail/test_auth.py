"""Credential serialisation. The OAuth flow itself is not exercised here."""

import json
from pathlib import Path

import pytest
from google.oauth2.credentials import Credentials

from epc.errors import CredentialError
from epc.gmail.auth import SCOPES, LocalFileCredentialStore, deserialise, serialise

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

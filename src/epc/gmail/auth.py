"""Gmail credentials, and where they are kept.

Three things changed from the previous implementation, and all three are
security rather than convenience:

* **No `pickle`.** `pickle.load` on a file an attacker can write is arbitrary
  code execution. Credentials round-trip as JSON, which is also the only form a
  secret store can hold.
* **One scope.** `gmail.modify` subsumes both `gmail.readonly` and
  `gmail.labels`; asking for all three granted nothing extra and widened the
  blast radius of a stolen token.
* **Only the durable fields are stored.** The short-lived access token stays in
  memory for the life of a run. Writing it back would mean a new secret version
  on every single run, and secret stores keep a bounded version history.

Because the scope set shrank, any previously issued token is invalid: Google
re-prompts for consent. There is consequently nothing to migrate out of the old
`token.pickle`, and no reason to write a reader for it.
"""

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from epc.errors import CredentialError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
"""Read message content and change labels. Not enough to delete mail permanently."""

# Everything needed to mint a fresh access token, and nothing that expires.
_DURABLE_FIELDS = (
    "client_id",
    "client_secret",
    "refresh_token",
    "token_uri",
    "scopes",
    "universe_domain",
)


@runtime_checkable
class CredentialStore(Protocol):
    """Where the durable half of the OAuth credentials lives.

    Implemented against the local filesystem here; SSM Parameter Store and
    Secrets Manager plug in behind the same two methods.
    """

    def load(self) -> str | None:
        """Serialised credentials, or None when nothing has been stored yet."""
        ...

    def store(self, payload: str) -> None:
        """Persist serialised credentials, replacing whatever was there."""
        ...


class LocalFileCredentialStore:
    """Credentials in a JSON file, for local runs."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str | None:
        if not self._path.is_file():
            return None
        return self._path.read_text(encoding="utf-8")

    def store(self, payload: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(payload, encoding="utf-8")
        # Tokens are user-only; the directory may well be shared.
        self._path.chmod(0o600)


def serialise(credentials: Credentials) -> str:
    """Serialise the durable fields only, dropping the access token."""
    data: dict[str, Any] = json.loads(credentials.to_json())
    return json.dumps({key: data[key] for key in _DURABLE_FIELDS if data.get(key) is not None}, indent=2)


def deserialise(payload: str) -> Credentials:
    """Rebuild credentials from a stored payload."""
    try:
        info = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CredentialError("stored credentials are not valid JSON") from exc
    try:
        return Credentials.from_authorized_user_info(info, SCOPES)
    except (ValueError, KeyError) as exc:
        raise CredentialError(f"stored credentials are unusable: {exc}") from exc


def get_credentials(store: CredentialStore, *, client_secrets_file: Path | None = None) -> Credentials:
    """Return usable credentials, refreshing or re-authorising as needed.

    A newly issued refresh token is written back; a merely refreshed access
    token is not, because it is not stored in the first place.
    """
    payload = store.load()
    credentials = deserialise(payload) if payload else None

    if credentials and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:  # google-auth raises a wide range here
            raise CredentialError(
                "could not refresh the stored Gmail credentials; re-run `epc login`. "
                "If the OAuth consent screen is still in Testing, refresh tokens expire after 7 days."
            ) from exc
        return credentials

    if client_secrets_file is None:
        raise CredentialError("no stored credentials and no client secrets file to authorise with")
    if not client_secrets_file.is_file():
        raise CredentialError(f"client secrets file not found: {client_secrets_file}")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
    credentials = flow.run_local_server()
    store.store(serialise(credentials))
    return credentials


class SsmCredentialStore:
    """Credentials in an SSM Parameter Store `SecureString`.

    Parameter Store rather than Secrets Manager by default, and the reasoning is
    worth writing down because the name suggests otherwise: the encryption and
    access control are not weaker. Both are KMS-encrypted at rest, IAM-gated and
    CloudTrail-audited. What Secrets Manager adds is automatic rotation,
    resource-based policies, cross-region replication and a 64KB limit — none of
    which apply here. The Gmail credential is well under the 4KB Standard-tier
    limit, and its refresh token is rotated by Google's OAuth flow, not by a
    rotation Lambda. Standard-tier parameters are free; secrets are not.

    The caller needs `ssm:GetParameter` and `ssm:PutParameter` on this
    parameter's ARN, plus `kms:Decrypt` — mandatory for a customer-managed key,
    and harmless to state for the AWS-managed `aws/ssm` key.
    """

    def __init__(self, name: str, client: Any = None, *, region: str | None = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("ssm", region_name=region)
        self._client = client
        self._name = name

    def load(self) -> str | None:
        try:
            response = self._client.get_parameter(Name=self._name, WithDecryption=True)
        except Exception as exc:  # botocore raises ClientError for every failure
            if "ParameterNotFound" in str(exc):
                return None
            raise CredentialError(f"could not read {self._name}: {exc}") from exc
        value = (response.get("Parameter") or {}).get("Value")
        return str(value) if value else None

    def store(self, payload: str) -> None:
        try:
            self._client.put_parameter(Name=self._name, Value=payload, Type="SecureString", Overwrite=True)
        except Exception as exc:
            raise CredentialError(f"could not write {self._name}: {exc}") from exc

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

    Implemented for the local filesystem, SSM Parameter Store and Secrets
    Manager, all behind the same two methods.
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


# Appended to every "the stored value is no good" message, because the fix is
# always the same and the person reading it is usually mid-setup.
_LOGIN_HINT = "run `epc login` to authorise again and replace it"


def deserialise(payload: str) -> Credentials:
    """Rebuild credentials from a stored payload."""
    try:
        info = json.loads(payload)
    except json.JSONDecodeError as exc:
        # The commonest cause by far: a secret store entry that was created
        # empty — Terraform writes a placeholder — and never filled.
        raise CredentialError(f"stored credentials are not valid JSON; {_LOGIN_HINT}") from exc
    if not isinstance(info, dict):
        raise CredentialError(f"stored credentials are not a JSON object; {_LOGIN_HINT}")
    try:
        return Credentials.from_authorized_user_info(info, SCOPES)
    except (ValueError, KeyError) as exc:
        raise CredentialError(f"stored credentials are unusable ({exc}); {_LOGIN_HINT}") from exc


def load_credentials(store: CredentialStore) -> Credentials:
    """Credentials for a run: whatever is stored, refreshed.

    Never opens a browser. Runs happen on schedulers and in containers where
    nobody is there to click through a consent screen, so a missing or broken
    credential is an error naming the fix, not an attempt to authorise.
    """
    payload = store.load()
    if not payload:
        raise CredentialError("no Gmail credentials are stored yet; run `epc login`")

    credentials = deserialise(payload)
    if not credentials.refresh_token:
        raise CredentialError(f"stored credentials have no refresh token; {_LOGIN_HINT}")

    try:
        credentials.refresh(Request())
    except Exception as exc:  # google-auth raises a wide range here
        raise CredentialError(
            f"could not refresh the stored Gmail credentials; {_LOGIN_HINT}. "
            "If the OAuth consent screen is still in Testing, refresh tokens expire after 7 days."
        ) from exc
    return credentials


def authorise(store: CredentialStore, *, client_secrets_file: Path) -> Credentials:
    """Run the consent flow in a browser and store what it grants.

    Deliberately does not read what is already stored. `epc login` is the
    recovery for every broken-credential error `load_credentials` raises — an
    expired refresh token, a placeholder nobody filled, a file that got
    truncated — so it must not fail on the very thing it exists to replace.
    """
    if not client_secrets_file.is_file():
        raise CredentialError(f"client secrets file not found: {client_secrets_file}")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
    # `prompt=consent`: Google issues a refresh token only when the consent
    # screen is actually shown, and skips the screen for an app the account has
    # already approved. Without this, logging in again to replace an expired
    # token could come back without a new one.
    credentials = flow.run_local_server(prompt="consent")
    if not credentials.refresh_token:
        # Storing this would look like success and fail on the first run.
        raise CredentialError("Google did not issue a refresh token; nothing was stored")

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


class SecretsManagerCredentialStore:
    """Credentials in a Secrets Manager secret.

    For an estate that already keeps its secrets there, or wants what Parameter
    Store lacks — resource policies, replication. See `SsmCredentialStore` for
    why that is not the default.

    Reading needs `secretsmanager:GetSecretValue`. `epc login` also needs
    `secretsmanager:PutSecretValue`, and `secretsmanager:CreateSecret` when the
    secret does not exist yet. `kms:Decrypt` is needed for a customer-managed
    key.
    """

    def __init__(self, secret_id: str, client: Any = None, *, region: str | None = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("secretsmanager", region_name=region)
        self._client = client
        self._secret_id = secret_id

    def load(self) -> str | None:
        try:
            response = self._client.get_secret_value(SecretId=self._secret_id)
        except Exception as exc:  # botocore raises ClientError for every failure
            if "ResourceNotFoundException" in str(exc):
                return None
            raise CredentialError(f"could not read {self._secret_id}: {exc}") from exc
        value = response.get("SecretString")
        return str(value) if value else None

    def store(self, payload: str) -> None:
        try:
            self._client.put_secret_value(SecretId=self._secret_id, SecretString=payload)
        except Exception as exc:
            if "ResourceNotFoundException" not in str(exc):
                raise CredentialError(f"could not write {self._secret_id}: {exc}") from exc
            self._create(payload)

    def _create(self, payload: str) -> None:
        try:
            self._client.create_secret(
                Name=self._secret_id,
                SecretString=payload,
                Description="Durable half of the Gmail OAuth credentials. Written by `epc login`.",
            )
        except Exception as exc:
            raise CredentialError(f"could not create {self._secret_id}: {exc}") from exc

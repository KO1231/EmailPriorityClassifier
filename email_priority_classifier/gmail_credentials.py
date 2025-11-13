import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Gmail APIのスコープを設定
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.metadata",
]

_CLIENT_SECRETS_FILE = Path(__file__).parent.parent / "secrets" / "client_secrets.json"
_TOKEN_FILE = Path(__file__).parent.parent / "secrets" / "token.pickle"


def get_credential() -> Credentials:
    creds = None
    if _TOKEN_FILE.exists():
        with _TOKEN_FILE.open("rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(_CLIENT_SECRETS_FILE.resolve()), _SCOPES)
            creds = flow.run_local_server()
        with _TOKEN_FILE.open("wb") as token:
            pickle.dump(creds, token)
    return creds


if __name__ == "__main__":
    get_credential()

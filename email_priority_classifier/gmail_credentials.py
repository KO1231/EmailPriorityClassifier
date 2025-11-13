import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify"
]


def get_credential(client_secrets_file_path: str, token_file_path: str) -> Credentials:
    creds = None
    client_secrets_file = Path(client_secrets_file_path).resolve()
    token_file = Path(token_file_path).resolve()

    if token_file.exists():
        with token_file.open("rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), _SCOPES)
            creds = flow.run_local_server()
        with token_file.open("wb") as token:
            pickle.dump(creds, token)
    return creds


if __name__ == "__main__":
    CLIENT_SECRETS_FILE = Path(__file__).resolve().parent.parent / "secrets" / "client_secrets.json"
    TOKEN_FILE = Path(__file__).resolve().parent.parent / "secrets" / "token.pickle"
    print(type(get_credential(str(CLIENT_SECRETS_FILE), str(TOKEN_FILE))))

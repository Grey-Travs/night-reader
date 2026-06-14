"""Google OAuth (installed/desktop app) and service builders.

The app reads the developer's *own* Docs, so a desktop OAuth flow is the right
fit (a service account would only work for explicitly shared files). The token
is cached to disk and refreshed automatically.
"""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read-only access to Google Docs is all we need — the doc is opened by ID, so no
# broad Drive permission is required. (Re-add drive.readonly only if you later want
# to look the doc up by name/folder.)
SCOPES = [
    "https://www.googleapis.com/auth/documents.readonly",
]


def get_credentials(credentials_file: Path, token_file: Path) -> Credentials:
    """Return cached/refreshed credentials, running the consent flow if needed."""
    creds: Credentials | None = None
    token_file = Path(token_file)
    credentials_file = Path(credentials_file)

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_file.exists():
                raise FileNotFoundError(
                    f"OAuth client secret not found: {credentials_file}. "
                    "Create an OAuth 'Desktop app' client in Google Cloud Console "
                    "(APIs & Services > Credentials) and download it to this path."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return creds


def load_saved_credentials(token_file: Path) -> Credentials:
    """Return cached/refreshed credentials WITHOUT ever launching the consent flow.

    Used on hot paths (loading a novel's chapters) where popping a browser sign-in
    inside the server would hang or fail on a device that can't show it. Raises if
    there's no usable token, so the caller can degrade gracefully (e.g. show the
    saved offline copy) instead of blocking. Interactive sign-in stays in
    :func:`get_credentials`, reached only from the explicit Login action.
    """
    token_file = Path(token_file)
    if not token_file.exists():
        raise FileNotFoundError("Not signed in to Google on this device.")
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds
    raise RuntimeError("Google sign-in needs renewing — open Login to reconnect.")


def build_docs_service(creds: Credentials):
    """Build the Docs service client from credentials."""
    return build("docs", "v1", credentials=creds, cache_discovery=False)

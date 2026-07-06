"""Google OAuth (installed/desktop app) and service builders.

The app reads the developer's *own* Docs, so a desktop OAuth flow is the right
fit (a service account would only work for explicitly shared files). The token
is cached to disk and refreshed automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.auth.exceptions import GoogleAuthError, RefreshError
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


def _load_token(token_file: Path) -> Credentials | None:
    """Read cached credentials, tolerating a missing/corrupt token file.

    A truncated or hand-edited ``token.json`` would otherwise raise on every call;
    treating it as "not signed in" lets the caller re-run consent instead of dying.
    """
    if not token_file.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except (ValueError, OSError):
        return None


def _save_token(token_file: Path, creds: Credentials) -> None:
    """Persist credentials atomically (temp file + replace) so a crash mid-write
    can't leave a truncated token that fails to parse next launch."""
    tmp = token_file.with_suffix(token_file.suffix + ".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")
    os.replace(tmp, token_file)


def get_credentials(credentials_file: Path, token_file: Path) -> Credentials:
    """Return cached/refreshed credentials, running the consent flow if needed.

    If the cached refresh token is expired or revoked (``invalid_grant`` —
    common for OAuth apps still in "Testing" status, whose refresh tokens expire
    after 7 days), the dead token is discarded and the interactive consent flow
    re-runs, rather than failing with a raw ``RefreshError``.
    """
    token_file = Path(token_file)
    credentials_file = Path(credentials_file)
    creds = _load_token(token_file)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(token_file, creds)
            return creds
        except RefreshError:
            creds = None  # token revoked/expired — fall through to re-consent

    if not credentials_file.exists():
        raise FileNotFoundError(
            f"OAuth client secret not found: {credentials_file}. "
            "Create an OAuth 'Desktop app' client in Google Cloud Console "
            "(APIs & Services > Credentials) and download it to this path."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(token_file, creds)
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
    creds = _load_token(token_file)
    if creds is None:
        raise FileNotFoundError("Not signed in to Google on this device.")
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except (RefreshError, GoogleAuthError) as exc:
            raise RuntimeError("Google sign-in needs renewing — open Login to reconnect.") from exc
        _save_token(token_file, creds)
        return creds
    raise RuntimeError("Google sign-in needs renewing — open Login to reconnect.")


def build_docs_service(creds: Credentials):
    """Build the Docs service client from credentials."""
    return build("docs", "v1", credentials=creds, cache_discovery=False)

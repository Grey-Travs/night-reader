"""Google OAuth (installed/desktop app) and service builders.

The app reads the developer's *own* Docs, so a desktop OAuth flow is the right
fit (a service account would only work for explicitly shared files). The token
is cached to disk and refreshed automatically.
"""

from __future__ import annotations

from pathlib import Path

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .atomic import atomic_write_text

# Read-only access to Google Docs is all we need — the doc is opened by ID, so no
# broad Drive permission is required. (Re-add drive.readonly only if you later want
# to look the doc up by name/folder.)
SCOPES = [
    "https://www.googleapis.com/auth/documents.readonly",
]

# What the Doc export needs on top: creating a document and writing into it.
#
# ``drive.file`` is deliberately the narrowest scope that can do it. It grants access to
# files THIS APP creates and nothing else, so an export can never read, change or delete
# anything already in the user's Drive — which matters here, because the Drive in question
# holds the source document of every novel in the library. The alternatives (``documents``
# or ``drive``) would both hand over the lot.
WRITE_SCOPES = SCOPES + ["https://www.googleapis.com/auth/drive.file"]


class MissingScope(Exception):
    """Raised before a write when the saved token was never granted one.

    Its own type rather than a message, because the advice is unusually easy to get
    wrong: Google answers an ungranted scope with a 403, which reads exactly like the
    document not being shared, and sends the user off to re-share a document they
    already own.
    """


def missing_scopes(creds: Credentials, scopes: list[str]) -> list[str]:
    """Which of ``scopes`` this token was never granted.

    Reads the token's OWN scopes, which only works because ``_load_token`` stopped
    passing a scope list in — see the note there.
    """
    have = set(creds.scopes or [])
    return [s for s in scopes if s not in have]


def require_scopes(creds: Credentials, scopes: list[str]) -> None:
    """Refuse a write the token cannot do, before anything is created.

    Checked up front rather than letting Google refuse it, so that a half-built document
    never ends up in the user's Drive for them to find and clean up.
    """
    absent = missing_scopes(creds, scopes)
    if absent:
        raise MissingScope(
            "Night Reader was never granted permission to write to Google Docs "
            f"({', '.join(s.rsplit('/', 1)[-1] for s in absent)}). Reconnect Google and "
            "approve it.")


def _load_token(token_file: Path) -> Credentials | None:
    """Read cached credentials, tolerating a missing/corrupt token file.

    A truncated or hand-edited ``token.json`` would otherwise raise on every call;
    treating it as "not signed in" lets the caller re-run consent instead of dying.

    **No scope list is passed.** ``from_authorized_user_file(path, SCOPES)`` ignores what
    the token file actually says and stamps the list it was given onto the credentials —
    so the moment ``SCOPES`` widened, every token would have claimed a permission the
    user never granted, and the next refresh would have written that claim back to disk,
    destroying the only record of what was really authorised. Verified directly: with a
    list passed, a read-only token reports both scopes and ``to_json()`` persists both;
    without one, it reports the truth.
    """
    if not token_file.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_file))
    except (ValueError, OSError):
        return None


def _save_token(token_file: Path, creds: Credentials) -> None:
    """Persist credentials atomically (temp file + replace) so a crash mid-write
    can't leave a truncated token that fails to parse next launch."""
    atomic_write_text(token_file, creds.to_json())


def get_credentials(credentials_file: Path, token_file: Path,
                    scopes: list[str] | None = None) -> Credentials:
    """Return cached/refreshed credentials, running the consent flow if needed.

    If the cached refresh token is expired or revoked (``invalid_grant`` —
    common for OAuth apps still in "Testing" status, whose refresh tokens expire
    after 7 days), the dead token is discarded and the interactive consent flow
    re-runs, rather than failing with a raw ``RefreshError``.

    ``scopes`` defaults to the read-only set. Ask for :data:`WRITE_SCOPES` to reach the
    consent screen for the export — a cached read-only token is otherwise perfectly
    valid, so it short-circuits every check below and the browser never opens. That was
    the whole trap: widening the scope list on its own does nothing at all, and the first
    sign of it is a 403 much later that reads like a sharing problem.
    """
    token_file = Path(token_file)
    credentials_file = Path(credentials_file)
    scopes = list(scopes or SCOPES)
    creds = _load_token(token_file)

    # A token NARROWER than what is being asked for must not satisfy either shortcut.
    # Refreshing cannot widen one either — only consent can.
    wide_enough = bool(creds) and not missing_scopes(creds, scopes)

    if creds and creds.valid and wide_enough:
        return creds

    if creds and creds.expired and creds.refresh_token and wide_enough:
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
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
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

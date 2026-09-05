"""Turn an exception into something a reader — not a developer — can act on.

Before this module, a failure took one of two useless shapes:

* The Claude Agent SDK raises a BARE ``Exception`` when the CLI is spawned but doesn't
  answer its ``initialize`` control request ("Control request timeout: initialize").
  It matched no ``except`` clause in the translator or the endpoints, so it reached
  FastAPI as an unhandled 500 and uvicorn dumped an ~80-line traceback to the console.
* A bare 500 carries no JSON ``detail``, so the frontend's ``req()`` fell back to
  ``r.statusText`` and the user was shown the words "Internal Server Error".

``explain()`` maps what this codebase can actually raise onto a title, a plain-English
description, and ordered fix steps. The full traceback still gets captured — it goes to
``logs/errors.log`` and behind a "Technical details" disclosure, not into the user's face.
"""

from __future__ import annotations

import re
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "errors.log"
_LOG_MAX_BYTES = 1_000_000  # keep the tail; this is a breadcrumb trail, not an archive

# UI affordances the frontend knows how to render as a button.
ACTION_RETRY = "retry"
ACTION_RECONNECT_GOOGLE = "reconnect-google"
ACTION_SETTINGS = "settings"
ACTION_SETUP = "setup"


@dataclass
class Explained:
    code: str                                   # stable slug for the frontend to switch on
    title: str                                  # one short line, shown as the heading
    what: str                                   # plain-English description of what happened
    fixes: list[str] = field(default_factory=list)   # ordered, actionable steps
    action: str | None = None                   # optional button the UI can offer
    retryable: bool = False                     # whether "try again" is likely to help
    detail: str = ""                            # exception type + message, one line
    trace: str = ""                             # full traceback, for the copy button
    status: int = 500                           # HTTP status to answer with


def as_dict(e: Explained) -> dict:
    return asdict(e)


def _message(exc: BaseException) -> str:
    """``str(exc)`` can itself raise — a custom __str__ that fails, or a lazily-formatted
    message. Since this module exists to make failures legible, it must never add one."""
    try:
        return str(exc)
    except Exception:  # noqa: BLE001
        return "<unprintable error>"


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_message(exc)}".strip()


def _trace(exc: BaseException) -> str:
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:  # noqa: BLE001
        return _detail(exc)


def _http_status(exc: BaseException) -> int | None:
    """Pull the status code out of a googleapiclient HttpError without importing it
    (the import is heavy and this module is used on paths that never touch Google)."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


# Ordered rules: the first whose predicate matches wins, so put specific before broad.
def explain(exc: BaseException) -> Explained:
    """Classify ``exc``. Never raises — an unrecognised error still yields a usable
    Explained with the raw detail and a copyable traceback."""
    try:
        return _classify(exc)
    except Exception:  # noqa: BLE001 — explaining a failure must not fail
        return Explained(
            code="unknown", title="Something went wrong",
            what="Night Reader hit an error it couldn't identify.",
            fixes=["Try the action again.", "If it keeps happening, copy the report below."],
            retryable=True, detail=_detail(exc), trace=_trace(exc),
        )


def _classify(exc: BaseException) -> Explained:
    name = type(exc).__name__
    msg = _message(exc)
    low = msg.lower()
    detail, trace = _detail(exc), _trace(exc)

    def out(**kw) -> Explained:
        return Explained(detail=detail, trace=trace, **kw)

    # ---- Claude Code CLI: startup / availability -----------------------------------
    if "control request timeout" in low or "initialize timeout" in low:
        return out(
            code="claude-start-timeout",
            title="Claude Code didn't start in time",
            what="Night Reader launched Claude Code but it didn't finish starting up "
                 "before the timeout. This is usually a slow cold start — antivirus "
                 "scanning the process, or the machine being busy — rather than a real "
                 "fault, so the same action often works on a second try.",
            fixes=[
                "Try again — translations already retry this automatically, and a warm "
                "start is much faster.",
                "Check you're still signed in: open a terminal and run `claude`. If it "
                "asks you to log in, do that and retry.",
                "If it keeps timing out, close any leftover `claude` or `node` processes "
                "in Task Manager, then restart Night Reader.",
            ],
            action=ACTION_RETRY, retryable=True, status=503,
        )

    if name == "CLINotFoundError" or "claude code not found" in low or "enoent" in low:
        return out(
            code="claude-missing",
            title="Claude Code isn't installed (or isn't on PATH)",
            what="The translation engine drives the Claude Code CLI, and it couldn't be "
                 "found on this machine.",
            fixes=[
                "Install it: `npm install -g @anthropic-ai/claude-code`",
                "Then run `claude` once in a terminal and sign in with your Claude "
                "Max/Pro account.",
                "Come back and run full setup to re-check the connection.",
            ],
            action=ACTION_SETUP, retryable=False, status=503,
        )

    if name in ("CLIConnectionError", "ProcessError") or "cli connection" in low:
        return out(
            code="claude-connection",
            title="Lost the connection to Claude",
            what="The Claude Code process stopped responding partway through. Night "
                 "Reader already retried several times before giving up.",
            fixes=[
                "Try again — this is usually transient.",
                "Check your internet connection, and any VPN or proxy.",
                "If it happens on every chapter, restart Night Reader.",
            ],
            action=ACTION_RETRY, retryable=True, status=502,
        )

    if name == "RateLimitedError" or "usage limit" in low or "rate limit" in low:
        return out(
            code="rate-limited",
            title="Your Claude plan's limit was reached",
            what="The subscription's usage window is exhausted. Progress is saved — "
                 "finished chapters are never redone, so nothing is lost.",
            fixes=[
                "Wait for the window to reset; queued translations resume by themselves.",
                "Switch to a lighter model or a lower effort in Settings to stretch the "
                "allowance further.",
            ],
            action=ACTION_SETTINGS, retryable=False, status=429,
        )

    # ---- Translator-level problems --------------------------------------------------
    if name == "TranslationAborted":
        return out(
            code="stopped",
            title="Translation stopped",
            what="This chapter was stopped before it finished. Nothing was overwritten.",
            fixes=["Start it again whenever you're ready."],
            action=ACTION_RETRY, retryable=True, status=409,
        )

    if "incomplete response" in low:
        return out(
            code="incomplete-response",
            title="The translation was cut off before finishing",
            what="The model run ended without completing the chapter. Night Reader "
                 "deliberately discards a partial chapter rather than saving it as if "
                 "it were finished.",
            fixes=[
                "Try the chapter again.",
                "If it keeps happening on a long chapter, lower `effort` in Settings, or "
                "reduce `chunk_threshold` in config.toml so it's translated in smaller "
                "pieces.",
            ],
            action=ACTION_RETRY, retryable=True, status=502,
        )

    if "agent error" in low or re.search(r"\b400\b", msg):
        return out(
            code="agent-rejected",
            title="Claude rejected the request",
            what="The model returned an error instead of a translation. The most common "
                 "cause is an unsupported combination of model and thinking/effort "
                 "settings.",
            fixes=[
                "Open Settings and pick a different model or a lower effort.",
                "Note that some models always think — turning thinking off is rejected "
                "for those.",
                "Try again after changing the setting.",
            ],
            action=ACTION_SETTINGS, retryable=False, status=502,
        )

    # ---- Google Docs ----------------------------------------------------------------
    status = _http_status(exc)
    if name == "RefreshError" or "invalid_grant" in low or "token has been expired" in low \
            or (status == 401):
        return out(
            code="google-auth-expired",
            title="Your Google sign-in expired",
            what="Night Reader can't read your source documents until you reconnect the "
                 "Google account that owns them.",
            fixes=["Click Reconnect Google and sign in again.",
                   "Then retry what you were doing."],
            action=ACTION_RECONNECT_GOOGLE, retryable=False, status=401,
        )

    if status == 403:
        return out(
            code="google-forbidden",
            title="No access to that Google Doc",
            what="The signed-in Google account isn't allowed to open this novel's source "
                 "document.",
            fixes=[
                "Open the Doc in your browser and share it with the account you signed "
                "in to Night Reader with.",
                "If you own it under a different Google account, reconnect with that one.",
            ],
            action=ACTION_RECONNECT_GOOGLE, retryable=False, status=403,
        )

    if status == 404:
        return out(
            code="google-not-found",
            title="That Google Doc no longer exists",
            what="The source document was deleted, moved to the trash, or its link "
                 "changed. Chapters already translated are still saved on this machine.",
            fixes=[
                "Check the document link in this novel's Settings.",
                "Restore the Doc from Google Drive's trash if it was deleted by mistake.",
            ],
            action=ACTION_SETTINGS, retryable=False, status=404,
        )

    if status is not None and status >= 500:
        return out(
            code="google-unavailable",
            title="Google Docs is having trouble",
            what="Google returned a server error. This is on their side, not yours.",
            fixes=["Wait a minute and try again."],
            action=ACTION_RETRY, retryable=True, status=502,
        )

    # ---- Network / filesystem -------------------------------------------------------
    if name in ("ConnectionError", "ConnectionResetError", "TimeoutError",
                "SSLError", "SSLEOFError", "gaierror", "socket.timeout") \
            or "getaddrinfo" in low or "ssl" in low or "connection aborted" in low:
        return out(
            code="offline",
            title="No internet connection",
            what="Night Reader couldn't reach the network. Translating and fetching "
                 "source documents both need it; reading already-translated chapters "
                 "does not.",
            fixes=["Check your connection and try again.",
                   "Already-translated chapters stay readable offline."],
            action=ACTION_RETRY, retryable=True, status=503,
        )

    if name in ("PermissionError", "FileExistsError") or "permission denied" in low \
            or "being used by another process" in low:
        return out(
            code="file-locked",
            title="Couldn't save the file",
            what="A chapter or settings file couldn't be written because something else "
                 "on this machine is holding it open.",
            fixes=[
                "Close the file if you have it open in another program.",
                "If the folder syncs (OneDrive, Dropbox), pause syncing and try again.",
                "Check there's free disk space.",
            ],
            action=ACTION_RETRY, retryable=True, status=500,
        )

    if name in ("OSError", "IOError") and ("no space" in low or "errno 28" in low):
        return out(
            code="disk-full",
            title="The disk is full",
            what="There isn't enough free space to save the translation.",
            fixes=["Free up some disk space, then try again."],
            action=ACTION_RETRY, retryable=True, status=507,
        )

    if name == "JSONDecodeError" or "expecting value" in low:
        return out(
            code="corrupt-data",
            title="A saved file couldn't be read",
            what="One of this novel's data files is unreadable — usually because the app "
                 "was killed mid-write.",
            fixes=[
                "Reload the page; Night Reader rebuilds what it can automatically.",
                "If a novel looks empty, restore it from a .zip backup.",
            ],
            action=ACTION_RETRY, retryable=True, status=500,
        )

    # ---- Fallback -------------------------------------------------------------------
    return out(
        code="unknown",
        title="Something went wrong",
        what="Night Reader hit an unexpected error. The technical details below say "
             "exactly what happened.",
        fixes=["Try the action again.",
               "If it keeps happening, use Copy report and include that text when "
               "asking for help."],
        retryable=True, status=500,
    )


def from_http_detail(detail, status: int) -> Explained:
    """Wrap an ``HTTPException`` detail in the same shape as ``explain()``.

    The endpoints raise HTTPException with hand-written, already-friendly messages
    ("This novel is in read-only saved mode…"). Those are good copy, so they become the
    title verbatim — the point here is only that the frontend gets ONE payload shape to
    parse rather than sometimes-a-string and sometimes-an-object.
    """
    if isinstance(detail, dict) and "code" in detail:
        return Explained(**{k: v for k, v in detail.items()
                            if k in Explained.__dataclass_fields__})
    text = detail if isinstance(detail, str) else str(detail)
    code = {400: "bad-request", 401: "google-auth-expired", 403: "forbidden",
            404: "not-found", 409: "conflict", 429: "rate-limited"}.get(status, "request-failed")
    return Explained(
        code=code, title=text, what="", fixes=[],
        action=ACTION_RECONNECT_GOOGLE if status == 401 else None,
        retryable=status in (429, 503, 502), detail=text, status=status,
    )


def log_error(e: Explained, where: str = "") -> None:
    """One compact line to the terminal; the full traceback to ``logs/errors.log``.

    Splitting these two is the whole point: the console stays readable while the
    traceback is still available (and copyable) when something needs diagnosing.
    """
    from . import console

    console.print_error(e.title, f"{where} {e.detail}".strip())
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Cheap size cap: once over the limit, keep the newest half and carry on.
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_MAX_BYTES:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-_LOG_MAX_BYTES // 2:]
            LOG_FILE.write_text(tail, encoding="utf-8")
        import time as _time

        stamp = _time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"\n{'=' * 70}\n[{stamp}] {e.code} — {e.title}\n"
                     f"{('at ' + where) if where else ''}\n{e.trace or e.detail}\n")
    except Exception:  # noqa: BLE001 — logging must never break the response
        pass

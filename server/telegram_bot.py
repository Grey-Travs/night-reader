"""A Telegram bot as another way to send pages: photos, albums and scanner PDFs sent to
it go into the novel as a chapter, and the same notices the phone gets come back in the
chat.

It asks Telegram for new messages (long polling), so nothing on this computer has to be
reachable from outside. Standard library only. Once paired, messages from anyone but the
one person paired with it are ignored without a word.

- ``/novel`` picks the novel, from buttons.
- ``/chapter [title]`` starts a chapter; photos and files then go into it, in the order
  they were sent. A photo sent with no chapter open starts one.
- ``/done`` finishes it, and it is read, built and translated by itself (hands_free.py).
- ``/status`` says where the chapters stand.
- A PDF sent with no chapter open is a whole chapter by itself.

Not in this version: ``/undo`` and ``/newnovel``.

Two files, both gitignored: ``telegram.json`` (the token and who is paired; written
rarely) and ``telegram_state.json`` (how far the messages have been taken in, and the
chapter open; saved after every message, so a restart carries on where it stopped).
Nothing here imports app.py: app.py hands over what it needs as ``hooks``.
"""

from __future__ import annotations

import copy
import hmac
import json
import os
import queue
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from translation_bot.atomic import atomic_write_text, quarantine_unreadable

from . import errors
from . import hands_free
from . import intake
from . import pages as pages_mod
from . import pdf_pages
from . import projects as pj
from . import remote
from .locks import file_lock

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,64}$")
MAX_DOWNLOAD = 20 * 1024 * 1024      # the most a bot may fetch from Telegram
PDF_PAGES = 150
PAIR_MINUTES = 10
PAIR_TRIES = 5
RETRIES = 5                          # tries at fetching one page before giving up on it
POLL_S = 25
KEPT_S = 23 * 3600                   # Telegram keeps undelivered messages for about a day

TIP = ("For the sharpest pages, send them as files: choose the photos, then ⋮ → Send "
       "without compression.")
HELP = ("1. /novel — choose the novel.\n"
        "2. Send the chapter's pages in order: photos, an album, or a scanner PDF.\n"
        "3. /done — it is then read, built and translated by itself. I'll message you when "
        "it's translated, or if a page needs you.\n\n"
        "/chapter [title] starts the next chapter, finishing the one open. /status says "
        "where things stand.\n\n" + TIP)
NOT_A_PAGE = "I take photos of pages and scanner PDFs. /help says how."
TOO_BIG = ("That file is over 20 MB, the most a bot may fetch. Use the phone's Share → "
           "Night Reader instead.")
HEIC = ("That's a HEIC photo, which can't be read. Send it as a photo rather than a file — "
        "Telegram turns it into a JPEG — or turn off High efficiency pictures in the "
        "camera's settings.")
FINISHED = "Reading them now; I'll message you when it's translated, or if a page needs you."
STOPPED = ("Hands-free is stopped for this novel, so it waits: press Resume in Night "
           "Reader to read and translate it.")


def settings_path() -> Path:
    return Path(os.environ.get("NR_TELEGRAM_SETTINGS") or (ROOT / "telegram.json"))


def state_path() -> Path:
    return Path(os.environ.get("NR_TELEGRAM_STATE") or (ROOT / "telegram_state.json"))


SETTINGS = {"token": "", "bot": None, "paired": None, "pairing": None, "notify": True}
STATE = {"bot_id": None, "offset": 0, "handled": [], "retries": {},
         "session": {"pid": None, "chapter": None}, "last_poll_at": None, "error": None}


def _read(path: Path, default: dict, *, strict: bool = False) -> dict:
    """The file, or ``default`` when there isn't one. A file that can't be parsed is kept
    aside before anything overwrites it. ``strict`` lets a failed read raise rather than
    look empty, for a change that would otherwise save over the token."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except ValueError:
        quarantine_unreadable(path)
        return copy.deepcopy(default)
    except OSError:
        if strict:
            raise
        return copy.deepcopy(default)
    if not isinstance(found, dict):
        quarantine_unreadable(path)
        return copy.deepcopy(default)
    return {**copy.deepcopy(default), **found}


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2))


def load_settings() -> dict:
    return _read(settings_path(), SETTINGS)


def update_settings(change) -> dict:
    """Read, change and save the settings as one step: the API and the poller both write
    them. ``change`` returning False saves nothing."""
    path = settings_path()
    with file_lock(path):
        doc = _read(path, SETTINGS, strict=True)
        if change(doc) is not False:
            _write(path, doc)
    return doc


def load_state() -> dict:
    return _read(state_path(), STATE)


def save_state(doc: dict) -> None:
    _write(state_path(), doc)


def redact(text, token: str = "") -> str:
    """Never let the token reach a log or an error message."""
    text = str(text)
    return text.replace(token, "<token>") if token else text


# ---- talking to Telegram ------------------------------------------------------------------

class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: float | None = None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class Unreachable(Exception):
    """Telegram couldn't be reached: try again later, losing nothing."""


def _open(request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


class Transport:
    def __init__(self, token: str, opener=None):
        self.token = token
        self.opener = opener or _open

    def call(self, method: str, params: dict | None = None, timeout: float = 35.0):
        data = json.dumps(params or {}).encode("utf-8")
        request = urllib.request.Request(f"{API}/bot{self.token}/{method}", data=data,
                                         headers={"Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=timeout) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Telegram's refusals come as an error status with the reason in the body.
            try:
                answer = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                raise TelegramError(exc.code, "Telegram refused the request.")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise Unreachable(redact(exc, self.token))
        if not isinstance(answer, dict) or not answer.get("ok"):
            answer = answer if isinstance(answer, dict) else {}
            retry = (answer.get("parameters") or {}).get("retry_after")
            raise TelegramError(int(answer.get("error_code") or 0),
                                str(answer.get("description") or ""), retry)
        return answer.get("result")

    def download(self, file_path: str, limit: int = MAX_DOWNLOAD) -> bytes:
        request = urllib.request.Request(f"{API}/file/bot{self.token}/{file_path}")
        try:
            with self.opener(request, timeout=120) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            raise TelegramError(exc.code, "Telegram refused the download.")
        except (urllib.error.URLError, OSError) as exc:
            raise Unreachable(redact(exc, self.token))
        if len(data) > limit:
            raise TelegramError(413, "That file is too big to fetch.")
        return data


class Transient(Exception):
    """A page that couldn't be fetched this time. The messages after it wait, in order."""


class Refused(Exception):
    """A settings change Telegram or the rules turn down, with the HTTP status to say so."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---- the bot -----------------------------------------------------------------------------------

def _message_id(update: dict) -> int:
    return int((update.get("message") or {}).get("message_id") or 0)


def _runs(updates: list[dict]) -> list[list[dict]]:
    """The updates in the order Telegram numbered them, each album's photos kept together
    and put in the order they were sent. Everything else is a run of its own."""
    runs: list[tuple[str | None, list[dict]]] = []
    for update in sorted(updates, key=lambda u: int(u.get("update_id") or 0)):
        group = (update.get("message") or {}).get("media_group_id")
        if group and runs and runs[-1][0] == group:
            runs[-1][1].append(update)
        else:
            runs.append((group, [update]))
    return [sorted(run, key=_message_id) if group else run for group, run in runs]


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class Bot:
    """Takes in what Telegram sends. ``hooks`` is app.py's side of it:

    - ``novels()`` → ``[{id, name, next_number}]``, the photographed novels;
    - ``nudge(pid)`` → photos arrived or a chapter was finished: hands-free looks at it;
    - ``progress(pid)`` → ``{stopped, chapters: [status rows]}`` or None;
    - ``hostname`` → this computer's name, for the pairing reply.

    Tests drive ``poll_once`` and ``handle_batch`` directly, with no threads.
    """

    def __init__(self, transport: Transport, hooks, *, clock=time.time, alive=lambda: True):
        self.transport = transport
        self.hooks = hooks
        self.clock = clock
        self.alive = alive
        self._report: dict[str, dict] = {}

    def say(self, chat_id, text: str, **extra) -> None:
        # Plain text, no parse_mode, so a Korean title never needs escaping.
        try:
            self.transport.call("sendMessage", {"chat_id": chat_id, "text": text[:4000], **extra},
                                timeout=20)
        except (TelegramError, Unreachable):
            pass   # a lost reply is not worth holding up the pages for

    # -- polling
    def poll_once(self) -> int:
        updates = self.transport.call("getUpdates", {
            "offset": int(load_state().get("offset") or 0), "timeout": POLL_S,
            "allowed_updates": ["message", "callback_query"]}, timeout=POLL_S + 10)
        if not self.alive():
            return 0
        with _handling:
            state = load_state()
            before = state.get("last_poll_at")
            state["last_poll_at"] = self.clock()
            state["error"] = None
            save_state(state)
        if before and self.clock() - float(before) > KEPT_S:
            self._back_after(float(before))
        handled, stuck = self.handle_batch(updates or [])
        if stuck:
            raise Unreachable("A page couldn't be fetched from Telegram; trying again.")
        return handled

    def _back_after(self, since: float) -> None:
        paired = load_settings().get("paired")
        if paired:
            when = time.strftime("%a %d %b, %H:%M", time.localtime(since))
            self.say(paired["chat_id"], f"Night Reader is back. It was off from {when}, and "
                                        f"Telegram only keeps messages for a day, so check that "
                                        f"what you sent then arrived, and send again what didn't.")

    def handle_batch(self, updates: list[dict]) -> tuple[int, bool]:
        """Take in updates in order, saving after each. A page that can't be fetched stops
        the batch there, so nothing sent after it lands before it; after a few tries it is
        given up on, with a message. Returns (how many were taken in, stopped early)."""
        handled = 0
        try:
            with _handling:
                for run in _runs(updates):
                    last = max(int(u.get("update_id") or 0) for u in run)
                    state = load_state()
                    if last < int(state.get("offset") or 0):
                        continue
                    done = set(state.get("handled") or [])
                    for update in run:
                        if not self.alive():
                            return handled, False
                        uid = int(update.get("update_id") or 0)
                        if uid in done:
                            continue   # an album taken in part way before a retry
                        try:
                            self.handle(update, state)
                        except Exception as exc:
                            # What the failed message changed is not kept: it starts afresh
                            # next time, from what the messages before it saved.
                            state = load_state()
                            retries = state.setdefault("retries", {})
                            tries = int(retries.get(str(uid), 0)) + 1
                            if tries < RETRIES:
                                retries[str(uid)] = tries
                                save_state(state)
                                if isinstance(exc, Transient):
                                    return handled, True
                                raise
                            if not isinstance(exc, Transient):
                                errors.log_error(errors.explain(exc), where="telegram/message")
                            chat = ((update.get("message") or {}).get("chat") or {}).get("id")
                            if chat is not None:
                                self.say(chat, "A page couldn't be fetched from Telegram, so it "
                                               "was left out. Send it again."
                                         if isinstance(exc, Transient) else
                                         "Something went wrong with that message, so it was "
                                         "left out. Send it again.")
                        state.setdefault("retries", {}).pop(str(uid), None)
                        done.add(uid)
                        state["handled"] = sorted(done)
                        save_state(state)
                        handled += 1
                    state["offset"] = last + 1
                    state["handled"] = []
                    save_state(state)
        finally:
            self._send_reports()
        return handled, False

    def handle(self, update: dict, state: dict) -> None:
        settings = load_settings()
        if "callback_query" in update:
            self._callback(update["callback_query"], settings, state)
            return
        message = update.get("message")
        if not message:
            return
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if not settings.get("paired"):
            self._pairing(message, chat, user)
            return
        paired = settings["paired"]
        if chat.get("id") != paired.get("chat_id") or user.get("id") != paired.get("user_id"):
            return
        chat_id = chat["id"]
        text = (message.get("text") or "").strip()
        caption = (message.get("caption") or "").strip()
        if text.startswith("/"):
            self._command(text, chat_id, state)
            return
        # /chapter as a photo's caption starts the chapter with that photo. Anything else
        # there isn't followed: an album's caption rides on its first photo, so a /done
        # there would finish the chapter one page in.
        word = caption.split(maxsplit=1)[0].split("@")[0].lower() if caption.startswith("/") else ""
        if word == "/chapter":
            self._command(caption, chat_id, state)
        elif word:
            self.say(chat_id, f"Send {word} as a message of its own, after the pages.")
        if message.get("photo") or message.get("document"):
            self._media(message, chat_id, state)
        elif text:
            self.say(chat_id, "Send pages as photos or files. /help says how.")
        elif not word:
            self.say(chat_id, NOT_A_PAGE)

    # -- pairing
    def _pairing(self, message: dict, chat: dict, user: dict) -> None:
        if chat.get("type") != "private":
            return
        text = (message.get("text") or "").strip()
        code = text[len("/start"):].strip() if text.startswith("/start") else text
        if not re.fullmatch(r"\d{6}", code):
            self.say(chat["id"], "To pair, send the 6-digit code that Night Reader shows on the "
                                 "computer, in Settings → Telegram.")
            return
        said: dict = {}

        def attempt(settings: dict):
            window = settings.get("pairing")
            if settings.get("paired"):
                return False
            if not window or self.clock() > float(window.get("expires_at") or 0):
                said["text"] = ("Pairing isn't open. On the computer, press Pair in Settings → "
                                "Telegram, then send the code it shows.")
                return False
            if not hmac.compare_digest(code.encode(), str(window.get("code") or "").encode()):
                window["attempts"] = int(window.get("attempts") or 0) + 1
                if window["attempts"] >= PAIR_TRIES:
                    settings["pairing"] = None
                    said["text"] = "Too many wrong codes. Ask the computer for a new one."
                else:
                    said["text"] = "That code doesn't match. Check Settings → Telegram on the computer."
                return True
            settings["paired"] = {
                "chat_id": chat["id"], "user_id": user.get("id"),
                "name": " ".join(filter(None, [user.get("first_name"), user.get("last_name")])),
                "username": user.get("username") or "", "at": self.clock()}
            settings["pairing"] = None
            said["paired"] = True
            return True

        update_settings(attempt)
        if not said.get("paired"):
            if said.get("text"):
                self.say(chat["id"], said["text"])
            return
        try:
            self.transport.call("setMyCommands", {"commands": [
                {"command": "novel", "description": "Choose the novel"},
                {"command": "chapter", "description": "Start a chapter"},
                {"command": "done", "description": "Finish the chapter"},
                {"command": "status", "description": "Where the chapters stand"},
                {"command": "help", "description": "How to send pages"}]}, timeout=20)
        except (TelegramError, Unreachable):
            pass
        self.say(chat["id"], f"Paired with Night Reader on {self.hooks.hostname}. Choose a "
                             f"novel with /novel, then send its pages.\n\n{HELP}")

    # -- commands
    def _command(self, text: str, chat_id, state: dict) -> None:
        self._send_reports()   # what came before the command is told first
        word, _, rest = text.partition(" ")
        word = word.split("@")[0].lower()
        session = state["session"]
        if word == "/novel":
            buttons = [[{"text": f"{n['name'][:40]} — next: Chapter {n.get('next_number') or '?'}",
                         "callback_data": f"n:{n['id']}"}]
                       for n in self.hooks.novels() if len(f"n:{n['id']}".encode()) <= 64][:40]
            if not buttons:
                self.say(chat_id, "There are no photographed novels yet. Make one in Night "
                                  "Reader on the computer first.")
                return
            self.say(chat_id, "Which novel?", reply_markup={"inline_keyboard": buttons})
        elif word == "/chapter":
            if not session.get("pid"):
                self.say(chat_id, "Choose the novel first, with /novel.")
                return
            finished = self._finish(state)
            if finished:
                self.say(chat_id, finished)
            title = rest.strip()[:120]
            session["chapter"] = self._new_chapter(title)
            self.say(chat_id, f"Chapter started{f': {title}' if title else ''}. Send its pages "
                              f"in order, then /done.")
        elif word == "/done":
            self.say(chat_id, self._finish(state)
                     or "No chapter is open. Send its pages to start one, or /chapter.")
        elif word == "/status":
            self.say(chat_id, self._status(state))
        elif word in ("/help", "/start"):
            self.say(chat_id, HELP)
        else:
            self.say(chat_id, "I don't know that one. /help says what I understand.")

    def _new_chapter(self, label: str = "") -> dict:
        # The box is made when the first page arrives, found by this token from then on:
        # a page sent again after a restart lands in the same box, and an empty box is
        # never left blocking the chapters after it.
        return {"token": secrets.token_hex(4), "box": None, "label": label,
                "opened_at": self.clock(), "tipped": False}

    def _callback(self, query: dict, settings: dict, state: dict) -> None:
        paired = settings.get("paired") or {}
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        if (not paired or (query.get("from") or {}).get("id") != paired.get("user_id")
                or chat_id != paired.get("chat_id")):
            return
        self._send_reports()
        try:
            self.transport.call("answerCallbackQuery", {"callback_query_id": query.get("id")},
                                timeout=20)
        except (TelegramError, Unreachable):
            pass
        data = str(query.get("data") or "")
        if not data.startswith("n:"):
            return
        pid = data[2:]
        novel = next((n for n in self.hooks.novels() if n["id"] == pid), None)
        if novel is None:
            self.say(chat_id, "That novel isn't there any more.")
            return
        if state["session"].get("pid") != pid:
            finished = self._finish(state)
            if finished:
                self.say(chat_id, finished)
            state["session"] = {"pid": pid, "chapter": None}
        self.say(chat_id, f"Sending to {novel['name']}. Next is Chapter "
                          f"{novel.get('next_number') or '?'}: send its pages, then /done.")

    def _finish(self, state: dict) -> str | None:
        """Finish the open chapter, so it may build. What to say about it, or None when
        no chapter was open."""
        session = state["session"]
        chapter = session.get("chapter")
        if not chapter:
            return None
        session["chapter"] = None
        pid = session.get("pid")
        if not chapter.get("box") or not pid:
            return "Nothing was sent to that chapter, so there was nothing to finish."
        try:
            with pages_mod.mutate_pages(pid) as doc:
                pages_mod.set_captured(doc, chapter["box"])
        except pages_mod.BoxError as exc:
            if exc.status == 404:
                return "That chapter was deleted on the computer, so it is closed."
            return exc.message
        self.hooks.nudge(pid)
        number = pages_mod.box_numbers(doc).get(chapter["box"])
        count = sum(1 for p in doc.get("pages", []) if p.get("batch") == chapter["box"])
        head = f"Chapter {number} — {_plural(count, 'page', 'pages')}."
        return f"{head} {STOPPED if hands_free.paused(doc) else FINISHED}"

    def _status(self, state: dict) -> str:
        session = state["session"]
        pid = session.get("pid")
        if not pid:
            return "No novel chosen yet: /novel."
        novel = next((n for n in self.hooks.novels() if n["id"] == pid), None)
        if novel is None:
            return "That novel isn't there any more. Choose another with /novel."
        lines = [f"Sending to {novel['name']}."]
        chapter = session.get("chapter")
        if chapter and not chapter.get("box"):
            lines.append("A chapter is open, with no pages yet.")
        progress = self.hooks.progress(pid) or {}
        if progress.get("stopped"):
            lines.append("Hands-free is stopped for this novel: press Resume in Night Reader.")
        for row in (progress.get("chapters") or [])[-10:]:
            lines.append(f"Chapter {row.get('number')}: {row.get('message') or row.get('step')}")
        if len(lines) == 1:
            lines.append("Nothing has been sent to it from here or the phone yet.")
        return "\n".join(lines)[:4000]

    # -- pages
    def _fetch(self, file_id: str) -> tuple[bytes, str]:
        try:
            info = self.transport.call("getFile", {"file_id": file_id}, timeout=30) or {}
            path = str(info.get("file_path") or "")
            return self.transport.download(path), path
        except Unreachable:
            raise Transient()
        except TelegramError as exc:
            if exc.code >= 500 or exc.code == 429:
                raise Transient()
            raise

    def _report_for(self, chapter: dict, chat_id, pid: str, started: bool) -> dict:
        key = chapter["token"]
        if key not in self._report:
            self._report[key] = {"chat": chat_id, "pid": pid, "box": chapter.get("box"),
                                 "added": 0, "duplicates": 0, "started": started, "tip": False}
        return self._report[key]

    def _send_reports(self) -> None:
        """One message per chapter for the pages just taken in, rather than one a page."""
        reports, self._report = self._report, {}
        for report in reports.values():
            lines = []
            if report["added"] and report["box"]:
                doc = pages_mod.load_pages(report["pid"])
                number = pages_mod.box_numbers(doc).get(report["box"])
                total = sum(1 for p in doc.get("pages", []) if p.get("batch") == report["box"])
                if report["started"]:
                    lines.append(f"Started Chapter {number} — "
                                 f"{_plural(total, 'page', 'pages')} so far. Send the rest, "
                                 f"then /done.")
                else:
                    lines.append(f"Chapter {number} — {_plural(total, 'page', 'pages')} so far "
                                 f"(+{report['added']}).")
            if report["duplicates"]:
                lines.append(f"{_plural(report['duplicates'], 'photo was', 'photos were')} in "
                             f"the novel already, so left where they were.")
            if report["tip"]:
                lines.append(TIP)
            if lines:
                self.say(report["chat"], "\n\n".join(lines))

    def _novel(self, state: dict, chat_id) -> str | None:
        """The chosen novel, while it is still there. One deleted on the computer is let
        go of: a page saved into it would bring back a folder of it."""
        pid = state["session"].get("pid")
        if not pid:
            self.say(chat_id, "Choose the novel first, with /novel, then send the pages again.")
            return None
        project = pj.get_project(pid)
        if not project or project.get("source_type") != "images":
            state["session"] = {"pid": None, "chapter": None}
            self.say(chat_id, "That novel isn't there any more. Choose another with /novel, "
                              "then send the pages again.")
            return None
        return pid

    def _media(self, message: dict, chat_id, state: dict) -> None:
        session = state["session"]
        pid = self._novel(state, chat_id)
        if not pid:
            return
        document = message.get("document")
        if document:
            mime = str(document.get("mime_type") or "").lower()
            name = str(document.get("file_name") or "")
            if mime == "application/pdf" or name.lower().endswith(".pdf"):
                self._pdf(document, chat_id, state)
                return
            if mime in ("image/heic", "image/heif") or name.lower().endswith((".heic", ".heif")):
                self.say(chat_id, HEIC)
                return
            if mime and not mime.startswith("image/"):
                self.say(chat_id, "That file isn't a photo or a PDF. " + NOT_A_PAGE)
                return
            if int(document.get("file_size") or 0) > MAX_DOWNLOAD:
                self.say(chat_id, TOO_BIG)
                return
            file_id, compressed = document["file_id"], False
        else:
            # Telegram sends each photo in several sizes; the largest is the one to read.
            biggest = max(message["photo"], key=lambda p: (int(p.get("width") or 0)
                                                            * int(p.get("height") or 0),
                                                            int(p.get("file_size") or 0)))
            file_id, compressed = biggest["file_id"], True
            name = f"telegram-{message.get('message_id')}.jpg"
        chapter = session.get("chapter")
        started = chapter is None
        if started:
            chapter = session["chapter"] = self._new_chapter()
            # Kept before its box is made: taken in again after a crash, this photo then
            # finds that box by the chapter's token, rather than a new chapter's.
            save_state(state)
        origin = f"tg:{chapter['token']}"
        try:
            data, path = self._fetch(file_id)
        except TelegramError:
            self.say(chat_id, "A page couldn't be fetched from Telegram. Send it again.")
            return
        try:
            arrival = intake.add_photo(
                pid, data, batch=chapter.get("box") or "", label=chapter.get("label") or "",
                name=name or Path(path).name, origin=origin, hands_free=True)
        except pages_mod.BoxError as exc:
            if exc.status == 404 and chapter.get("box"):
                # Never quietly into a new box: the rest of the chapter would follow it.
                session["chapter"] = None
                self.say(chat_id, "That chapter was deleted on the computer, so it is closed. "
                                  "Send its pages again to start a new one.")
            elif "HEIC" in exc.message:
                self.say(chat_id, HEIC)
            else:
                self.say(chat_id, exc.message)
            return
        report = self._report_for(chapter, chat_id, pid, started)
        if arrival.duplicate:
            report["duplicates"] += 1
            # Already in this chapter's own box (sent before a crash): that is its box.
            box = pages_mod.find_box(arrival.doc, arrival.page.get("batch"))
            if box is not None and box.get("origin") == origin:
                chapter["box"] = box["id"]
            return
        chapter["box"] = report["box"] = arrival.page["batch"]
        report["added"] += 1
        if compressed and not chapter.get("tipped"):
            chapter["tipped"] = report["tip"] = True
        self.hooks.nudge(pid)

    def _pdf(self, document: dict, chat_id, state: dict) -> None:
        if int(document.get("file_size") or 0) > MAX_DOWNLOAD:
            self.say(chat_id, TOO_BIG)
            return
        self._send_reports()
        session = state["session"]
        pid = session["pid"]
        chapter = session.get("chapter")
        try:
            data, _path = self._fetch(document["file_id"])
        except TelegramError:
            self.say(chat_id, "That PDF couldn't be fetched from Telegram. Send it again.")
            return
        base = Path(str(document.get("file_name") or "scan")).stem[:60] or "scan"
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "in.pdf"
            pdf.write_bytes(data)
            out = Path(folder) / "pages"
            out.mkdir()
            try:
                files = pdf_pages.split_in_child(pdf, out, PDF_PAGES)
            except pdf_pages.PdfError as exc:
                self.say(chat_id, exc.message)
                return
            items = [(f"{base}-{i:03d}{f.suffix}", f.read_bytes())
                     for i, f in enumerate(files, start=1)]
        if chapter:
            origin = f"tg:{chapter['token']}"
        else:
            # Sent again, the same PDF finds the box it made rather than making another.
            unique = re.sub(r"[^A-Za-z0-9_-]", "", str(document.get("file_unique_id") or ""))
            origin = f"tg:pdf-{unique[:40] or secrets.token_hex(4)}"
        try:
            bundle = intake.add_photos(pid, items, batch=(chapter or {}).get("box") or "",
                                       label=(chapter or {}).get("label") or "", origin=origin,
                                       hands_free=True, captured=chapter is None)
        except pages_mod.BoxError as exc:
            if exc.status == 404 and chapter and chapter.get("box"):
                session["chapter"] = None
                self.say(chat_id, "That chapter was deleted on the computer, so it is closed. "
                                  "Send the PDF again to make it a chapter of its own.")
            else:
                self.say(chat_id, exc.message)
            return
        if not bundle.added:
            self.say(chat_id, "Every page of that PDF is in the novel already.")
            return
        number = pages_mod.box_numbers(bundle.doc).get(bundle.batch)
        pages_text = _plural(len(bundle.added), "page", "pages")
        also = (f" ({_plural(len(bundle.duplicates), 'page was', 'pages were')} in the novel "
                f"already.)" if bundle.duplicates else "")
        if chapter:
            chapter["box"] = bundle.batch
            self.say(chat_id, f"Added {pages_text} from the PDF to Chapter {number}.{also} "
                              f"/done when it's all in.")
        else:
            paused = hands_free.paused(bundle.doc)
            self.say(chat_id, f"Chapter {number} — {pages_text} from the PDF.{also} "
                              f"{STOPPED if paused else FINISHED}")
        self.hooks.nudge(pid)


# ---- running it ----------------------------------------------------------------------------------

# Only one batch is taken in at a time: when the token changes, the old poller may still be
# finishing one while the new one starts.
_handling = threading.RLock()


class Runner:
    """The polling and sending threads for one token. Threads, not asyncio tasks: a long
    poll left waiting in the default executor would hold up the app's shutdown."""

    def __init__(self, token: str, hooks):
        self.token = token
        self._stop = threading.Event()
        self.transport = Transport(token)
        self.bot = Bot(self.transport, hooks, alive=lambda: not self._stop.is_set())
        self.outbox: queue.Queue = queue.Queue()
        self.threads = [threading.Thread(target=self._poll, name="telegram-poll", daemon=True),
                        threading.Thread(target=self._send, name="telegram-send", daemon=True)]

    def start(self) -> None:
        remote.add_sink(self.sink)
        for thread in self.threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        remote.remove_sink(self.sink)
        self.outbox.put(None)

    def stopped(self) -> bool:
        return self._stop.is_set()

    def sink(self, title: str, body: str) -> None:
        """The notices ntfy gets, to the paired chat too. Queued, so it never blocks."""
        settings = load_settings()
        if settings.get("paired") and settings.get("notify", True):
            self.outbox.put((settings["paired"]["chat_id"], f"{title}\n\n{body}" if body else title))

    def _error(self, kind: str, message: str) -> None:
        with _handling:
            if self._stop.is_set():
                return   # replaced or removed: its troubles are no one's concern now
            state = load_state()
            state["error"] = {"kind": kind, "message": redact(message, self.token)[:300],
                              "since": time.time()}
            save_state(state)

    def _poll(self) -> None:
        wait = 1.0
        while not self._stop.is_set():
            try:
                self.bot.poll_once()
                wait = 1.0
                continue
            except Unreachable:
                kind, message, pause = "unreachable", "Can't reach Telegram.", wait
            except TelegramError as exc:
                if exc.code in (401, 404):
                    if not self._stop.is_set():
                        self._error("token-rejected", "Telegram no longer accepts this bot's "
                                                      "token. Make a new one with @BotFather.")
                    return
                if exc.code == 409:
                    kind, message, pause = ("conflict", "Something else is reading this bot's "
                                            "messages: another copy of Night Reader, or a webhook.",
                                            15.0)
                elif exc.code == 429:
                    kind, message, pause = ("busy", "Telegram asked for a pause.",
                                            float(exc.retry_after or wait))
                else:
                    kind, message, pause = "error", f"Telegram answered: {exc.description}", wait
            except Exception as exc:  # noqa: BLE001 — one bad message must not stop the bot
                errors.log_error(errors.explain(exc), where="telegram")
                kind, message, pause = "error", str(exc), wait
            if self._stop.is_set():
                return
            self._error(kind, message)
            self._stop.wait(pause)
            wait = min(wait * 2, 60.0)

    def _send(self) -> None:
        while True:
            item = self.outbox.get()
            if item is None or self._stop.is_set():
                return
            self.bot.say(*item)


_runner: Runner | None = None
_hooks = None
_lock = threading.Lock()


def configure(hooks) -> None:
    """app.py's side of the bot (see Bot), given once when the app is imported."""
    global _hooks
    _hooks = hooks


def start(hooks=None) -> bool:
    """Start polling with the saved token, if there is one; called again, it starts over
    with whatever is saved now. False when there is nothing to start."""
    global _runner, _hooks
    with _lock:
        if hooks is not None:
            _hooks = hooks
        if _runner is not None:
            _runner.stop()
            _runner = None
        settings = load_settings()
        if _hooks is None or not settings.get("token"):
            return False
        bot_id = (settings.get("bot") or {}).get("id")
        with _handling:
            # Another bot's offset would skip this one's messages, and its chapter isn't
            # this bot's to carry on.
            if load_state().get("bot_id") != bot_id:
                save_state({**copy.deepcopy(STATE), "bot_id": bot_id})
        _runner = Runner(settings["token"], _hooks)
        _runner.start()
        return True


def stop(wait: float = 0.0) -> None:
    """Stop the bot. ``wait`` is how long to give its threads to finish what they are
    doing; they are daemon threads, so the app never waits on them unless asked."""
    global _runner
    with _lock:
        runner, _runner = _runner, None
    if runner is not None:
        runner.stop()
        for thread in runner.threads:
            if wait and thread.is_alive():
                thread.join(wait)


def running() -> bool:
    runner = _runner
    return runner is not None and not runner.stopped() and runner.threads[0].is_alive()


def open_pairing(settings: dict, clock=time.time) -> dict:
    settings["pairing"] = {"code": f"{secrets.randbelow(1_000_000):06d}",
                           "expires_at": clock() + PAIR_MINUTES * 60, "attempts": 0}
    return settings


def status(local: bool) -> dict:
    """What Settings shows. Never the token; the pairing code only to the computer."""
    settings = load_settings()
    state = load_state()
    bot = settings.get("bot") or {}
    paired = settings.get("paired")
    configured = bool(settings.get("token"))
    out = {
        "configured": configured,
        "bot_username": bot.get("username") or "",
        "paired": ({"name": paired.get("name") or "", "username": paired.get("username") or ""}
                   if paired else None),
        "notify": bool(settings.get("notify", True)),
        "running": running(),
        "last_poll_at": state.get("last_poll_at") if configured else None,
        "error": state.get("error") if configured else None,
        "local": local,
    }
    window = settings.get("pairing")
    if local and window and not paired and float(window.get("expires_at") or 0) > time.time():
        username = bot.get("username") or ""
        out["pairing"] = {"code": window["code"], "expires_at": window["expires_at"],
                          "link": f"https://t.me/{username}?start={window['code']}" if username else ""}
    return out


# ---- what Settings does ------------------------------------------------------------------------

def check_token(token: str, transport: Transport | None = None) -> dict:
    """The bot a token belongs to, once Telegram has confirmed it and nothing else is
    taking its messages. Refused with a reason otherwise."""
    if not TOKEN_RE.match(token):
        raise Refused(400, "That doesn't look like a bot token. Copy it again from @BotFather: "
                           "it looks like 123456789:AAH…")
    transport = transport or Transport(token)
    try:
        me = transport.call("getMe", timeout=15) or {}
        hook = transport.call("getWebhookInfo", timeout=15) or {}
    except Unreachable:
        raise Refused(502, "Can't reach Telegram from this computer. Check its internet "
                           "connection, then try again.")
    except TelegramError as exc:
        if exc.code in (401, 404):
            raise Refused(400, "Telegram doesn't accept that token. Copy it again from @BotFather.")
        raise Refused(502, f"Telegram answered: {redact(exc.description, token)}")
    if hook.get("url"):
        raise Refused(409, "That bot is already connected to something else. Make a new bot "
                           "with @BotFather for Night Reader.")
    return {"id": me.get("id"), "username": me.get("username") or "",
            "name": me.get("first_name") or ""}


def save_token(token: str, transport: Transport | None = None) -> None:
    bot = check_token(token, transport)

    def change(settings: dict) -> None:
        if (settings.get("bot") or {}).get("id") != bot["id"]:
            settings["paired"] = None   # a different bot: whoever was paired never met it
        settings["token"] = token
        settings["bot"] = bot
        if not settings.get("paired"):
            open_pairing(settings)

    update_settings(change)
    start()


def pair() -> None:
    def change(settings: dict) -> None:
        if not settings.get("token"):
            raise Refused(400, "Save the bot's token first.")
        if settings.get("paired"):
            raise Refused(409, "The bot is paired already. Unpair it first to pair someone else.")
        open_pairing(settings)

    update_settings(change)


def unpair() -> None:
    def change(settings: dict) -> None:
        settings["paired"] = None
        settings["pairing"] = None

    update_settings(change)
    with _handling:
        state = load_state()
        state["session"] = copy.deepcopy(STATE["session"])
        save_state(state)


def set_notify(on: bool) -> None:
    def change(settings: dict) -> None:
        settings["notify"] = bool(on)

    update_settings(change)


def send_test(transport: Transport | None = None) -> None:
    settings = load_settings()
    paired = settings.get("paired")
    if not settings.get("token") or not paired:
        raise Refused(400, "Pair the bot first.")
    transport = transport or Transport(settings["token"])
    try:
        transport.call("sendMessage", {"chat_id": paired["chat_id"],
                                       "text": "Night Reader can reach you here."}, timeout=15)
    except Unreachable:
        raise Refused(502, "Can't reach Telegram from this computer.")
    except TelegramError as exc:
        raise Refused(502, f"Telegram answered: {redact(exc.description, settings['token'])}")


def forget() -> None:
    """Stop the bot and remove everything it kept: the token, the pairing, the session."""
    stop()
    with _handling:
        for path in (settings_path(), state_path()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

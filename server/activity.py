"""What the app is doing, in one place: for the sidebar, the Activity page and the icon by
the clock.

Besides the live queue (which app.py owns, because the jobs live there), this keeps:

- **the settings** for the notices and for holding work while the computer is short of
  memory, in ``activity.json`` (gitignored, beside ``telegram.json``);
- **how long a chapter usually takes**, from the last few finished ones, so the queue can
  say when it should be done;
- **today's tally and the failed chapters**, read from every novel's ``state.json`` and
  cached, because the sidebar asks every few seconds;
- **free memory**. This PC has 8 GB and crashed under load in November 2026; each chapter
  starts a Claude Code process of its own, so not starting one while memory is short is
  the part of the load this app can actually take off.

Nothing here imports app.py.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path

from translation_bot import state as state_mod
from translation_bot.atomic import atomic_write_text

from . import projects as pj

ROOT = Path(__file__).resolve().parent.parent
MB = 1024 * 1024

DEFAULTS = {
    "notify_finished": True,      # a novel's queue ran out
    "notify_failed": True,        # a chapter failed
    "notify_limit": True,         # Claude's usage limit paused the work, and when it resumed
    "toasts": True,               # Windows pop-ups from the icon, as well as Telegram/phone
    # "box": in the middle of the screen, on top, until OK is pressed -- the user wants a
    # notice to interrupt them. "corner": the usual notification, which fades and which
    # Windows hides altogether under Focus Assist.
    "popup_style": "box",
    "hold_on_low_memory": False,  # an option, so off until the user turns it on
    "low_memory_mb": 500,
}
CHOICES = {"popup_style": ("box", "corner")}
LOW_MEMORY_RANGE = (100, 16000)
# Once holding for memory, carry on only with this much more than the threshold free, or
# a chapter starting (and its process taking memory) would put it straight back.
RELEASE_MARGIN_MB = 150

_DURATIONS_KEPT = 30
_TYPICAL_OF = 15                  # the median of the last this many chapters
_DURATION_RANGE = (5.0, 6 * 3600.0)   # a skip, or a chapter left over a lunch break, says nothing

_lock = threading.Lock()


def settings_path() -> Path:
    return Path(os.environ.get("NR_ACTIVITY_SETTINGS") or (ROOT / "activity.json"))


def _read() -> dict:
    try:
        doc = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write(doc: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(doc, indent=2))


# ---- settings ------------------------------------------------------------------------

def _clean(saved: dict) -> dict:
    """The defaults, with each saved value that has the right type laid over them. ``type``
    rather than ``isinstance``, because True is an int and must not become a memory size."""
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = saved.get(key)
        if type(value) is type(default) and value in CHOICES.get(key, (value,)):
            out[key] = value
    return out


def load_settings() -> dict:
    return _clean(_read().get("settings") or {})


def update_settings(change: dict) -> dict:
    """Change some settings and return all of them. Refuses an unknown name, a value of
    the wrong type, or a memory threshold outside ``LOW_MEMORY_RANGE``."""
    with _lock:
        doc = _read()
        current = _clean(doc.get("settings") or {})
        for key, value in (change or {}).items():
            if key not in DEFAULTS:
                raise ValueError(f"There is no setting called {key!r}.")
            if type(value) is not type(DEFAULTS[key]):
                raise ValueError(f"{key} must be {type(DEFAULTS[key]).__name__}.")
            if key == "low_memory_mb" and not LOW_MEMORY_RANGE[0] <= value <= LOW_MEMORY_RANGE[1]:
                raise ValueError(f"The memory threshold must be between {LOW_MEMORY_RANGE[0]} "
                                 f"and {LOW_MEMORY_RANGE[1]} MB.")
            if key in CHOICES and value not in CHOICES[key]:
                raise ValueError(f"{key} must be one of {', '.join(CHOICES[key])}.")
            current[key] = value
        doc["settings"] = current
        _write(doc)
    return current


# ---- the desktop pop-up ---------------------------------------------------------------
#
# The icon by the clock (server/tray.py) registers how it pops a notice up, so the
# Activity page's test button can show one without sending anything to Telegram or the
# phone. None when there is no icon: not Windows, or started some other way.
_popup = {"show": None}


def set_popup(show) -> None:
    _popup["show"] = show


def popup(title: str, body: str) -> bool:
    """Pop a notice up on this PC now, in the chosen style. False when nothing can."""
    show = _popup["show"]
    if show is None:
        return False
    show(title, body)
    return True


# ---- how long a chapter takes ---------------------------------------------------------

_durations: deque | None = None


def _load_durations() -> deque:
    global _durations
    if _durations is None:
        saved = _read().get("durations") or []
        _durations = deque(
            (float(x) for x in saved
             if isinstance(x, (int, float)) and not isinstance(x, bool)
             and _DURATION_RANGE[0] <= x <= _DURATION_RANGE[1]),
            maxlen=_DURATIONS_KEPT)
    return _durations


def record_duration(seconds: float) -> None:
    """A chapter finished in ``seconds``. Kept on disk too, so the first estimate after a
    restart isn't a blank."""
    if not _DURATION_RANGE[0] <= seconds <= _DURATION_RANGE[1]:
        return
    with _lock:
        durations = _load_durations()
        durations.append(round(seconds, 1))
        doc = _read()
        doc["durations"] = list(durations)
        _write(doc)


def typical_seconds() -> float | None:
    """How long a chapter usually takes: the median of the recent ones, which one stalled
    retry doesn't drag the way it would an average. Until a chapter has been timed here,
    the pace read off the library (``library_pace``) stands in, so "done around" shows
    from the first chapter rather than after it."""
    recent = list(_load_durations())[-_TYPICAL_OF:]
    if recent:
        return float(statistics.median(recent))
    return library_pace()


_PACE_TTL = 3600.0
_PACE_WINDOW = 7 * 86400.0          # finishes older than a week say little about now
_PACE_GAP = (20.0, 1800.0)          # shorter is an import, longer is a break between runs
_pace = {"at": None, "value": None}


def _epoch(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return None


def library_pace(now: float | None = None) -> float | None:
    """How long a chapter has been taking, read from when recent ones finished.

    A novel has one worker, so its chapters finish one after another, and the gap between
    two finishing back to back is how long the second took. Gaps shorter than 20 s are
    chapters that arrived already translated; longer than half an hour, a break between
    runs. The median of the 30 most recent gaps; cached for an hour.
    """
    now = time.time() if now is None else now
    if _pace["at"] is not None and time.monotonic() - _pace["at"] < _PACE_TTL:
        return _pace["value"]
    gaps: list[tuple[float, float]] = []
    for project in pj.list_projects():
        pid = str(project.get("id") or "")
        if not pid:
            continue
        stamps = sorted(
            t for rec in _chapters(pj.PROJECTS_DIR / pid / "state.json").values()
            if isinstance(rec, dict) and rec.get("status") in state_mod.TRANSLATED_STATUSES
            and (t := _epoch(rec.get("translated_at"))) is not None and now - t < _PACE_WINDOW)
        gaps += [(later, later - earlier) for earlier, later in zip(stamps, stamps[1:])
                 if _PACE_GAP[0] <= later - earlier <= _PACE_GAP[1]]
    recent = [gap for _end, gap in sorted(gaps, reverse=True)[:_DURATIONS_KEPT]]
    _pace.update(at=time.monotonic(), value=float(statistics.median(recent)) if recent else None)
    return _pace["value"]


def job_eta(*, now: float, typical: float | None, chapters_waiting: int,
            elapsed: float | None, waiting_until: float | None = None,
            held: bool = False) -> float | None:
    """When a novel's queue should be done (epoch seconds), or None when that can't be
    said: nothing measured yet, or the queue is held and won't move until someone acts.

    ``elapsed`` is how long the chapter in progress has run, None when none is. A chapter
    running over the usual time is given a tenth of it more, rather than "done now".
    """
    if held or typical is None:
        return None
    left = chapters_waiting * typical
    if elapsed is not None:
        left += max(typical - elapsed, typical * 0.1)
    return max(now, waiting_until or 0.0) + left


# ---- memory ---------------------------------------------------------------------------

def free_memory() -> dict | None:
    """``{"free_mb", "total_mb"}`` of physical memory, or None where it can't be read."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return {"free_mb": int(status.ullAvailPhys // MB),
                    "total_mb": int(status.ullTotalPhys // MB)}
        fields = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, _, rest = line.partition(":")
            fields[name] = int(rest.split()[0]) // 1024
        return {"free_mb": fields["MemAvailable"], "total_mb": fields["MemTotal"]}
    except Exception:  # noqa: BLE001 - a gauge that can't be read is simply not shown
        return None


def memory_low(settings: dict, mem: dict | None, *, holding: bool = False) -> bool:
    """Whether to hold new work for memory. While already holding, it takes
    ``RELEASE_MARGIN_MB`` more than the threshold to let go."""
    if not settings.get("hold_on_low_memory") or not mem:
        return False
    line = settings["low_memory_mb"] + (RELEASE_MARGIN_MB if holding else 0)
    return mem["free_mb"] < line


def memory_state(settings: dict, mem: dict | None = None) -> dict | None:
    mem = free_memory() if mem is None else mem
    if not mem:
        return None
    return {**mem, "low": mem["free_mb"] < settings["low_memory_mb"],
            "hold_on": settings["hold_on_low_memory"], "threshold_mb": settings["low_memory_mb"]}


# ---- today's tally and the failed chapters -------------------------------------------

_SCAN_TTL = 30.0
_scan = {"at": 0.0, "day": None, "result": None}
_files: dict[str, tuple[tuple[int, int], dict]] = {}


def invalidate() -> None:
    """A chapter just finished or failed: the next report reads the files again."""
    _scan["at"] = 0.0


def _local_date(stamp: str | None) -> date | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone().date()
    except (TypeError, ValueError):
        return None


def _chapters(path: Path) -> dict:
    """A novel's chapter records, re-read only when the file has changed. Size as well as
    time, because two saves inside one tick of the file system's clock share a time."""
    try:
        info = path.stat()
    except OSError:
        return {}
    stamp = (info.st_mtime_ns, info.st_size)
    key = str(path)
    cached = _files.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        chapters = doc.get("chapters") if isinstance(doc, dict) else None
        chapters = chapters if isinstance(chapters, dict) else {}
    except (OSError, ValueError):
        chapters = {}
    _files[key] = (stamp, chapters)
    return chapters


def library_report(now: float | None = None) -> dict:
    """``{"today": {"done", "failed"}, "failed": [...], "last_failed_at"}``.

    ``failed`` is every chapter whose status is failed right now, newest first, across the
    novels on the shelf (an archived novel's old failures are not today's business).
    Cached for half a minute, and dropped early by ``invalidate``.
    """
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).astimezone().date()
    if (_scan["result"] is not None and _scan["day"] == today
            and time.monotonic() - _scan["at"] < _SCAN_TTL):
        return _scan["result"]
    done = failed_today = 0
    failed: list[dict] = []
    for project in pj.list_projects():
        pid = str(project.get("id") or "")
        if not pid:
            continue
        for key, rec in _chapters(pj.PROJECTS_DIR / pid / "state.json").items():
            if not isinstance(rec, dict):
                continue
            status = rec.get("status")
            if status in state_mod.TRANSLATED_STATUSES:
                if _local_date(state_mod.translated_at(rec)) == today:
                    done += 1
            elif status == state_mod.STATUS_FAILED:
                at = rec.get("updated_at")
                if _local_date(at) == today:
                    failed_today += 1
                if project.get("archived"):
                    continue
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    continue
                failed.append({"pid": pid, "name": project.get("name") or "Novel",
                               "index": index, "title": rec.get("title") or "",
                               "error": short_reason(rec.get("error") or ""), "at": at})
    failed.sort(key=lambda f: f["at"] or "", reverse=True)
    result = {"today": {"done": done, "failed": failed_today}, "failed": failed,
              "last_failed_at": failed[0]["at"] if failed else None}
    _scan.update(at=time.monotonic(), day=today, result=result)
    return result


def short_reason(error: str, limit: int = 200) -> str:
    """An error as one readable line: without the exception class in front of it."""
    text = " ".join(str(error or "").split())
    head, sep, rest = text.partition(": ")
    if sep and head.replace(".", "").replace("_", "").isalnum() and head[:1].isupper():
        text = rest
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---- notices ---------------------------------------------------------------------------

_LIMIT_EVERY = 30 * 60.0          # one "waiting for Claude" across all novels, at most this often
_last_limit_notice = {"at": 0.0}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class JobNotices:
    """What one queue says to the phone, Telegram and the pop-ups as it goes.

    ``heard`` turns an event into ``(title, body)`` or None. Novels the phone feeds
    (hands-free) already get their own notices, so their chapters are left to those.
    """

    def __init__(self) -> None:
        self.done = 0
        self.flagged = 0
        self.failed = 0
        self.told_limit = False

    def heard(self, ev: dict, *, novel: str, settings: dict, hands_free_job: bool = False,
              hands_free_chapter: bool = False, cancelled: bool = False,
              clock=time.time) -> tuple[str, str] | None:
        kind = ev.get("type")
        if kind == "chapter":
            if (ev.get("aborted") or ev.get("skipped") or ev.get("refused")
                    or ev.get("page_id") or ev.get("index") is None):
                return None
            if ev.get("status") == state_mod.STATUS_FAILED:
                self.failed += 1
                if not settings.get("notify_failed") or hands_free_chapter:
                    return None
                why = ((ev.get("explain") or {}).get("title") or short_reason(ev.get("error") or "")
                       or "no reason was given")
                return f"Chapter {ev['index']} failed", f"{novel}: {why}"
            self.done += 1
            if ev.get("status") == state_mod.STATUS_NEEDS_REVIEW:
                self.flagged += 1
            return None
        if kind == "waiting":
            now = clock()
            if (not settings.get("notify_limit") or hands_free_job or self.told_limit
                    or now - _last_limit_notice["at"] < _LIMIT_EVERY):
                return None
            self.told_limit = True
            _last_limit_notice["at"] = now
            at = ev.get("resume_at")
            when = f" around {time.strftime('%H:%M', time.localtime(at))}" if at else ""
            return ("Waiting for Claude's limit",
                    f"{novel}: it carries on by itself{when}.")
        if kind == "resumed":
            if not self.told_limit:
                return None
            self.told_limit = False
            return "Back to work", f"{novel}: Claude's limit has reset, so it's translating again."
        if kind == "paused":
            if not settings.get("notify_limit") or hands_free_job:
                return None
            return ("Stopped for Claude's limit",
                    f"{novel}: the limit kept being hit, so it stopped. Press Resume on the "
                    f"novel to carry on.")
        if kind == "done":
            worked = self.done + self.failed
            if not settings.get("notify_finished") or hands_free_job or cancelled or not worked:
                return None
            parts = [f"{_plural(self.done, 'chapter')} translated"]
            if self.flagged:
                parts.append(f"{self.flagged} to look over")
            if self.failed:
                parts.append(f"{self.failed} failed")
            return f"Finished: {novel}", ", ".join(parts) + "."
        return None

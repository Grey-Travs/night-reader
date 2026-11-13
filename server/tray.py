"""Night Reader's icon by the clock (the notification area), so it is plain whether the
app is open, and it can be closed without hunting through Task Manager for pythonw.exe.

Left-click opens the app in the browser. Right-click: Open, Restart, Quit. Quitting or
restarting while a job is still working asks first, because no job survives a restart.

Hovering says what it is doing ("ch 87 of When That Day Comes · 12 left · done ~21:40"),
and a dot on the icon says how it is going: green working, amber waiting (Claude's
limit, paused, or short of memory), red a chapter failed in the last hour. The notices
the phone and Telegram get also pop up here, unless that is switched off on Activity.

Optional on purpose: on an install from before pystray and Pillow were listed, or on
anything but Windows, the app runs exactly as before, just without the icon.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

ICON = Path(__file__).resolve().parent.parent / "web" / "public" / "icon-192.png"
TITLE = "Night Reader"
REFRESH_S = 5.0
TIP_MAX = 120                   # Windows cuts a tray tooltip at 127 characters
FAILED_RECENT_S = 3600.0
DOTS = {"working": (46, 160, 67), "waiting": (219, 154, 4), "failed": (207, 34, 46)}


def snapshot() -> dict | None:
    """What the app is doing, read in-process like ``jobs_running``. None until uvicorn
    has imported the app."""
    app = sys.modules.get("server.app")
    try:
        return app.activity_snapshot(brief=True) if app else None
    except Exception:  # noqa: BLE001 - a tooltip must never crash the icon
        return None


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def tooltip(snap: dict | None) -> str:
    """One line for hovering over the icon."""
    if not snap:
        return TITLE
    jobs = snap.get("jobs") or []
    today = snap.get("today") or {}
    tally = f"today {today.get('done', 0)} done" + (
        f", {today['failed']} failed" if today.get("failed") else "")
    if not jobs:
        text = f"{TITLE}: nothing running · {tally}"
    else:
        job = next((j for j in jobs if j.get("current") is not None), jobs[0])
        if job.get("waiting"):
            at = job["waiting"].get("resume_at")
            doing = f"waiting for Claude's limit{f' until {_clock(at)}' if at else ''}"
        elif job.get("hold"):
            doing = ("paused" if job["hold"].get("reason") == "paused"
                     else "holding: memory is low")
        elif job.get("current") is not None:
            noun = "page" if job.get("kind") in ("ocr", "ocr-verify") else "ch"
            doing = f"{noun} {job['current']}"
        else:
            doing = "starting"
        parts = [f"{doing} · {job.get('name') or 'Novel'}"]
        others = len(jobs) - 1
        parts.append(f"{snap.get('queued', 0)} left" + (f" in {others + 1} novels" if others else ""))
        if snap.get("eta"):
            parts.append(f"done ~{_clock(snap['eta'])}")
        text = f"{TITLE}: " + " · ".join(parts)
    return text if len(text) <= TIP_MAX else text[: TIP_MAX - 1] + "…"


def dot(snap: dict | None, now: float | None = None) -> str | None:
    """Which dot the icon wears: "failed", "waiting", "working", or None when idle."""
    if not snap:
        return None
    now = time.time() if now is None else now
    last = snap.get("last_failed_at")
    if last:
        try:
            if now - datetime.fromisoformat(last).timestamp() < FAILED_RECENT_S:
                return "failed"
        except (TypeError, ValueError):
            pass
    jobs = snap.get("jobs") or []
    if any(j.get("waiting") or j.get("hold") or j.get("paused") for j in jobs):
        return "waiting"
    return "working" if jobs else None


def supported() -> bool:
    return sys.platform == "win32"


def jobs_running() -> int:
    """Jobs still working in this process.

    Read through ``sys.modules`` because uvicorn imports the app itself, by name, after
    the icon is already up; until it has, nothing can be running.
    """
    app = sys.modules.get("server.app")
    try:
        return app.jobs_running() if app else 0
    except Exception:  # noqa: BLE001 - a question for a menu click must never crash it
        return 0


def ask(question: str) -> bool:
    """A yes/no box. Windows only, like the icon itself."""
    import ctypes

    MB_YESNO, MB_ICONWARNING, MB_SETFOREGROUND, MB_TOPMOST, IDYES = 0x4, 0x30, 0x10000, 0x40000, 6
    flags = MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(None, question, TITLE, flags) == IDYES


def _question(jobs: int, restart: bool) -> str:
    what = "A job is" if jobs == 1 else f"{jobs} jobs are"
    if restart:
        return f"{what} still working. Restarting stops it partway.\n\nRestart anyway?"
    return f"{what} still working. Quitting stops it partway.\n\nQuit anyway?"


class Tray:
    """The icon. ``stop(restart)`` is how it asks the launcher to shut the server down."""

    def __init__(self, url: str, stop: Callable[[bool], None], *,
                 busy: Callable[[], int] = jobs_running,
                 confirm: Callable[[str], bool] = ask,
                 open_url: Callable[[str], object] = webbrowser.open) -> None:
        self.url = url
        self._stop = stop
        self._busy = busy
        self._confirm = confirm
        self._open_url = open_url
        self._icon = None
        self._images: dict[str | None, object] = {}
        self._shown: tuple[str, str | None] | None = None
        self._quit = threading.Event()

    def open(self, *_) -> None:
        self._open_url(self.url)

    def quit(self, *_) -> None:
        self._leave(restart=False)

    def restart(self, *_) -> None:
        self._leave(restart=True)

    def _leave(self, *, restart: bool) -> None:
        jobs = self._busy()
        if jobs and not self._confirm(_question(jobs, restart)):
            return
        self._stop(restart)

    def start(self) -> bool:
        """Put the icon up on a thread of its own. False when it can't be shown here."""
        if not supported():
            return False
        try:
            import pystray
            from PIL import Image
        except ImportError:
            return False
        image = Image.open(ICON) if ICON.is_file() else Image.new("RGB", (64, 64), (60, 52, 140))
        self._images = {None: image}
        for name, colour in DOTS.items():
            self._images[name] = _with_dot(image, colour)
        menu = pystray.Menu(
            pystray.MenuItem("Open Night Reader", self.open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart", self.restart),
            pystray.MenuItem("Quit Night Reader", self.quit),
        )
        self._icon = pystray.Icon("NightReader", image, TITLE, menu)
        # pystray makes its window on the thread that runs it, and that thread pumps the
        # window's messages, so the whole icon lives there. Daemon, so it can never be
        # what keeps the process alive.
        threading.Thread(target=self._icon.run, name="tray", daemon=True).start()
        threading.Thread(target=self._watch, name="tray-status", daemon=True).start()
        try:
            from server import remote
            remote.add_sink(self.toast)
        except Exception:  # noqa: BLE001 - pop-ups are a nicety; the icon still works
            pass
        return True

    def refresh(self, snap: dict | None) -> None:
        """Bring the tooltip and the dot up to date, touching the icon only on a change."""
        if self._icon is None:
            return
        shown = (tooltip(snap), dot(snap))
        if shown == self._shown:
            return
        self._shown = shown
        try:
            self._icon.title = shown[0]
            self._icon.icon = self._images.get(shown[1]) or self._images.get(None)
        except Exception:  # noqa: BLE001 - not up yet, or already gone
            self._shown = None

    def _watch(self) -> None:
        while not self._quit.wait(REFRESH_S):
            self.refresh(snapshot())

    def toast(self, title: str, body: str) -> None:
        """A Windows pop-up for a notice. Called by remote.notify on any thread."""
        if self._icon is None:
            return
        try:
            from server import activity
            if not activity.load_settings().get("toasts", True):
                return
            self._icon.notify(body or title, title)
        except Exception:  # noqa: BLE001 - a missed pop-up is not worth an error
            pass

    def stop(self) -> None:
        """Take the icon down, or Windows leaves a dead one by the clock until hovered."""
        self._quit.set()
        try:
            from server import remote
            remote.remove_sink(self.toast)
        except Exception:  # noqa: BLE001
            pass
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001 - stopped before it had finished starting
            pass


def _with_dot(image, colour: tuple[int, int, int]):
    """The icon with a coloured dot in its lower right corner, ringed in white so it
    reads on a dark taskbar and a light one."""
    from PIL import ImageDraw

    out = image.convert("RGBA").copy()
    size = min(out.size)
    radius = size * 0.22
    cx = cy = size - radius - size * 0.04
    draw = ImageDraw.Draw(out)
    ring = radius + size * 0.05
    draw.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), fill=(255, 255, 255, 255))
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*colour, 255))
    return out

"""Night Reader's icon by the clock (the notification area), so it is plain whether the
app is open, and it can be closed without hunting through Task Manager for pythonw.exe.

Left-click opens the app in the browser. Right-click: Open, Restart, Quit. Quitting or
restarting while a job is still working asks first, because no job survives a restart.

Optional on purpose: on an install from before pystray and Pillow were listed, or on
anything but Windows, the app runs exactly as before, just without the icon.
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

ICON = Path(__file__).resolve().parent.parent / "web" / "public" / "icon-192.png"
TITLE = "Night Reader"


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
        return True

    def stop(self) -> None:
        """Take the icon down, or Windows leaves a dead one by the clock until hovered."""
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001 - stopped before it had finished starting
            pass

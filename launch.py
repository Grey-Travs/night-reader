"""One-command launcher for the Translation Bot app.

Ensures config exists, builds the web interface if needed, starts the backend
(which also serves the built interface), and opens your browser. Run it with the
project's virtual-env Python:

    .venv\\Scripts\\python.exe launch.py     (Windows)
    .venv/bin/python launch.py               (macOS/Linux)

Or just double-click start.bat on Windows.

``--background`` is how Windows starts it at sign-in (Settings → This computer): no
browser tab, no console, everything it would print written to logs/launch.log, and a
quiet exit if it is already running.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DIST = WEB / "dist"
VITE = WEB / "node_modules" / "vite" / "bin" / "vite.js"
HOST, PORT = "127.0.0.1", 8000
URL = f"http://localhost:{PORT}"
# Serving every interface rather than just loopback is what lets a phone reach
# the app. Uvicorn binds one address, and loopback has to keep working for the
# desktop browser and the extension, so it is all-or-nothing: the access key is
# what protects it. See server/remote.py.
ANY_INTERFACE = "0.0.0.0"


def ensure_config() -> None:
    cfg = ROOT / "config.toml"
    example = ROOT / "config.example.toml"
    if not cfg.exists() and example.exists():
        shutil.copyfile(example, cfg)
        print("Created config.toml from the example.")


def _newest_mtime(paths) -> float:
    newest = 0.0
    for path in paths:
        try:
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _build_is_stale() -> bool:
    """True when anything the build reads has changed since ``dist/`` was written.

    Without this the launcher only ever built when ``dist/`` was ABSENT, so every run
    after a source edit quietly served the previous interface. ``start.bat`` passes no
    arguments, so ``--rebuild`` was unreachable from the double-click path, and the
    in-app guide's advice ("close the app and run start.bat again") could not pick up
    a frontend change no matter how many times you followed it.
    """
    built = _newest_mtime(p for p in DIST.rglob("*"))
    if not built:
        return True
    sources = [WEB / "index.html", WEB / "package.json", WEB / "vite.config.js"]
    # public/ too: it holds the phone app's service worker, copied into dist/ as it is.
    for folder in (WEB / "src", WEB / "public"):
        if folder.is_dir():
            sources.extend(folder.rglob("*"))
    return _newest_mtime(sources) > built


def _has_built_interface() -> bool:
    """Whether there is actually something to serve.

    index.html specifically, not just a dist/ folder: an interrupted build can leave
    the directory there and empty, and `dist/ exists` would then read as "fine".
    """
    return (DIST / "index.html").is_file()


def _child_output():
    """Our own stdout, for a child process to write into, or None to leave it alone.

    None when there is no real file behind it (pythonw without the log), which is what
    passing nothing always did.
    """
    try:
        sys.stdout.flush()
        sys.stdout.fileno()
        return sys.stdout
    except (AttributeError, OSError, ValueError):
        return None


def build_frontend(force: bool = False) -> bool:
    """Build the interface if needed. Returns whether there is one to serve.

    The return value matters on a FIRST run. A failed build used to print one line
    and carry on, so the launcher started the server and opened a browser onto a
    served-nothing app — and the line had already scrolled away behind uvicorn's
    startup output. On the double-click start.bat path, where the console is often
    not read at all, that looks like the app itself is broken.
    """
    if not VITE.exists():
        print("! Web dependencies aren't installed yet. Run setup first "
              "(see README: `npm install` inside web/).")
        return _has_built_interface()
    first_run = not _has_built_interface()
    if not first_run and not force and not _build_is_stale():
        return True
    print("Building the app interface (first run only)…" if first_run
          else "The interface changed — rebuilding it…")
    # Call vite directly via node — robust across shells. Its output goes where ours does:
    # started in the background that is logs/launch.log, where pythonw would otherwise
    # throw it away and leave "the build failed" with no reason anywhere.
    try:
        subprocess.run(["node", str(VITE), "build"], cwd=str(WEB), check=True,
                       stdout=_child_output(), stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        # A clear nudge beats a raw traceback, and an existing dist/ still runs.
        print("! Building the interface failed."
              + ("" if first_run else " Starting with the previous build instead."))
        return not first_run
    except FileNotFoundError:
        print("! Node.js wasn't found, so the interface couldn't be rebuilt."
              + ("" if first_run else " Starting with the previous build."))
        return not first_run
    return _has_built_interface()


def open_browser_later() -> None:
    time.sleep(2.0)
    webbrowser.open(URL)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def remote_state() -> dict:
    """Whether this app may answer other devices, and how to reach it.

    Read here rather than passed as a flag: the switch in Settings is the deliberate act,
    and needing to remember a command-line argument as well would only mean the feature
    quietly not working after a restart.
    """
    try:
        from server import remote
    except Exception:  # noqa: BLE001 - a broken import must not stop the app starting
        return {"enabled": False, "url": "", "addresses": []}
    try:
        doc = remote.ensure_token() if remote.load()["enabled"] else remote.load()
        return {"enabled": doc["enabled"], "url": remote.phone_url(PORT),
                "addresses": remote.addresses(PORT)}
    except Exception:  # noqa: BLE001
        return {"enabled": False, "url": "", "addresses": []}


def app_is_running() -> bool:
    """Whether an instance of the app is already serving on the usual port.

    A heuristic — it cannot tell this app from anything else that happens to hold
    the port, and it cannot see an instance started by hand on a different one. Good
    enough to stop the common mistake (running a repair tool with the app open),
    which is what ``tools/repair_library.py`` uses it for.
    """
    return _port_in_use(HOST, PORT)


def _to_log() -> None:
    """In the background there is no console — pythonw has none at all — so what would
    be printed goes to logs/launch.log instead, where a problem can still be found."""
    folder = ROOT / "logs"
    folder.mkdir(exist_ok=True)
    stream = open(folder / "launch.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    stream.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} started in the background ---\n")
    sys.stdout = sys.stderr = stream


def _has_console() -> bool:
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleWindow())


def hide_child_windows() -> None:
    """Stop every program the app starts from opening a console window of its own.

    pythonw, which is how Windows starts the app at sign-in, has no console, and Windows
    gives each console program started by a parent without one a window of its own. The
    claude.exe behind every translation is one, so a window popped up per call. The app's
    own calls already pass CREATE_NO_WINDOW (server/remote.py, server/pdf_pages.py); the
    Claude SDK has no option for it, so it is made the default here, for every child.

    Only when there is no console: under start.bat a child shares the visible window,
    which is where, say, the interface build's output belongs.
    """
    if sys.platform != "win32" or _has_console():
        return
    init = subprocess.Popen.__init__
    if getattr(init, "hides_window", False):
        return
    keep = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS

    def __init__(self, *args, creationflags=0, **kwargs):
        # Either of those asks for something else, and Windows ignores CREATE_NO_WINDOW
        # beside them anyway (relaunch() uses DETACHED_PROCESS).
        if not creationflags & keep:
            creationflags |= subprocess.CREATE_NO_WINDOW
        init(self, *args, creationflags=creationflags, **kwargs)

    __init__.hides_window = True
    subprocess.Popen.__init__ = __init__


def main() -> None:
    # Every relative path in config.toml (client_secret.json, token.json, chapters, ...)
    # is read against the working folder. Started at sign-in from the Run key, that folder
    # is System32, not this one — so Google "couldn't find" a client secret sitting right
    # here, and Reconnect failed every time.
    os.chdir(ROOT)
    background = "--background" in sys.argv
    if background:
        _to_log()
    # Dependencies present? (A clear nudge beats a raw ImportError traceback.)
    try:
        import uvicorn  # noqa: F401
    except ModuleNotFoundError:
        print("\n  This app isn't fully installed yet.")
        print("  Run setup first (double-click setup.bat on Windows), then start it again.\n")
        return

    # Already running? Don't crash with "address already in use" — just open it.
    if app_is_running():
        if background:
            print("Already running, so nothing to start.")
            return
        print(f"\n  The app is already running at {URL} — opening it in your browser.\n")
        webbrowser.open(URL)
        return

    hide_child_windows()
    force_build = "--rebuild" in sys.argv
    ensure_config()
    if not build_frontend(force=force_build):
        # Nothing to serve. Starting anyway would open a browser onto a blank page
        # and bury the reason behind uvicorn's startup output.
        print("\n  There's no app interface to open yet, so it wasn't started.")
        print("  Fix the error above and run this again — if you've just installed,")
        print("  try running setup once more (setup.bat on Windows).\n")
        return

    print("\n  Translation Bot is starting…")
    print(f"  Open {URL} in your browser (it should open automatically).")
    away = remote_state()
    host = ANY_INTERFACE if away["enabled"] else HOST
    if away["enabled"]:
        print("\n  Other devices can reach this app, because 'Use from your phone' is on.")
        if away["url"]:
            print(f"  On your phone, open:\n    {away['url']}")
        else:
            print("  No reachable address was found yet - connect Tailscale, or")
            print("  check Settings on this computer for the link.")
        print("  That link carries the access key; anything without it is refused.")
    print("\n  Leave this window open while you use the app. Close it, or use Quit on")
    print("  the Night Reader icon by the clock, to quit.\n")

    if not background:
        threading.Thread(target=open_browser_later, daemon=True).start()

    serve(host)


def relaunch() -> None:
    """Start a fresh copy in the background, the way Windows starts it at sign-in. Only
    called once this one has let go of the port, or the new copy would see it taken and
    quietly bow out."""
    from server import autostart

    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(autostart.command(ROOT), cwd=str(ROOT), creationflags=flags,  # noqa: S603
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)


def serve(host: str) -> None:
    """Run the server until it is stopped, with the icon by the clock up meanwhile.

    The same as ``uvicorn.run``, but holding the server object, so the icon's Quit and
    Restart can stop it cleanly rather than the process being killed.
    """
    import uvicorn

    from server import tray

    server = uvicorn.Server(uvicorn.Config(
        "server.app:app", host=host, port=PORT, log_level="warning",
        # An open browser tab holds a live progress stream, which would otherwise keep
        # Quit waiting for as long as the tab stays open.
        timeout_graceful_shutdown=5))
    leaving: dict[str, bool] = {}

    def stop(restart: bool) -> None:
        leaving["restart"] = restart
        server.should_exit = True

    icon = tray.Tray(URL, stop)
    if not icon.start() and tray.supported():
        print("  (No icon by the clock: run setup again to add it.)\n")
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        icon.stop()
    if leaving:
        if leaving["restart"]:
            relaunch()
        # Exit for certain. A job's worker thread would otherwise keep this copy alive
        # with nothing serving, which is exactly the invisible process the icon is for.
        sys.stdout.flush()
        os._exit(0)
    if not server.started:
        sys.exit(3)  # uvicorn's own code for "could not start"


if __name__ == "__main__":
    main()

"""One-command launcher for the Translation Bot app.

Ensures config exists, builds the web interface if needed, starts the backend
(which also serves the built interface), and opens your browser. Run it with the
project's virtual-env Python:

    .venv\\Scripts\\python.exe launch.py     (Windows)
    .venv/bin/python launch.py               (macOS/Linux)

Or just double-click start.bat on Windows.
"""

from __future__ import annotations

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


def ensure_config() -> None:
    cfg = ROOT / "config.toml"
    example = ROOT / "config.example.toml"
    if not cfg.exists() and example.exists():
        shutil.copyfile(example, cfg)
        print("Created config.toml from the example.")


def build_frontend(force: bool = False) -> None:
    if not VITE.exists():
        print("! Web dependencies aren't installed yet. Run setup first "
              "(see README: `npm install` inside web/).")
        return
    if DIST.exists() and not force:
        return
    print("Building the app interface (first run only)…")
    # Call vite directly via node — robust across shells.
    subprocess.run(["node", str(VITE), "build"], cwd=str(WEB), check=True)


def open_browser_later() -> None:
    time.sleep(2.0)
    webbrowser.open(URL)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def main() -> None:
    # Dependencies present? (A clear nudge beats a raw ImportError traceback.)
    try:
        import uvicorn  # noqa: F401
    except ModuleNotFoundError:
        print("\n  This app isn't fully installed yet.")
        print("  Run setup first (double-click setup.bat on Windows), then start it again.\n")
        return

    # Already running? Don't crash with "address already in use" — just open it.
    if _port_in_use(HOST, PORT):
        print(f"\n  The app is already running at {URL} — opening it in your browser.\n")
        webbrowser.open(URL)
        return

    force_build = "--rebuild" in sys.argv
    ensure_config()
    build_frontend(force=force_build)

    print("\n  Translation Bot is starting…")
    print(f"  Open {URL} in your browser (it should open automatically).")
    print("  Leave this window open while you use the app. Close it to quit.\n")

    threading.Thread(target=open_browser_later, daemon=True).start()

    import uvicorn

    uvicorn.run("server.app:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

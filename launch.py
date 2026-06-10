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


def main() -> None:
    force_build = "--rebuild" in sys.argv
    ensure_config()
    build_frontend(force=force_build)

    print(f"\n  Translation Bot is starting…")
    print(f"  Open {URL} in your browser (it should open automatically).")
    print("  Leave this window open while you use the app. Close it to quit.\n")

    threading.Thread(target=open_browser_later, daemon=True).start()

    import uvicorn

    uvicorn.run("server.app:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

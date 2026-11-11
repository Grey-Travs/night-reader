"""The icon by the clock: what Open, Quit and Restart do, and that it is never required."""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launch  # noqa: E402
from server import autostart, tray  # noqa: E402


def _tray(busy=0, answer=True):
    stops, asked, opened = [], [], []

    def confirm(question):
        asked.append(question)
        return answer

    icon = tray.Tray("http://localhost:8000", stops.append, busy=lambda: busy,
                     confirm=confirm, open_url=opened.append)
    return icon, stops, asked, opened


def test_open_goes_to_the_app():
    icon, _stops, _asked, opened = _tray()
    icon.open()
    assert opened == ["http://localhost:8000"]


def test_quit_with_nothing_running_stops_without_asking():
    icon, stops, asked, _ = _tray(busy=0)
    icon.quit()
    assert stops == [False] and asked == []


def test_restart_asks_the_server_to_restart():
    icon, stops, _asked, _ = _tray(busy=0)
    icon.restart()
    assert stops == [True]


def test_quit_mid_job_asks_first_and_no_means_no():
    icon, stops, asked, _ = _tray(busy=2, answer=False)
    icon.quit()
    assert stops == []
    assert "2 jobs are still working" in asked[0] and "Quit anyway?" in asked[0]


def test_restart_mid_job_goes_ahead_on_yes():
    icon, stops, asked, _ = _tray(busy=1, answer=True)
    icon.restart()
    assert stops == [True] and "A job is still working" in asked[0]
    assert "Restart anyway?" in asked[0]


def test_without_pystray_the_app_still_runs(monkeypatch):
    real_import = builtins.__import__

    def no_pystray(name, *args, **kwargs):
        if name == "pystray":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(tray, "supported", lambda: True)
    monkeypatch.setattr(builtins, "__import__", no_pystray)
    icon, *_ = _tray()
    assert icon.start() is False
    icon.stop()  # nothing up, nothing to take down


def test_jobs_running_before_the_app_is_loaded_is_none(monkeypatch):
    monkeypatch.delitem(sys.modules, "server.app", raising=False)
    assert tray.jobs_running() == 0


def test_the_icon_image_ships_with_the_app():
    assert tray.ICON.is_file()


def test_restart_starts_the_same_command_windows_does(monkeypatch):
    started = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            started.append((cmd, kwargs["cwd"]))

    monkeypatch.setattr(launch.subprocess, "Popen", FakePopen)
    launch.relaunch()
    assert started == [(autostart.command(launch.ROOT), str(launch.ROOT))]


def test_main_works_from_any_folder(monkeypatch, tmp_path):
    """Windows starts it at sign-in from System32; relative config paths must still land here."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launch, "app_is_running", lambda: True)
    monkeypatch.setattr(launch.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(sys, "argv", ["launch.py"])
    launch.main()
    assert Path(os.getcwd()) == launch.ROOT

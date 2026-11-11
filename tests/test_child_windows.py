"""Running in the background, nothing the app starts may pop up a console window."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launch  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="a Windows console matter")


@pytest.fixture
def seen(monkeypatch):
    """What the real Popen would have been asked for. Restored after the test, which also
    takes the wrapper back off."""
    flags = []

    def recorder(self, *args, creationflags=0, **kwargs):
        flags.append(creationflags)

    monkeypatch.setattr(subprocess.Popen, "__init__", recorder)
    return flags


def _start(**kwargs):
    subprocess.Popen.__init__(None, ["claude.exe"], **kwargs)


def test_without_a_console_children_get_no_window(monkeypatch, seen):
    monkeypatch.setattr(launch, "_has_console", lambda: False)
    launch.hide_child_windows()
    _start()
    _start(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    assert seen == [subprocess.CREATE_NO_WINDOW,
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW]


def test_a_detached_relaunch_is_left_as_asked(monkeypatch, seen):
    monkeypatch.setattr(launch, "_has_console", lambda: False)
    launch.hide_child_windows()
    _start(creationflags=subprocess.DETACHED_PROCESS)
    assert seen == [subprocess.DETACHED_PROCESS]


def test_with_a_console_children_share_it(monkeypatch, seen):
    monkeypatch.setattr(launch, "_has_console", lambda: True)
    launch.hide_child_windows()
    _start()
    assert seen == [0]


def test_wrapping_twice_wraps_once(monkeypatch, seen):
    monkeypatch.setattr(launch, "_has_console", lambda: False)
    launch.hide_child_windows()
    once = subprocess.Popen.__init__
    launch.hide_child_windows()
    assert subprocess.Popen.__init__ is once

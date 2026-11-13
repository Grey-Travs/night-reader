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


# ---- what the icon says while it works --------------------------------------------------

def _snap(**over):
    snap = {"jobs": [], "queued": 0, "eta": None, "today": {"done": 0, "failed": 0},
            "last_failed_at": None}
    snap.update(over)
    return snap


def _job(**over):
    job = {"pid": "p1", "name": "When That Day Comes", "kind": "translate", "current": 87,
           "waiting": None, "hold": None, "paused": False}
    job.update(over)
    return job


def test_hovering_says_the_chapter_the_novel_what_is_left_and_when():
    eta = 1_800_000_000.0
    text = tray.tooltip(_snap(jobs=[_job()], queued=12, eta=eta))
    assert text == f"Night Reader: ch 87 · When That Day Comes · 12 left · done ~{tray._clock(eta)}"


def test_hovering_when_idle_gives_todays_tally():
    assert tray.tooltip(_snap(today={"done": 14, "failed": 1})) == \
        "Night Reader: nothing running · today 14 done, 1 failed"
    assert tray.tooltip(None) == "Night Reader"


def test_a_long_tooltip_is_cut_to_what_windows_shows():
    text = tray.tooltip(_snap(jobs=[_job(name="N" * 300)], queued=3))
    assert len(text) <= tray.TIP_MAX and text.endswith("…")


def test_the_dot_says_how_it_is_going():
    now = 1_800_000_000.0
    from datetime import datetime, timezone
    recent = datetime.fromtimestamp(now - 600, timezone.utc).isoformat()
    old = datetime.fromtimestamp(now - 7200, timezone.utc).isoformat()
    assert tray.dot(_snap(), now) is None
    assert tray.dot(_snap(jobs=[_job()]), now) == "working"
    assert tray.dot(_snap(jobs=[_job(waiting={"resume_at": now + 60})]), now) == "waiting"
    assert tray.dot(_snap(jobs=[_job(paused=True)]), now) == "waiting"
    assert tray.dot(_snap(jobs=[_job()], last_failed_at=recent), now) == "failed"
    assert tray.dot(_snap(jobs=[_job()], last_failed_at=old), now) == "working"


def test_refresh_only_touches_the_icon_on_a_change():
    icon, *_ = _tray()

    class FakeIcon:
        title, icon, sets = "", None, 0

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name == "title":
                object.__setattr__(self, "sets", self.sets + 1)

    icon._icon = FakeIcon()
    icon._images = {None: "plain", "working": "green"}
    snap = _snap(jobs=[_job()], queued=2)
    icon.refresh(snap)
    icon.refresh(snap)
    assert icon._icon.sets == 1 and icon._icon.icon == "green"


# ---- the pop-up that waits for OK --------------------------------------------------------

def test_one_notice_is_one_box_and_bad_news_gets_the_warning_icon():
    assert tray.combine([("Finished: Novel", "3 chapters translated.")]) == \
        ("Night Reader: Finished: Novel", "3 chapters translated.", False)
    assert tray.combine([("Chapter 31 failed", "Novel: refusal")])[2] is True


def test_notices_that_arrive_together_share_one_box():
    items = [(f"Chapter {i} failed", "why") for i in range(10)] + [("Finished: N", "done")]
    title, text, warning = tray.combine(items)
    assert title == "Night Reader: 11 notices" and warning
    assert text.count("failed") == tray.POPUP_MAX and text.endswith("…and 3 more.")


def test_boxes_are_shown_one_at_a_time_with_the_backlog_together():
    import threading
    shown, done = [], threading.Event()

    def show(title, text, *, warning=False):
        shown.append(title)
        if len(shown) == 1:
            done.wait(2)   # the first box stays open while more notices arrive

    popups = tray.Popups(show=show, gather=0.05)
    popups.add("Chapter 1 failed", "a")
    import time
    time.sleep(0.2)
    popups.add("Chapter 2 failed", "b")
    popups.add("Finished: N", "c")
    done.set()
    for _ in range(100):
        if len(shown) == 2:
            break
        time.sleep(0.02)
    assert shown == ["Night Reader: Chapter 1 failed", "Night Reader: 2 notices"]


def test_a_notice_pops_up_in_the_chosen_style(monkeypatch, tmp_path):
    from server import activity
    monkeypatch.setenv("NR_ACTIVITY_SETTINGS", str(tmp_path / "activity.json"))
    icon, *_ = _tray()
    boxed, cornered = [], []
    icon._popups = type("P", (), {"add": lambda self, t, b: boxed.append(t)})()
    icon._icon = type("I", (), {"notify": lambda self, b, t: cornered.append(t)})()

    icon.toast("Chapter 3 failed", "why")
    activity.update_settings({"popup_style": "corner"})
    icon.toast("Finished: N", "done")
    activity.update_settings({"toasts": False})
    icon.toast("Chapter 4 failed", "why")
    icon.pop_up("Test pop-up", "shown even with notices off")
    assert boxed == ["Chapter 3 failed"]
    assert cornered == ["Finished: N", "Test pop-up"]


# ---- the status at the top of the right-click menu --------------------------------------

def test_the_menu_lists_each_novel_what_is_left_when_and_failures():
    eta = 1_800_000_000.0
    snap = _snap(jobs=[_job(pending=[2, 3, 4, 5, 6, 7], current=1, name="When That Day Comes 2")],
                 queued=7, eta=eta, failed_count=2, today={"done": 398, "failed": 1})
    assert tray.menu_lines(snap) == [
        "When That Day Comes 2: ch 1, 6 waiting",
        f"7 chapters left · done ~{tray._clock(eta)}",
        "⚠ 2 failed chapters: open to retry",
        "Today: 398 done",
    ]


def test_the_menu_when_idle_or_before_a_finish_time_is_known():
    assert tray.menu_lines(_snap(today={"done": 3, "failed": 0})) == [
        "Nothing running", "No failed chapters", "Today: 3 done"]
    lines = tray.menu_lines(_snap(jobs=[_job(paused=True, current=None, hold={"reason": "paused"})],
                                  queued=4))
    assert lines[:2] == ["When That Day Comes: paused", "4 chapters left · finish time after this chapter"]
    assert tray.menu_lines(None) == ["Starting…"]


def test_many_novels_are_counted_and_an_ampersand_survives_windows():
    jobs = [_job(pid=str(i), name=f"N{i}") for i in range(5)]
    jobs[0]["name"] = "Love & War"
    lines = tray.menu_lines(_snap(jobs=jobs, queued=9))
    assert lines[0].startswith("Love && War: ch 87")
    assert lines[3] == "…and 2 more novels"


def test_the_menu_is_rebuilt_only_when_the_status_changes():
    icon, *_ = _tray()
    updates = []

    class FakeIcon:
        title, icon = "", None

        def update_menu(self):
            updates.append(1)

    icon._icon = FakeIcon()
    icon._images = {None: "plain", "working": "green"}
    snap = _snap(jobs=[_job()], queued=2)
    icon.refresh(snap)
    icon.refresh(snap)
    icon.refresh(_snap(jobs=[_job(current=88)], queued=1))
    assert len(updates) == 2
    import pystray
    items = list(icon.menu_items())
    labels = [item.text for item in items]
    # Actions on top, then a line, then the status.
    assert labels[:5] == ["Open Night Reader", "- - - -", "Restart", "Quit Night Reader", "- - - -"]
    assert labels[5] == "When That Day Comes: ch 88"
    assert items[0].default and items[1] is pystray.Menu.SEPARATOR


def test_a_status_line_opens_the_activity_page():
    icon, _stops, _asked, opened = _tray()
    icon.open_activity()
    assert opened == ["http://localhost:8000/activity"]

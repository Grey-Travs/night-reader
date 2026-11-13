"""Tests for server/activity.py: the settings, how long a chapter takes and when a queue
should be done, the memory hold, today's tally and the failed chapters, and the notices.

Runs under pytest (``pytest tests/``) and standalone (``python tests/test_activity.py``).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.activity as act  # noqa: E402
import server.projects as pj  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("NR_ACTIVITY_SETTINGS", str(tmp_path / "activity.json"))
    monkeypatch.setattr(act, "_durations", None)
    monkeypatch.setitem(act._last_limit_notice, "at", 0.0)
    monkeypatch.setitem(act._pace, "at", None)
    act._files.clear()
    act.invalidate()
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    return root


# ---- settings ---------------------------------------------------------------------

def test_settings_start_from_the_defaults():
    assert act.load_settings() == act.DEFAULTS
    assert act.DEFAULTS["hold_on_low_memory"] is False   # an option, off until chosen


def test_a_change_is_kept_and_the_rest_left_alone():
    act.update_settings({"notify_finished": False, "low_memory_mb": 800})
    settings = act.load_settings()
    assert settings["notify_finished"] is False and settings["low_memory_mb"] == 800
    assert settings["notify_failed"] is True


@pytest.mark.parametrize("change", [
    {"no_such_thing": True},
    {"low_memory_mb": True},        # True is an int in Python; it must not become 1 MB
    {"low_memory_mb": 20},
    {"toasts": "yes"},
    {"popup_style": "loud"},
])
def test_a_bad_setting_is_refused(change):
    with pytest.raises(ValueError):
        act.update_settings(change)
    assert act.load_settings() == act.DEFAULTS


def test_a_hand_edited_file_with_wrong_types_falls_back_to_defaults(tmp_path):
    (tmp_path / "activity.json").write_text(json.dumps(
        {"settings": {"toasts": "no", "low_memory_mb": "lots", "popup_style": "loud"}}),
        encoding="utf-8")
    assert act.load_settings() == act.DEFAULTS


def test_the_pop_up_is_a_box_that_waits_unless_the_corner_is_chosen():
    assert act.load_settings()["popup_style"] == "box"
    assert act.update_settings({"popup_style": "corner"})["popup_style"] == "corner"


def test_a_pop_up_needs_the_icon_to_show_it(monkeypatch):
    monkeypatch.setitem(act._popup, "show", None)
    assert act.popup("Test", "x") is False
    shown = []
    act.set_popup(lambda title, body: shown.append(title))
    assert act.popup("Test", "x") is True and shown == ["Test"]


# ---- how long a chapter takes, and when the queue is done -------------------------------

def test_typical_time_is_the_median_so_one_stall_does_not_drag_it():
    for seconds in (100, 110, 120, 3000):
        act.record_duration(seconds)
    assert act.typical_seconds() == 115


def test_a_skip_says_nothing_about_pace():
    act.record_duration(0.4)
    assert act.typical_seconds() is None


def test_chapter_times_survive_a_restart(monkeypatch):
    act.record_duration(200)
    monkeypatch.setattr(act, "_durations", None)   # as a fresh process would start
    assert act.typical_seconds() == 200


def test_before_any_chapter_is_timed_the_pace_comes_from_the_library(fresh):
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    at = lambda s: (base + timedelta(seconds=s)).isoformat()   # noqa: E731
    _novel(fresh, "alpha", {
        "1": {"status": "validated", "translated_at": at(0)},
        "2": {"status": "validated", "translated_at": at(240)},     # took 4 min
        "3": {"status": "validated", "translated_at": at(480)},     # 4 min
        "4": {"status": "needs-review", "translated_at": at(780)},  # 5 min
        "5": {"status": "validated", "translated_at": at(5000)},    # a break, not a chapter
        "6": {"status": "validated", "translated_at": at(5003)},    # arrived translated
    })
    assert act.library_pace() == 240
    assert act.typical_seconds() == 240
    act.record_duration(100)                 # once a chapter is timed here, that wins
    assert act.typical_seconds() == 100


def test_eta_counts_the_waiting_chapters_and_what_is_left_of_this_one():
    eta = act.job_eta(now=1000, typical=100, chapters_waiting=3, elapsed=40)
    assert eta == 1000 + 3 * 100 + 60


def test_eta_waits_out_claudes_limit_first():
    eta = act.job_eta(now=1000, typical=100, chapters_waiting=1, elapsed=None,
                      waiting_until=5000)
    assert eta == 5100


def test_a_chapter_running_long_is_given_a_little_more_not_zero():
    assert act.job_eta(now=0, typical=100, chapters_waiting=0, elapsed=500) == 10


def test_no_eta_when_held_or_nothing_measured():
    assert act.job_eta(now=0, typical=100, chapters_waiting=2, elapsed=1, held=True) is None
    assert act.job_eta(now=0, typical=None, chapters_waiting=2, elapsed=1) is None


# ---- memory -----------------------------------------------------------------------

def test_memory_holds_only_when_switched_on():
    low = {"free_mb": 300, "total_mb": 8000}
    assert not act.memory_low(act.DEFAULTS, low)
    on = {**act.DEFAULTS, "hold_on_low_memory": True}
    assert act.memory_low(on, low)
    assert not act.memory_low(on, None)


def test_letting_go_needs_more_room_than_holding_did():
    on = {**act.DEFAULTS, "hold_on_low_memory": True, "low_memory_mb": 500}
    just_over = {"free_mb": 550, "total_mb": 8000}
    assert not act.memory_low(on, just_over)                # not holding: fine
    assert act.memory_low(on, just_over, holding=True)      # holding: not yet
    assert not act.memory_low(on, {"free_mb": 700, "total_mb": 8000}, holding=True)


def test_free_memory_reads_something_here():
    mem = act.free_memory()
    assert mem is None or (0 < mem["free_mb"] <= mem["total_mb"])


# ---- today's tally and the failed chapters -------------------------------------------

def _novel(root, pid, chapters, **project):
    (root / pid).mkdir()
    (root / pid / "project.json").write_text(json.dumps({"id": pid, "name": pid.title(), **project}))
    (root / pid / "state.json").write_text(json.dumps({"chapters": chapters}))


def test_today_counts_chapters_finished_and_failed_today(fresh):
    now = datetime.now(timezone.utc)
    today, old = now.isoformat(), (now - timedelta(days=3)).isoformat()
    _novel(fresh, "alpha", {
        "1": {"status": "validated", "translated_at": today},
        "2": {"status": "needs-review", "translated_at": today},
        "3": {"status": "validated", "translated_at": old},
        "4": {"status": "failed", "updated_at": today, "title": "Tab 4",
              "error": "TranslatorError: agent error: stopped: max_tokens"},
        "5": {"status": "failed", "updated_at": old, "error": "boom"},
    })
    _novel(fresh, "shelved", {"1": {"status": "failed", "updated_at": today}}, archived=True)
    report = act.library_report()
    assert report["today"] == {"done": 2, "failed": 2}
    # Newest first, and an archived novel's failures are not listed.
    assert [(f["pid"], f["index"]) for f in report["failed"]] == [("alpha", 4), ("alpha", 5)]
    assert report["failed"][0]["error"] == "agent error: stopped: max_tokens"
    assert report["last_failed_at"] == today


def test_the_report_is_cached_until_a_chapter_changes_it(fresh):
    _novel(fresh, "alpha", {})
    assert act.library_report()["failed"] == []
    (fresh / "alpha" / "state.json").write_text(json.dumps(
        {"chapters": {"9": {"status": "failed"}}}))
    assert act.library_report()["failed"] == []        # still the cached answer
    act.invalidate()
    assert [f["index"] for f in act.library_report()["failed"]] == [9]


def test_short_reason_drops_the_exception_class_and_long_tails():
    assert act.short_reason("TranslatorError: agent error: x") == "agent error: x"
    assert act.short_reason("HTTP 529: overloaded") == "HTTP 529: overloaded"
    assert len(act.short_reason("x" * 500)) == 200


# ---- notices ----------------------------------------------------------------------

ON = act.DEFAULTS


def _heard(notices, ev, **kw):
    return notices.heard(ev, novel="When That Day Comes", settings=kw.pop("settings", ON), **kw)


def test_a_failed_chapter_is_told_with_its_reason():
    n = act.JobNotices()
    ev = {"type": "chapter", "index": 31, "status": "failed",
          "error": "TranslatorError: agent error: success",
          "explain": {"title": "Claude stopped partway through"}}
    assert _heard(n, ev) == ("Chapter 31 failed",
                             "When That Day Comes: Claude stopped partway through")


def test_the_phone_already_tells_its_own_chapters():
    n = act.JobNotices()
    ev = {"type": "chapter", "index": 3, "status": "failed", "error": "x"}
    assert _heard(n, ev, hands_free_chapter=True) is None


def test_finishing_sums_up_what_was_done():
    n = act.JobNotices()
    for i, status in enumerate(["validated", "validated", "needs-review", "failed"], 1):
        _heard(n, {"type": "chapter", "index": i, "status": status}, settings={**ON, "notify_failed": False})
    _heard(n, {"type": "chapter", "index": 9, "status": "validated", "skipped": True})
    assert _heard(n, {"type": "done"}) == (
        "Finished: When That Day Comes", "3 chapters translated, 1 to look over, 1 failed.")


def test_no_finished_notice_after_a_stop_or_when_nothing_was_done():
    n = act.JobNotices()
    assert _heard(n, {"type": "done"}) is None
    _heard(n, {"type": "chapter", "index": 1, "status": "validated"})
    assert _heard(n, {"type": "done"}, cancelled=True) is None


def test_claudes_limit_is_told_once_and_its_end_too():
    n = act.JobNotices()
    first = _heard(n, {"type": "waiting", "resume_at": None}, clock=lambda: 10_000.0)
    assert first[0] == "Waiting for Claude's limit"
    assert _heard(n, {"type": "waiting"}, clock=lambda: 10_001.0) is None
    assert _heard(n, {"type": "resumed"})[0] == "Back to work"
    assert _heard(n, {"type": "resumed"}) is None


def test_two_novels_hitting_the_limit_together_send_one_notice():
    a, b = act.JobNotices(), act.JobNotices()
    assert _heard(a, {"type": "waiting"}, clock=lambda: 50_000.0)
    assert _heard(b, {"type": "waiting"}, clock=lambda: 50_060.0) is None
    assert _heard(b, {"type": "resumed"}) is None   # it never said it was waiting


def test_switched_off_notices_stay_quiet():
    off = {**ON, "notify_failed": False, "notify_finished": False, "notify_limit": False}
    n = act.JobNotices()
    assert _heard(n, {"type": "chapter", "index": 1, "status": "failed"}, settings=off) is None
    assert _heard(n, {"type": "waiting"}, settings=off) is None
    assert _heard(n, {"type": "paused"}, settings=off) is None
    assert _heard(n, {"type": "done"}, settings=off) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

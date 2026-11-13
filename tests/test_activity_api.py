"""Tests for the Activity endpoints and the worker's side of them: the snapshot the
sidebar, the Activity page and the tray read; Pause, Continue and "do this one next";
the hold between chapters; and a job's events turning into notices.

A Job is put in place by hand, with no worker, so nothing here calls Claude.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_activity_api.py``).
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.activity as activity  # noqa: E402
import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
import server.remote as remote  # noqa: E402
from translation_bot.config import Config  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    for module in (pj, P):
        monkeypatch.setattr(module, "PROJECTS_DIR", root)
    monkeypatch.setenv("NR_ACTIVITY_SETTINGS", str(tmp_path / "activity.json"))
    monkeypatch.setattr(activity, "_durations", None)
    monkeypatch.setitem(activity._last_limit_notice, "at", 0.0)
    monkeypatch.setitem(activity._pace, "at", None)
    activity.invalidate()
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    A._offline_projects.clear()
    yield TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    A._jobs.clear()
    A._active_job_by_project.clear()


def _novel(client, name="When That Day Comes"):
    return client.post("/api/projects/text", json={
        "name": name, "text": "seed", "split_mode": "single"}).json()["id"]


def _job(pid, *, current=2, waiting=(3, 4, 5)):
    """A job mid-chapter, as the worker leaves it, with no worker behind it."""
    job = A.Job("job-" + pid, pid)
    job.enqueue([(i, False, A.TASK_TRANSLATE) for i in waiting])
    job.current = current
    job.item_started_at = time.time() - 30
    job.live = {"index": current, "title": f"Tab {current}", "source": ["가", "나", "다", "라"],
                "english": "One.\n\nTwo.", "chunk": [1, 1], "started_at": job.item_started_at}
    A._jobs[job.id] = job
    A._active_job_by_project[pid] = job.id
    return job


def test_the_snapshot_says_what_is_running_and_how_far_along(client):
    pid = _novel(client)
    _job(pid)
    snap = client.get("/api/activity").json()
    assert snap["queued"] == 4
    job = snap["jobs"][0]
    assert (job["name"], job["current"], job["in_queue"]) == ("When That Day Comes", 2, 4)
    assert job["step"] == "paragraph 2 of 4"
    assert 29 <= job["elapsed"] <= 60
    assert job["eta"] is None and snap["eta"] is None   # nothing measured yet
    assert "failed" in snap and "today" in snap


def test_once_a_chapter_time_is_known_it_says_when_it_will_be_done(client):
    pid = _novel(client)
    _job(pid)
    activity.record_duration(100)
    snap = client.get("/api/activity?brief=true").json()
    # 3 waiting x 100 s, plus what is left of the one running (100 - ~30 s).
    assert 360 <= snap["eta"] - snap["now"] <= 372
    assert "failed" not in snap


def test_the_step_says_when_a_second_look_is_being_taken():
    assert A._step_text({"stage": "checking"}) == "checking the translation"
    assert A._step_text({"source": ["a"] * 9, "english": "", "chunk": [2, 3], "attempt": 2}) \
        == "starting · part 2 of 3 · try 2"


def test_a_waiting_chapter_can_be_done_next(client):
    pid = _novel(client)
    job = _job(pid)
    res = client.post(f"/api/projects/{pid}/translate/next", json={"index": 5})
    assert res.status_code == 200, res.text
    assert res.json()["pending"] == [5, 3, 4]
    assert client.post(f"/api/projects/{pid}/translate/next", json={"index": 99}).status_code == 404
    assert [i for i, _f, _k in job.snapshot_pending()] == [5, 3, 4]


def test_do_next_stays_behind_photos_waiting_to_be_read(client):
    pid = _novel(client)
    job = _job(pid)
    job.enqueue([(7, False, "ocr")], ahead_of_chapters=True)
    assert job.move_to_front(5)
    assert [(i, k) for i, _f, k in job.snapshot_pending()][:2] == [(7, "ocr"), (5, "translate")]


def test_pause_keeps_the_queue_and_continue_lets_it_go(client):
    pid = _novel(client)
    job = _job(pid)
    res = client.post(f"/api/projects/{pid}/translate/pause").json()
    assert res["paused"] is True and res["pending"] == [3, 4, 5]
    assert job.user_paused
    snap = client.get("/api/activity").json()["jobs"][0]
    assert snap["paused"] is True and snap["eta"] is None
    assert client.post(f"/api/projects/{pid}/translate/continue").json()["paused"] is False


def test_pausing_a_novel_with_nothing_running_says_so(client):
    pid = _novel(client)
    assert client.post(f"/api/projects/{pid}/translate/pause").status_code == 409


def test_the_settings_can_be_changed_and_a_bad_one_is_refused(client):
    res = client.put("/api/activity/settings", json={"hold_on_low_memory": True, "low_memory_mb": 700})
    assert res.status_code == 200
    assert res.json()["settings"]["low_memory_mb"] == 700
    assert client.put("/api/activity/settings", json={"low_memory_mb": 5}).status_code == 400
    assert client.put("/api/activity/settings", json={"nonsense": True}).status_code == 422


def test_the_test_pop_up_shows_on_this_pc_only(client, monkeypatch):
    shown = []
    monkeypatch.setitem(activity._popup, "show", lambda title, body: shown.append(title))
    sent = []
    monkeypatch.setattr(remote, "notify", lambda *a: sent.append(a))
    assert client.post("/api/activity/test-popup").json() == {"shown": True}
    assert shown == ["Test pop-up"] and sent == []
    monkeypatch.setitem(activity._popup, "show", None)
    assert client.post("/api/activity/test-popup").status_code == 409


def test_a_paused_job_waits_between_chapters_until_continue(client):
    pid = _novel(client)
    job = _job(pid)
    job.user_paused = True
    seen = []

    async def scenario():
        job.subscribers.append(q := asyncio.Queue())
        waiter = asyncio.create_task(A._wait_while_held(job))
        await asyncio.sleep(0.05)
        assert not waiter.done() and job.hold == {"reason": "paused", "since": job.hold["since"]}
        job.user_paused = False
        job.wake.set()
        await asyncio.wait_for(waiter, 2)
        while not q.empty():
            seen.append(q.get_nowait()["type"])

    asyncio.run(scenario())
    assert seen == ["hold", "released"] and job.hold is None


def test_the_memory_hold_needs_room_to_spare_before_carrying_on(client, monkeypatch):
    pid = _novel(client)
    job = _job(pid)
    activity.update_settings({"hold_on_low_memory": True, "low_memory_mb": 500})
    monkeypatch.setattr(activity, "free_memory", lambda: {"free_mb": 400, "total_mb": 8000})
    assert A._hold_reason(job) == "memory"
    monkeypatch.setattr(activity, "free_memory", lambda: {"free_mb": 560, "total_mb": 8000})
    assert A._hold_reason(job) is None                     # wasn't holding: fine
    assert A._hold_reason(job, holding="memory") == "memory"   # was: not yet


def test_a_failed_chapter_becomes_a_notice(client, monkeypatch):
    pid = _novel(client)
    job = _job(pid)
    sent = []
    monkeypatch.setattr(remote, "notify", lambda title, body="": sent.append((title, body)))
    job.publish({"type": "chapter", "index": 2, "kind": "translate", "status": "failed",
                 "title": "Tab 2", "error": "TranslatorError: agent error: stopped: refusal"})
    assert sent == [("Chapter 2 failed", "When That Day Comes: agent error: stopped: refusal")]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

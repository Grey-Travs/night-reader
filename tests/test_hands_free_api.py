"""Hands-free through the real app: photos in, a translated chapter out, nobody pressing
a button — with the model calls stubbed and every push recorded.

Runs the app's startup (``with TestClient``), since the looks, the nudges and the
restart all live on its event loop.

What has to hold:
- A phone chapter's photos are read as they arrive; once Done is tapped and every page
  is clean, the chapter builds and is translated, and the phone is told.
- A desktop upload is never read by itself.
- An unsure page is double-checked once; still unsure, it holds its chapter and the
  phone is told once. Approving it lets the chapter carry on.
- A photo that can't be read is read once more, then held.
- A chapter heading part way through holds the chapter until the reader decides.
- A later chapter never builds before an earlier one still being photographed.
- Stop pauses hands-free until Resume, and a restart carries on only with what wasn't
  stopped.
- A queued translation whose chapter changed meanwhile is dropped, never translated.
- A problem in a chapter the phone is showing is left to its badges, and pushed once
  nobody is watching.

Runs under pytest (``pytest tests/``).
"""

import asyncio
import itertools
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
import server.remote as R  # noqa: E402
from server import intake  # noqa: E402
from translation_bot import state as state_mod  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.translator import RateLimitedError  # noqa: E402

_photos = itertools.count(1)


def _photo() -> bytes:
    return b"\xff\xd8\xff\xe0" + next(_photos).to_bytes(8, "big") + b"\x00" * 248


class Claude:
    """Stands in for the model: how each photo reads and checks, by its upload name."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reads: list[str] = []
        self.checks: list[str] = []
        self.translated: list[int] = []
        self.unsure: set[str] = set()
        self.disagrees: set[str] = set()
        self.unreadable: set[str] = set()
        self.headings: dict[str, str] = {}
        self.limited = False

    def page(self, kind, pid, page, cfg, translator, hooks):
        name = page.get("name") or ""
        if self.limited:
            raise RateLimitedError(None)
        now = P.now_iso()
        if kind == A.TASK_OCR_VERIFY:
            with self.lock:
                self.checks.append(name)
            verdict = "discrepancies" if name in self.disagrees else "ok"
            return {"verify": {"at": now, "verdict": verdict, "issues": [], "cost_usd": 0.0},
                    "status": P.STATUS_NEEDS_CHECK if verdict != "ok" else P.STATUS_OK,
                    "_usage": {}, "_cost": 0.0}
        with self.lock:
            self.reads.append(name)
        if name in self.unreadable:
            raise RuntimeError("the photo couldn't be read")
        text = f"{name} 쪽의 본문입니다. 이야기가 이어진다."
        sure = name not in self.unsure
        return {"text": text, "raw_text": text, "confidence": "high" if sure else "low",
                "notes": [], "heading": self.headings.get(name),
                "starts_mid_sentence": False, "ends_mid_sentence": False, "ends_mid_word": False,
                "hangul_fraction": 1.0, "chars": len(text), "hint": "", "verify": None,
                "error": None, "status": P.STATUS_OK if sure else P.STATUS_NEEDS_CHECK,
                "ocr": {"at": now, "attempts": 1}, "_usage": {}, "_cost": 0.0}

    def chapter(self, kind, ch, total, translator, glossary, cfg, state, hooks):
        with self.lock:
            self.translated.append(ch.index)
        state.update(ch.index, status=state_mod.STATUS_VALIDATED, title=ch.title,
                     source_hash=ch.metrics.content_hash)
        return state_mod.STATUS_VALIDATED


@pytest.fixture
def world(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    monkeypatch.setattr(P, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    claude = Claude()
    monkeypatch.setattr(A, "_run_page_task", claude.page)
    monkeypatch.setattr(A, "_run_task", claude.chapter)
    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(R, "notify", lambda title, body="": pushed.append((title, body)) or True)
    monkeypatch.setattr(A, "_SETTLE_TEXT", 0.05)
    monkeypatch.setattr(A, "_SETTLE_STATUS", 0.05)
    monkeypatch.setattr(A, "_SETTLE_STRUCTURE", 0.05)
    claude.pushed = pushed
    return claude


@pytest.fixture
def app(world):
    with TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        yield client


def _until(check, timeout=15.0, what="the app to settle"):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _quiet(pid, pause=0.4):
    """Nothing running or about to run for this novel, for a moment."""
    def idle():
        return (A._live_job(pid) is None and pid not in A._settle_timers
                and not (A._looks.get(pid) and not A._looks[pid].done()))
    _until(idle, what="the novel to go quiet")
    time.sleep(pause)
    _until(idle, what="the novel to go quiet")


def _novel(client) -> str:
    res = client.post("/api/projects/images", json={"name": "달빛 소설"})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _phone_box(client, pid, origin="phone:a1b2c3d4") -> str:
    res = client.post(f"/api/projects/{pid}/pages/batches",
                      json={"origin": origin, "hands_free": True})
    assert res.status_code == 200, res.text
    return res.json()["batch"]["id"]


def _send(client, pid, box, name) -> dict:
    res = client.post(f"/api/projects/{pid}/pages", content=_photo(),
                      headers={"Content-Type": "image/jpeg"},
                      params={"batch": box, "name": name})
    assert res.status_code == 200, res.text
    return res.json()["page"]


def _done(client, pid, box):
    res = client.post(f"/api/projects/{pid}/pages/batches/{box}/captured")
    assert res.status_code == 200, res.text


def _status(pid, page_id):
    return P.find_page(P.load_pages(pid), page_id)["status"]


def _built(pid) -> int:
    return len(pj.load_text_chapters(pid))


def _titles(world):
    return [title for title, _body in world.pushed]


def _run_as_the_novels_job(job, cfg):
    """Run a worker by hand, registered as the novel's job the way _spawn_worker does,
    so work a look queues lands on it rather than on a second job."""
    A._jobs[job.id] = job
    A._active_job_by_project[job.pid] = job.id
    try:
        asyncio.run(A._run_worker(job, cfg))
    finally:
        A._jobs.pop(job.id, None)
        if A._active_job_by_project.get(job.pid) == job.id:
            A._active_job_by_project.pop(job.pid, None)


# ---- the whole way through ---------------------------------------------------------------------------

def test_a_phone_chapter_reads_builds_and_translates_by_itself(app, world):
    pid = _novel(app)
    box = _phone_box(app, pid)
    pages = [_send(app, pid, box, f"p{n}") for n in (1, 2, 3)]
    _until(lambda: all(_status(pid, p["id"]) == P.STATUS_OK for p in pages),
           what="every photo to be read")
    _quiet(pid)
    assert _built(pid) == 0, "photos may still be coming, so nothing builds"
    assert world.translated == []

    _done(app, pid, box)
    _until(lambda: world.translated == [1], what="the chapter to be translated")
    _quiet(pid)
    assert _built(pid) == 1
    assert world.reads == ["p1", "p2", "p3"], "each photo read once"
    assert "Chapter 1 is translated" in _titles(world)


def test_a_desktop_upload_is_never_read_by_itself(app, world):
    pid = _novel(app)
    res = app.post(f"/api/projects/{pid}/pages", content=_photo(),
                   headers={"Content-Type": "image/jpeg"}, params={"name": "desk"})
    page = res.json()["page"]
    time.sleep(0.5)
    _quiet(pid)
    assert _status(pid, page["id"]) == P.STATUS_NEW
    assert world.reads == []


def test_an_unsure_page_is_checked_once_then_waits_for_you(app, world):
    world.unsure.add("p2")
    world.disagrees.add("p2")
    pid = _novel(app)
    box = _phone_box(app, pid)
    pages = [_send(app, pid, box, f"p{n}") for n in (1, 2, 3)]
    _done(app, pid, box)
    _until(lambda: (P.find_page(P.load_pages(pid), pages[1]["id"]).get("verify") or {}).get("at"),
           what="the double-check")
    _quiet(pid)
    assert world.checks == ["p2"], "checked once for this reading"
    assert _built(pid) == 0 and world.translated == []
    needs = [t for t in _titles(world) if t == "Chapter 1 needs you"]
    assert len(needs) == 1
    assert "photo 2 needs checking" in world.pushed[_titles(world).index("Chapter 1 needs you")][1]

    # More looks find nothing new to say.
    _done(app, pid, box)
    _quiet(pid)
    assert len([t for t in _titles(world) if t == "Chapter 1 needs you"]) == 1

    res = app.post(f"/api/projects/{pid}/pages/{pages[1]['id']}", json={"status": "ok"})
    assert res.status_code == 200
    _until(lambda: world.translated == [1], what="the chapter to carry on")


def test_a_photo_that_cant_be_read_is_read_once_more_then_held(app, world):
    world.unreadable.add("p1")
    pid = _novel(app)
    box = _phone_box(app, pid)
    page = _send(app, pid, box, "p1")
    _send(app, pid, box, "p2")
    _done(app, pid, box)
    _until(lambda: world.reads.count("p1") == 2, what="the second try")
    _quiet(pid)
    assert world.reads.count("p1") == 2, "once more, and only once"
    assert _status(pid, page["id"]) == P.STATUS_FAILED
    assert _built(pid) == 0
    body = world.pushed[_titles(world).index("Chapter 1 needs you")][1]
    assert "photo 1 couldn't be read" in body


def test_a_heading_part_way_through_holds_until_kept_together(app, world):
    world.headings["p3"] = "제2화"
    pid = _novel(app)
    box = _phone_box(app, pid)
    pages = [_send(app, pid, box, f"p{n}") for n in (1, 2, 3)]
    _done(app, pid, box)
    _until(lambda: "Chapter 1 needs you" in _titles(world), what="the hold to be told")
    _quiet(pid)
    assert _built(pid) == 0
    assert "Photo 3 opens with a chapter heading" in world.pushed[-1][1]

    res = app.post(f"/api/projects/{pid}/pages/{pages[2]['id']}", json={"join_prev": "paragraph"})
    assert res.status_code == 200
    _until(lambda: world.translated == [1], what="the kept-together chapter")


def test_a_later_chapter_waits_for_an_earlier_one_still_being_photographed(app, world):
    pid = _novel(app)
    first = _phone_box(app, pid, "phone:first000")
    _send(app, pid, first, "a1")
    second = _phone_box(app, pid, "phone:second00")
    _send(app, pid, second, "b1")
    _done(app, pid, second)
    _until(lambda: "Chapter 2 needs you" in _titles(world), what="the wait to be told")
    _quiet(pid)
    assert _built(pid) == 0
    assert "which was never finished" in world.pushed[-1][1]

    _done(app, pid, first)
    _until(lambda: world.translated == [1, 2], what="both chapters, in order")
    doc = P.load_pages(pid)
    assert P.find_box(doc, first)["chapter"] == 1 and P.find_box(doc, second)["chapter"] == 2


def test_stop_pauses_hands_free_until_resume(app, world):
    pid = _novel(app)
    box = _phone_box(app, pid)
    _send(app, pid, box, "p1")
    _until(lambda: world.reads == ["p1"])
    _quiet(pid)
    assert app.post(f"/api/projects/{pid}/translate/cancel", json={}).status_code == 200
    assert P.load_pages(pid).get("hands_free_paused_at")
    page = _send(app, pid, box, "p2")
    time.sleep(0.4)
    _quiet(pid)
    assert _status(pid, page["id"]) == P.STATUS_NEW, "stopped means stopped"
    assert app.get(f"/api/projects/{pid}/pages").json()["hands_free_paused_at"]

    res = app.post(f"/api/projects/{pid}/hands-free/resume")
    assert res.status_code == 200 and res.json()["queued_pages"] == 1
    _until(lambda: _status(pid, page["id"]) == P.STATUS_OK)
    assert "hands_free_paused_at" not in P.load_pages(pid)


def test_progress_says_where_each_chapter_stands(app, world):
    world.unsure.add("b1")
    world.disagrees.add("b1")
    pid = _novel(app)
    first = _phone_box(app, pid, "phone:first000")
    _send(app, pid, first, "a1")
    second = _phone_box(app, pid, "phone:second00")
    _send(app, pid, second, "b1")
    _done(app, pid, second)
    _until(lambda: "b1" in world.checks)
    _quiet(pid)
    res = app.get("/api/phone/progress", params={"pid": pid})
    assert res.status_code == 200
    [novel] = res.json()["novels"]
    assert novel["pid"] == pid and novel["stopped"] is False
    steps = {row["batch"]: row for row in novel["chapters"]}
    assert steps[first]["step"] == "capturing"
    assert steps[second]["step"] == "needs-you"
    assert steps[second]["needs_you"][0]["photo"] == 1


def test_a_correction_holds_the_build_back_while_other_photos_are_read(app, world, monkeypatch):
    monkeypatch.setattr(A, "_SETTLE_TEXT", 2.0)
    world.unsure.add("p2")
    world.disagrees.add("p2")
    pid = _novel(app)
    box = _phone_box(app, pid, "phone:first000")
    pages = [_send(app, pid, box, f"p{n}") for n in (1, 2)]
    _done(app, pid, box)
    _until(lambda: world.checks == ["p2"])
    _quiet(pid)

    # Typed corrections arrive as several autosaves; the chapter mustn't build from the
    # first. Meanwhile another chapter's photo is read, which looks again.
    app.post(f"/api/projects/{pid}/pages/{pages[1]['id']}", json={"text": "고쳐 쓴 본문."})
    other = _phone_box(app, pid, "phone:second00")
    _send(app, pid, other, "q1")
    _until(lambda: "q1" in world.reads)
    time.sleep(0.4)
    assert _built(pid) == 0, "built from a half-corrected page"
    _until(lambda: world.translated == [1], what="the chapter once the edits settled")


# ---- the phone is watching ------------------------------------------------------------------------------

def test_a_problem_the_phone_is_showing_is_pushed_only_once_nobody_watches(app, world, monkeypatch):
    monkeypatch.setattr(A, "_WATCH_SECONDS", 1.5)
    monkeypatch.setattr(A, "_WATCH_RECHECK", 1.0)
    world.unsure.add("p1")
    world.disagrees.add("p1")
    pid = _novel(app)
    box = _phone_box(app, pid)

    def watch():
        # The camera asks every couple of seconds while it shows the chapter.
        app.get("/api/phone/progress", params={"pid": pid, "box": box})
        assert "Chapter 1 needs you" not in _titles(world), "the badges are showing it"

    watch()
    _send(app, pid, box, "p1")
    end = time.time() + 15
    while world.checks != ["p1"] and time.time() < end:
        watch()
        time.sleep(0.1)
    assert world.checks == ["p1"]
    for _ in range(10):   # a while longer on screen: still nothing pushed
        watch()
        time.sleep(0.1)
    _until(lambda: "Chapter 1 needs you" in _titles(world), timeout=15,
           what="the push once nobody watches")


# ---- a translation queued for a chapter that changed --------------------------------------------------

def test_a_queued_translation_is_dropped_when_its_chapter_changed(world):
    client = TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    pid = _novel(client)
    box = _phone_box(client, pid)
    for n in (1, 2):
        page = _send(client, pid, box, f"p{n}")
        client.post(f"/api/projects/{pid}/pages/{page['id']}",
                    json={"text": f"{n}쪽 본문입니다.", "status": "ok"})
    _done(client, pid, box)
    assert client.post(f"/api/projects/{pid}/pages/build", json={}).json()["added"] == [1]
    intake.add_photo(pid, _photo(), batch=box, name="late")   # opens the chapter again

    cfg = pj.project_config(Config(), pj.get_project(pid))
    job = A.Job("drop-test", pid)
    job.enqueue([(1, False, A.TASK_TRANSLATE)])
    job.hands_free_chapters.add(1)
    seen = []
    job.publish = seen.append
    _run_as_the_novels_job(job, cfg)
    assert world.translated == [], "half a chapter is never translated"
    dropped = [e for e in seen if e.get("type") == "chapter" and e.get("index") == 1]
    assert dropped and dropped[0]["refused"] is True


def test_work_found_as_the_queue_runs_dry_goes_on_the_same_job(world):
    """The worker looks once more before it finishes, and what that look finds has to
    run: left on a job that then ends, a photo sat marked queued with nothing coming."""
    client = TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    pid = _novel(client)
    box = _phone_box(client, pid)
    page = _send(client, pid, box, "p1")   # no loop here, so nothing reads it yet
    cfg = pj.project_config(Config(), pj.get_project(pid))
    job = A.Job("dry-test", pid)
    _run_as_the_novels_job(job, cfg)
    assert world.reads == ["p1"]
    assert _status(pid, page["id"]) == P.STATUS_OK
    assert job.done and not job.pending


def test_a_claim_takes_only_pages_still_as_planned(world):
    client = TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    pid = _novel(client)
    box = _phone_box(client, pid)
    planned, approved = _send(client, pid, box, "p1"), _send(client, pid, box, "p2")
    desk = client.post(f"/api/projects/{pid}/pages", content=_photo(),
                       headers={"Content-Type": "image/jpeg"}).json()["page"]
    # The reader approved p2 while the plan was being made.
    client.post(f"/api/projects/{pid}/pages/{approved['id']}", json={"text": "본문.", "status": "ok"})
    work = [(p["id"], P.STATUS_NEW, A.TASK_OCR, {}) for p in (planned, approved, desk)]
    claimed, stopped = A._claim_hands_free_pages(pid, work)
    assert stopped is False
    assert claimed == [(planned["seq"], A.TASK_OCR)], "never an approved page, never a desktop one"
    assert _status(pid, approved["id"]) == P.STATUS_OK
    assert _status(pid, desk["id"]) == P.STATUS_NEW

    with P.mutate_pages(pid) as doc:
        doc["hands_free_paused_at"] = P.now_iso()
    assert A._claim_hands_free_pages(pid, work) == ([], True)


# ---- the plan's limit -----------------------------------------------------------------------------------------

def test_giving_up_on_the_plan_schedules_one_retry_and_stop_cancels_it(app, world, monkeypatch):
    monkeypatch.setattr(A, "_rate_limit_resume_at", lambda exc, strikes: (time.time(), None))

    async def no_wait(job, until):
        return None

    monkeypatch.setattr(A, "_sleep_until", no_wait)
    world.limited = True
    pid = _novel(app)
    box = _phone_box(app, pid)
    _send(app, pid, box, "p1")
    _until(lambda: pid in A._retry_timers, what="the retry to be scheduled")
    assert "Stopped waiting for your Claude plan" in _titles(world)
    assert A._gave_up[pid]["retry_at"] > time.time()
    assert "Waiting for your Claude plan" in _titles(world)

    app.post(f"/api/projects/{pid}/translate/cancel", json={})
    _until(lambda: pid not in A._retry_timers, what="Stop to cancel the retry")


# ---- restarts -------------------------------------------------------------------------------------------------

def test_a_restart_carries_on_only_with_what_wasnt_stopped(world):
    setup = TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    going, stopped = _novel(setup), _novel(setup)
    pages = {}
    for pid in (going, stopped):
        first = intake.add_photo(pid, _photo(), origin="phone:restart0", hands_free=True,
                                 name="r1")
        second = intake.add_photo(pid, _photo(), batch=first.page["batch"], name="r2")
        pages[pid] = (first.page["id"], second.page["id"])
        with P.mutate_pages(pid) as doc:
            P.find_page(doc, first.page["id"])["status"] = P.STATUS_RUNNING  # left mid-read
    with P.mutate_pages(stopped) as doc:
        doc["hands_free_paused_at"] = P.now_iso()

    with TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000)):
        _until(lambda: all(_status(going, p) == P.STATUS_OK for p in pages[going]),
               what="the photos to be read after the restart")
        _quiet(going)
        assert "Night Reader is back" in _titles(world)
        assert [_status(stopped, p) for p in pages[stopped]] == [P.STATUS_NEW, P.STATUS_NEW], \
            "released, but never read: it was stopped"

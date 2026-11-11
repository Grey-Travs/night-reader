"""Fixes that have to be in place before anything runs without the reader watching.

- A double-check whose reply can't be understood holds the page for the reader. It used
  to approve the page, which is harmless with someone looking at every page and not at
  all harmless once pages build into chapters by themselves.
- A paused job lists only CHAPTER work as resumable, each item with its own kind and
  force. The client re-queued the whole pending list as forced translations, so a page
  sequence number became "re-translate chapter N", and a chapter queued without force
  (already finished, and about to be skipped) was translated again.
- Starting the app hands back pages the last run left mid-read, and remembers the event
  loop for the threads that will need to reach it.

Runs under pytest (``pytest tests/``).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.ocr import VerifyResult  # noqa: E402
from translation_bot.translator import RateLimitedError  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 256


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    monkeypatch.setattr(P, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    return TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def _novel_with_page(client) -> tuple[str, dict]:
    res = client.post("/api/projects/images", json={"name": "Scanned novel"})
    assert res.status_code == 200, res.text
    pid = res.json()["id"]
    res = client.post(f"/api/projects/{pid}/pages", content=JPEG,
                      headers={"Content-Type": "image/jpeg"})
    assert res.status_code == 200, res.text
    return pid, res.json()["page"]


# ---- an unclear double-check ---------------------------------------------------------

def test_an_unclear_double_check_holds_the_page(client, monkeypatch):
    pid, page = _novel_with_page(client)
    page = {**page, "text": "밖에는 아무도 없었다."}
    monkeypatch.setattr(A.ocr, "verify_page",
                        lambda *a, **k: VerifyResult(verdict="unclear"))
    fields = A._run_page_task(A.TASK_OCR_VERIFY, pid, page, Config(), None, None)
    assert fields["status"] == P.STATUS_NEEDS_CHECK, "an answer nobody could read approves nothing"
    assert fields["verify"]["verdict"] == "unclear", "the reader is told why it is held"


def test_a_clean_double_check_still_approves_the_page(client, monkeypatch):
    pid, page = _novel_with_page(client)
    page = {**page, "text": "밖에는 아무도 없었다."}
    monkeypatch.setattr(A.ocr, "verify_page", lambda *a, **k: VerifyResult(verdict="ok"))
    fields = A._run_page_task(A.TASK_OCR_VERIFY, pid, page, Config(), None, None)
    assert fields["status"] == P.STATUS_OK


# ---- what a paused job may resume ---------------------------------------------------------

def test_resumable_lists_only_chapter_work_with_its_own_kind_and_force():
    job = A.Job("j", "p")
    job.enqueue([(3, False, A.TASK_OCR), (5, False, A.TASK_TRANSLATE),
                 (4, False, A.TASK_OCR_VERIFY), (7, True, A.TASK_TRANSLATE),
                 (9, False, A.TASK_RESOLVE)])
    assert job.resumable() == [
        {"index": 5, "kind": A.TASK_TRANSLATE, "force": False},
        {"index": 7, "kind": A.TASK_TRANSLATE, "force": True},
        {"index": 9, "kind": A.TASK_RESOLVE, "force": False},
    ], "a page number must never come back as a chapter to translate"
    assert job.queue_state()["resumable"] == job.resumable(), \
        "a waiting event carries it too, since that is what the tab stores"


def test_a_paused_page_read_reports_only_chapters_as_resumable(client, monkeypatch):
    pid, page = _novel_with_page(client)
    job = A.Job("j", pid)
    job.enqueue([(page["seq"], False, A.TASK_OCR), (2, False, A.TASK_TRANSLATE)])
    item = job.take_next()

    def limited(*_a, **_k):
        raise RateLimitedError(None)

    monkeypatch.setattr(A, "_run_page_task", limited)
    seen = []
    monkeypatch.setattr(job, "publish", seen.append)

    async def run():
        return await A._run_page_item(job, Config(), item[0], item[1], item[2], None,
                                      asyncio.get_running_loop(), A._MAX_STRIKES - 1)

    _strikes, outcome = asyncio.run(run())
    assert outcome == "return"
    paused = [e for e in seen if e.get("type") == "paused"]
    assert len(paused) == 1
    assert paused[0]["resumable"] == [{"index": 2, "kind": A.TASK_TRANSLATE, "force": False}]
    assert page["seq"] in paused[0]["pending"], "the count of what's left still includes the page"


# ---- startup ------------------------------------------------------------------------------

def test_startup_releases_pages_left_mid_read_and_remembers_the_loop(client):
    pid, page = _novel_with_page(client)
    with P.mutate_pages(pid) as doc:
        rec = P.find_page(doc, page["id"])
        rec["status"] = P.STATUS_RUNNING
        rec["prior_status"] = P.STATUS_NEW
    A._LOOP = None
    with TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000)):
        assert A._LOOP is not None, "threads need a loop to hand work back to"
        rec = P.find_page(P.load_pages(pid), page["id"])
        assert rec["status"] == P.STATUS_NEW, "nothing is reading it any more"
    assert A._LOOP is None, "a stopped app must not be handed work"


def test_startup_with_no_novels_is_quiet(client):
    with TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/projects").status_code == 200

"""The HTTP side of hands-free chapter boxes.

- Creating a box with an origin it already has answers with that box, ``created`` false.
- ``/captured`` finishes a box, refuses only what can't be finished, and is safe twice.
- The desktop's Build, its preview and Start over all leave a box still being
  photographed alone, and build it once it is finished.
- ``slim=1`` leaves the novel's whole rail out of an answer the phone doesn't show.

Runs under pytest (``pytest tests/``).
"""

import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from translation_bot.config import Config  # noqa: E402

_photos = itertools.count(1)
ORIGIN = "phone:a1b2c3d4"


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    monkeypatch.setattr(P, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    return TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def _novel(client) -> str:
    res = client.post("/api/projects/images", json={"name": "Scanned novel"})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _photo() -> bytes:
    return b"\xff\xd8\xff\xe0" + next(_photos).to_bytes(8, "big") + b"\x00" * 248


def _upload(client, pid, **params):
    res = client.post(f"/api/projects/{pid}/pages", content=_photo(),
                      headers={"Content-Type": "image/jpeg"}, params=params)
    assert res.status_code == 200, res.text
    return res.json()


def _read(client, pid, page_id):
    res = client.post(f"/api/projects/{pid}/pages/{page_id}",
                      json={"text": "밖에는 아무도 없었다.", "status": "ok"})
    assert res.status_code == 200, res.text


def _phone_box(client, pid, origin=ORIGIN, photos=2):
    res = client.post(f"/api/projects/{pid}/pages/batches",
                      json={"origin": origin, "hands_free": True})
    assert res.status_code == 200, res.text
    box = res.json()["batch"]
    for _ in range(photos):
        page = _upload(client, pid, batch=box["id"])["page"]
        _read(client, pid, page["id"])
    return box


def _captured(client, pid, box_id, **body):
    return client.post(f"/api/projects/{pid}/pages/batches/{box_id}/captured", json=body or None)


def _build(client, pid, **body):
    res = client.post(f"/api/projects/{pid}/pages/build", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_an_origin_already_here_answers_with_that_box(client):
    pid = _novel(client)
    first = client.post(f"/api/projects/{pid}/pages/batches",
                        json={"origin": ORIGIN, "hands_free": True}).json()
    again = client.post(f"/api/projects/{pid}/pages/batches",
                        json={"origin": ORIGIN, "hands_free": True}).json()
    assert first["created"] is True and again["created"] is False
    assert again["batch"]["id"] == first["batch"]["id"]
    assert first["batch"]["hands_free"] is True and first["batch"]["state"] == "capturing"
    assert len(client.get(f"/api/projects/{pid}/pages").json()["batches"]) == 1


def test_a_desktop_box_answers_as_it_always_did(client):
    pid = _novel(client)
    res = client.post(f"/api/projects/{pid}/pages/batches", json={"label": "Prologue"}).json()
    assert res["created"] is True
    assert "hands_free" not in res["batch"]


def test_finishing_refuses_only_what_cant_be_finished(client):
    pid = _novel(client)
    res = _captured(client, pid, "deadbeef")
    assert res.status_code == 404 and res.json()["detail"]["title"] == P.BOX_GONE
    empty = client.post(f"/api/projects/{pid}/pages/batches",
                        json={"origin": ORIGIN, "hands_free": True}).json()["batch"]
    assert _captured(client, pid, empty["id"]).status_code == 409


def test_finishing_is_safe_twice_and_keeps_the_number_shown(client):
    pid = _novel(client)
    box = _phone_box(client, pid)
    first = _captured(client, pid, box["id"], expect_number=1).json()["batch"]
    second = _captured(client, pid, box["id"]).json()["batch"]
    assert first["captured_at"] and second["captured_at"] == first["captured_at"]
    assert second["expect_number"] == 1
    assert second["state"] == "ready"


def test_the_build_preview_says_a_box_is_still_being_photographed(client):
    pid = _novel(client)
    box = _phone_box(client, pid)
    res = _build(client, pid, preview=True)
    assert res["added"] == []
    assert res["waiting"][0]["batch"] == box["id"]
    assert res["waiting"][0]["reasons"][0] == P.CAPTURING


@pytest.mark.parametrize("body", [{}, {"include": "all"}, {"append": False}])
def test_no_build_takes_a_box_still_being_photographed(client, body):
    pid = _novel(client)
    _phone_box(client, pid)
    res = client.post(f"/api/projects/{pid}/pages/build", json=body)
    assert res.status_code == 400
    assert P.CAPTURING in res.json()["detail"]["title"], "the refusal says why"
    assert client.get(f"/api/projects/{pid}/pages").json()["chapter_count"] == 0


def test_a_finished_box_builds_from_the_desktop(client):
    pid = _novel(client)
    box = _phone_box(client, pid)
    _captured(client, pid, box["id"])
    assert _build(client, pid)["added"] == [1]


def test_the_novel_list_says_which_chapter_comes_next(client):
    pid = _novel(client)
    nxt = lambda: next(n for n in client.get("/api/scans").json()["novels"] if n["id"] == pid)["next_number"]
    assert nxt() == 1
    _phone_box(client, pid)
    assert nxt() == 2
    box = _phone_box(client, pid, origin="phone:second00")
    _captured(client, pid, box)
    assert nxt() == 3


def test_slim_answers_leave_the_rail_out(client):
    pid = _novel(client)
    made = client.post(f"/api/projects/{pid}/pages/batches", params={"slim": 1},
                       json={"origin": ORIGIN, "hands_free": True}).json()
    assert "pages" not in made and made["rev"] and made["batch"]["id"]
    up = _upload(client, pid, batch=made["batch"]["id"], slim=1)
    assert "pages" not in up and up["page"]["id"] and up["number"] == 1
    done = client.post(f"/api/projects/{pid}/pages/batches/{made['batch']['id']}/captured",
                       params={"slim": 1}).json()
    assert "pages" not in done and done["batch"]["captured_at"]
    gone = client.post(f"/api/projects/{pid}/pages/batches/{made['batch']['id']}/delete",
                       params={"slim": 1}, json={"expect_page_ids": [up["page"]["id"]]}).json()
    assert "pages" not in gone and gone["removed"] == 1
    assert "pages" in _upload(client, pid), "without slim the answer is what it always was"

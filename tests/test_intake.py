"""The shared intake: one photo into a novel, by whichever way it came.

- A photo arriving in a finished phone chapter opens it again, so the chapter waits for
  Done rather than building without the pages that may follow. A repeat of a photo
  already there arrives nowhere and opens nothing.
- Retake in place puts the new photo exactly where the old one was, as a new page with
  its own file, and the old page and file go in the same save. The chapter stays
  finished, the joins on both sides are worked out again, and a read still running for
  the old page has its result dropped when it lands.
- Refusals keep their words: the existing upload tests check them through the API.

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
from server import intake  # noqa: E402
from translation_bot.config import Config  # noqa: E402

_photos = itertools.count(1)


def _photo() -> bytes:
    return b"\xff\xd8\xff\xe0" + next(_photos).to_bytes(8, "big") + b"\x00" * 248


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    monkeypatch.setattr(P, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    return TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))


@pytest.fixture
def pid(client):
    res = client.post("/api/projects/images", json={"name": "Scanned novel"})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _phone_chapter(pid, photos=3, captured=True):
    first = intake.add_photo(pid, _photo(), origin="phone:abc123", hands_free=True)
    box_id = first.page["batch"]
    pages = [first.page] + [intake.add_photo(pid, _photo(), batch=box_id).page
                            for _ in range(photos - 1)]
    if captured:
        with P.mutate_pages(pid) as doc:
            P.set_captured(doc, box_id)
    return box_id, pages


def _box(pid, box_id):
    return P.find_box(P.load_pages(pid), box_id)


def _ids_in(pid, box_id):
    return [p["id"] for p in P.load_pages(pid)["pages"] if p["batch"] == box_id]


# ---- arrivals --------------------------------------------------------------------------------

def test_a_photo_into_a_finished_phone_chapter_opens_it_again(pid):
    box_id, _pages = _phone_chapter(pid)
    arrival = intake.add_photo(pid, _photo(), batch=box_id)
    assert arrival.reopened is True
    assert "captured_at" not in _box(pid, box_id)


def test_a_photo_into_a_chapter_still_open_or_from_the_desktop_reopens_nothing(pid):
    box_id, _pages = _phone_chapter(pid, captured=False)
    assert intake.add_photo(pid, _photo(), batch=box_id).reopened is False
    desktop = intake.add_photo(pid, _photo())
    assert desktop.reopened is False
    assert intake.add_photo(pid, _photo(), batch=desktop.page["batch"]).reopened is False


def test_a_repeated_photo_arrives_nowhere(pid):
    box_id, _pages = _phone_chapter(pid)
    data = _photo()
    intake.add_photo(pid, data)
    again = intake.add_photo(pid, data, batch=box_id)
    assert again.duplicate is True and again.reopened is False
    assert _box(pid, box_id).get("captured_at"), "nothing arrived, so nothing reopened"


def test_the_page_limit_is_read_when_called(pid, monkeypatch):
    intake.add_photo(pid, _photo())
    monkeypatch.setattr(P, "MAX_PAGES_PER_PROJECT", 1)
    with pytest.raises(intake.IntakeError) as refusal:
        intake.add_photo(pid, _photo())
    assert refusal.value.status == 400
    assert refusal.value.message == "This novel already has 1 pages."


def test_a_new_box_gets_its_origin(pid):
    first = intake.add_photo(pid, _photo(), origin="tg:9a1b2c3d", hands_free=True)
    again = intake.add_photo(pid, _photo(), origin="tg:9a1b2c3d", hands_free=True)
    assert again.page["batch"] == first.page["batch"], "the origin finds the box it made"
    box = _box(pid, first.page["batch"])
    assert box["hands_free"] is True and box["origin"] == "tg:9a1b2c3d"


# ---- retake in place ----------------------------------------------------------------------------

def test_a_retake_takes_the_old_photos_place(pid):
    box_id, pages = _phone_chapter(pid)
    old = pages[1]
    data = _photo()
    arrival = intake.replace_page(pid, old["id"], data)
    new = arrival.page
    assert arrival.replaced == old["id"]
    assert new["id"] != old["id"] and new["file"] != old["file"], \
        "a page's image is cached forever, so a new photo needs a new page"
    assert _ids_in(pid, box_id) == [pages[0]["id"], new["id"], pages[2]["id"]]
    folder = P.pages_dir(pid)
    assert (folder / new["file"]).read_bytes() == data
    assert not (folder / old["file"]).exists(), "the old file goes with its page"
    assert P.find_page(P.load_pages(pid), new["id"])["status"] == P.STATUS_NEW
    assert _box(pid, box_id).get("captured_at"), "nothing was added, so it stays finished"


def test_a_retake_works_out_the_joins_on_both_sides_again(pid):
    box_id, pages = _phone_chapter(pid)
    with P.mutate_pages(pid) as doc:
        for page in doc["pages"][1:]:
            P.set_seam(page, kind="sentence", glue="none", source="auto", reason="x",
                       prev=P.seam_neighbours(doc)[page["id"]])
    new = intake.replace_page(pid, pages[1]["id"], _photo()).page
    doc = P.load_pages(pid)
    assert P.find_page(doc, new["id"])["join_prev"] == ""
    assert P.find_page(doc, pages[2]["id"])["join_prev"] == "", \
        "the next photo's join was decided against a photo that is gone"


def test_a_read_still_running_for_the_old_photo_is_dropped(pid):
    _box_id, pages = _phone_chapter(pid)
    old = pages[0]
    new = intake.replace_page(pid, old["id"], _photo()).page
    assert A._apply_page_result(pid, old["id"], {"text": "옛 사진", "status": "ok"}) == {}
    assert P.find_page(P.load_pages(pid), new["id"])["text"] == ""


def test_the_same_photo_again_changes_nothing(pid):
    data = _photo()
    page = intake.add_photo(pid, data).page
    again = intake.replace_page(pid, page["id"], data)
    assert again.replaced is None and again.page["id"] == page["id"]
    assert (P.pages_dir(pid) / page["file"]).exists()


def test_a_retake_that_is_another_page_is_refused(pid):
    box_id, pages = _phone_chapter(pid)
    data = _photo()
    intake.add_photo(pid, data, batch=box_id)
    with pytest.raises(intake.IntakeError) as refusal:
        intake.replace_page(pid, pages[0]["id"], data)
    assert refusal.value.status == 409
    assert refusal.value.message == "That photo is already in this novel, as photo 4 of chapter 1."
    assert _ids_in(pid, box_id)[0] == pages[0]["id"]


def test_a_retake_of_a_page_that_is_gone_is_refused(pid):
    intake.add_photo(pid, _photo())
    with pytest.raises(intake.IntakeError) as refusal:
        intake.replace_page(pid, "deadbeef", _photo())
    assert refusal.value.status == 404


# ---- over HTTP ----------------------------------------------------------------------------------

def test_the_replace_endpoint_answers_with_the_new_page(client, pid):
    box_id, pages = _phone_chapter(pid)
    res = client.post(f"/api/projects/{pid}/pages/{pages[2]['id']}/replace",
                      content=_photo(), headers={"Content-Type": "image/jpeg"},
                      params={"slim": 1})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["replaced"] == pages[2]["id"] and body["batch"] == box_id
    assert body["number"] == 1 and "pages" not in body
    assert client.get(f"/api/projects/{pid}/pages/{body['page']['id']}/image").status_code == 200
    assert client.get(f"/api/projects/{pid}/pages/{pages[2]['id']}/image").status_code == 404


def test_the_replace_endpoint_refuses_what_isnt_a_photo(client, pid):
    _box_id, pages = _phone_chapter(pid)
    res = client.post(f"/api/projects/{pid}/pages/{pages[0]['id']}/replace",
                      content=b"%PDF-1.7\n" + b"\x00" * 32,
                      headers={"Content-Type": "application/pdf"})
    assert res.status_code == 400


def test_an_upload_says_when_it_reopened_a_chapter(client, pid):
    box_id, _pages = _phone_chapter(pid)
    res = client.post(f"/api/projects/{pid}/pages", content=_photo(),
                      headers={"Content-Type": "image/jpeg"}, params={"batch": box_id})
    assert res.status_code == 200, res.text
    assert res.json()["reopened"] is True


# ---- several at once: a PDF's pages ------------------------------------------------------------

def test_several_photos_go_in_in_order_each_only_once(pid):
    a, b = _photo(), _photo()
    bundle = intake.add_photos(pid, [("p1", a), ("p2", a), ("p3", b)])
    assert [p["name"] for p in bundle.added] == ["p1", "p3"]
    assert [d["name"] for d in bundle.duplicates] == ["p2"]
    assert _ids_in(pid, bundle.batch) == [p["id"] for p in bundle.added]


def test_captured_finishes_a_new_chapter_but_not_one_still_open(pid):
    box_id, _pages = _phone_chapter(pid, captured=False)
    intake.add_photos(pid, [("p", _photo())], batch=box_id, captured=True)
    assert "captured_at" not in _box(pid, box_id), "the chapter's own sender finishes it"
    fresh = intake.add_photos(pid, [("q", _photo())], origin="tg:pdf-1", hands_free=True,
                              captured=True)
    assert _box(pid, fresh.batch).get("captured_at")

"""The PDF endpoint: a scanner PDF's pages go in, in order, in one save.

- With no chapter given and hands_free, the PDF is one whole chapter, finished at once.
- Into a chapter, the pages go at its end; a finished phone chapter opens again.
- The same PDF twice adds nothing the second time.
- Refusals say why, and nothing is left behind in the novel's folder.

Runs under pytest (``pytest tests/``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

pytest.importorskip("pypdfium2")

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from server import pdf_pages  # noqa: E402
from tests.test_pdf_pages import jpeg, make_pdf  # noqa: E402
from translation_bot.config import Config  # noqa: E402


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
    return client.post("/api/projects/images", json={"name": "Scanned"}).json()["id"]


def _send(client, pid, data, **params):
    return client.post(f"/api/projects/{pid}/pages/pdf", content=data,
                       headers={"Content-Type": "application/pdf"}, params=params)


def _box_pages(pid, box_id):
    return [p for p in P.load_pages(pid)["pages"] if p["batch"] == box_id]


def test_a_pdf_sent_hands_free_is_one_finished_chapter(client, pid):
    a, b = jpeg("first"), jpeg("second")
    res = _send(client, pid, make_pdf([a, b, None]), hands_free=1, origin="tg:pdf00001",
                name="Lens scan.pdf", slim=1)
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["added_ids"]) == 3 and body["duplicates"] == 0 and body["number"] == 1
    assert "pages" not in body
    pages = _box_pages(pid, body["batch"])
    assert [p["id"] for p in pages] == body["added_ids"], "in the PDF's order"
    assert [p["name"] for p in pages] == ["Lens scan-001.jpg", "Lens scan-002.jpg", "Lens scan-003.png"]
    folder = P.pages_dir(pid)
    assert (folder / pages[0]["file"]).read_bytes() == a
    box = P.find_box(P.load_pages(pid), body["batch"])
    assert box["hands_free"] is True and box["captured_at"] and box["origin"] == "tg:pdf00001"


def test_the_same_pdf_twice_adds_nothing(client, pid):
    data = make_pdf([jpeg("one"), jpeg("two")])
    _send(client, pid, data, hands_free=1)
    again = _send(client, pid, data, hands_free=1).json()
    assert again["added_ids"] == [] and again["duplicates"] == 2 and again["batch"] is None
    assert len(P.load_pages(pid)["batches"]) == 1


def test_pages_into_a_chapter_go_at_its_end(client, pid):
    first = client.post(f"/api/projects/{pid}/pages", content=jpeg("photo"),
                        headers={"Content-Type": "image/jpeg"}).json()
    box = first["batch"]
    res = _send(client, pid, make_pdf([jpeg("p1"), jpeg("p2")]), batch=box).json()
    assert [p["id"] for p in _box_pages(pid, box)] == [first["page"]["id"], *res["added_ids"]]
    assert "hands_free" not in P.find_box(P.load_pages(pid), box)


def test_into_a_finished_phone_chapter_it_opens_again(client, pid):
    res = _send(client, pid, make_pdf([jpeg("a")]), hands_free=1).json()
    more = _send(client, pid, make_pdf([jpeg("b")]), batch=res["batch"]).json()
    assert more["reopened"] is True
    assert "captured_at" not in P.find_box(P.load_pages(pid), res["batch"])


def test_what_isnt_a_pdf_is_refused(client, pid):
    res = _send(client, pid, b"\xff\xd8\xff\xe0 a photo")
    assert res.status_code == 400
    assert res.json()["detail"]["title"] == "That isn't a PDF."


def test_a_pdf_too_big_is_refused_before_it_is_all_in(client, pid, monkeypatch):
    monkeypatch.setattr(A, "MAX_PDF_BYTES", 1000)
    res = _send(client, pid, b"%PDF-1.7\n" + b"x" * 5000)
    assert res.status_code == 413


def test_a_missing_library_says_to_run_setup(client, pid, monkeypatch):
    def missing(*_a, **_k):
        raise pdf_pages.PdfError("missing", "Reading PDFs needs one more part. Run setup.bat "
                                            "on the computer, then try again.", status=503)

    monkeypatch.setattr(pdf_pages, "split_in_child", missing)
    res = _send(client, pid, make_pdf([None]))
    assert res.status_code == 503 and "setup.bat" in res.json()["detail"]["title"]


def test_nothing_is_left_in_the_incoming_folder(client, pid):
    _send(client, pid, make_pdf([jpeg("x")]), hands_free=1)
    _send(client, pid, b"%PDF-1.7 broken")
    incoming = pj.PROJECTS_DIR / pid / ".incoming"
    assert list(incoming.iterdir()) == []


def test_the_photo_endpoint_still_refuses_a_pdf(client, pid):
    res = client.post(f"/api/projects/{pid}/pages", content=make_pdf([None]),
                      headers={"Content-Type": "application/pdf"})
    assert res.status_code == 400

"""End-to-end tests for the scanned-page HTTP flow.

Walks the real path a photographed novel takes: create → upload → correct → stitch →
build → and out the other side as an ordinary novel the translator can read.

No model is ever called. The OCR calls themselves are covered by the parser tests;
what matters here is the plumbing around them, and running real translations from a
test would spend the user's plan quota.

What has to hold:
- A page's type is decided by its bytes, and re-uploading the same photo is a no-op.
- Action routes (/pages/build, /pages/reorder, ...) are never captured as a page id.
- Editing a page's join is marked as the reader's decision, so a later automatic pass
  cannot overwrite it.
- After building, ``get_chapters`` reads the built source with no network — that one
  branch is the entire integration with the translation pipeline.
- A build appends by default, so photographing a new chapter never renumbers the
  chapters already translated.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_pages_api.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from translation_bot.config import Config  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 256
JPEG2 = b"\xff\xd8\xff\xe0" + b"\x01" * 256
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 256


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    monkeypatch.setattr(P, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    # The app only trusts localhost/127.0.0.1 (the API is unauthenticated and acts on
    # local files), so the test client has to present an allowed Host.
    return TestClient(A.app, base_url="http://localhost",
                      client=("127.0.0.1", 50000))


def _novel(client) -> str:
    res = client.post("/api/projects/images", json={"name": "Scanned novel"})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _upload(client, pid, data=JPEG, **params):
    return client.post(f"/api/projects/{pid}/pages", content=data,
                       headers={"Content-Type": "image/jpeg"}, params=params)


def _set(client, pid, page_id, **fields):
    res = client.post(f"/api/projects/{pid}/pages/{page_id}", json=fields)
    assert res.status_code == 200, res.text
    return res.json()


# ---- creating ----------------------------------------------------------------

def test_an_image_novel_starts_empty(client):
    pid = _novel(client)
    project = client.get(f"/api/projects/{pid}").json()
    assert project["source_type"] == "images"
    assert client.get(f"/api/projects/{pid}/pages").json()["counts"]["total"] == 0


# ---- uploading ---------------------------------------------------------------

def test_uploading_a_photo_stores_it_and_serves_it_back(client):
    pid = _novel(client)
    page = _upload(client, pid, name="IMG_0042.jpg").json()["page"]

    image = client.get(f"/api/projects/{pid}/pages/{page['id']}/image")
    assert image.status_code == 200
    assert image.content == JPEG, "the original bytes are served unchanged"
    assert "immutable" in image.headers.get("cache-control", "")


def test_the_same_photo_twice_is_a_no_op(client):
    """Re-dropping a folder must not double the novel."""
    pid = _novel(client)
    _upload(client, pid)
    second = _upload(client, pid).json()
    assert second["duplicate"] is True
    assert second["total"] == 1


def test_a_different_photo_is_a_new_page(client):
    pid = _novel(client)
    _upload(client, pid, data=JPEG)
    assert _upload(client, pid, data=JPEG2).json()["total"] == 2


def test_an_iphone_photo_is_refused_with_an_actionable_message(client):
    pid = _novel(client)
    res = client.post(f"/api/projects/{pid}/pages", content=HEIC,
                      headers={"Content-Type": "image/jpeg"})
    assert res.status_code == 400
    assert "Most Compatible" in res.text, "the fix must be told to the user"


def test_a_lying_content_type_cannot_smuggle_a_file_in(client):
    """The bytes decide, not the header."""
    pid = _novel(client)
    res = client.post(f"/api/projects/{pid}/pages", content=b"%PDF-1.7 not an image",
                      headers={"Content-Type": "image/png"})
    assert res.status_code == 400


def test_an_empty_upload_is_refused(client):
    pid = _novel(client)
    assert client.post(f"/api/projects/{pid}/pages", content=b"").status_code == 400


def test_uploading_to_a_non_image_novel_is_refused(client):
    text = client.post("/api/projects/text",
                       json={"name": "Pasted", "text": "본문", "split_mode": "single"})
    pid = text.json()["id"]
    assert _upload(client, pid).status_code == 400


# ---- listing -----------------------------------------------------------------

def test_the_page_list_omits_text(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="그는 문을 열었다.")

    row = client.get(f"/api/projects/{pid}/pages").json()["pages"][0]
    assert "text" not in row, "page text would make the rail payload megabytes"
    assert row["chars"] == len("그는 문을 열었다.")

    full = client.get(f"/api/projects/{pid}/pages/{page['id']}").json()
    assert full["text"] == "그는 문을 열었다.", "the single-page endpoint does carry it"


# ---- correcting --------------------------------------------------------------

def test_editing_text_marks_the_page_as_edited(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    updated = _set(client, pid, page["id"], text="사람이 고쳤다.")
    assert updated["status"] == P.STATUS_EDITED
    assert updated["hangul_fraction"] > 0.5, "the Korean fraction is recomputed"


def test_a_reader_set_join_is_marked_as_theirs(client):
    """So a later automatic stitching pass cannot overwrite their decision."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    updated = _set(client, pid, page["id"], join_prev="sentence")
    assert updated["join_prev"] == "sentence"
    assert updated["join_prev_source"] == "user"


def test_an_unknown_status_or_join_is_refused(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    assert client.post(f"/api/projects/{pid}/pages/{page['id']}",
                       json={"status": "banana"}).status_code == 400
    assert client.post(f"/api/projects/{pid}/pages/{page['id']}",
                       json={"join_prev": "banana"}).status_code == 400


# ---- ordering ----------------------------------------------------------------

def test_pages_can_be_reordered(client):
    pid = _novel(client)
    a = _upload(client, pid, data=JPEG).json()["page"]
    b = _upload(client, pid, data=JPEG2).json()["page"]

    assert client.post(f"/api/projects/{pid}/pages/reorder",
                       json={"ids": [b["id"], a["id"]]}).status_code == 200
    rail = client.get(f"/api/projects/{pid}/pages").json()["pages"]
    assert [p["id"] for p in rail] == [b["id"], a["id"]]


def test_a_partial_reorder_is_refused(client):
    pid = _novel(client)
    a = _upload(client, pid, data=JPEG).json()["page"]
    _upload(client, pid, data=JPEG2)
    assert client.post(f"/api/projects/{pid}/pages/reorder",
                       json={"ids": [a["id"]]}).status_code == 400


def test_action_routes_are_not_captured_as_page_ids(client):
    """/pages/build must reach the build endpoint, not look up a page called 'build'."""
    pid = _novel(client)
    for action in ("reorder", "delete"):
        res = client.post(f"/api/projects/{pid}/pages/{action}", json={"ids": []})
        assert res.status_code != 404, f"/pages/{action} was captured as a page id"


# ---- stitching ---------------------------------------------------------------

def test_stitching_decides_seams_without_a_model(client):
    pid = _novel(client)
    first = _upload(client, pid, data=JPEG).json()["page"]
    second = _upload(client, pid, data=JPEG2).json()["page"]
    _set(client, pid, first["id"], text="그는 천천히 문을")
    _set(client, pid, second["id"], text="열고 안으로 들어갔다.")

    res = client.post(f"/api/projects/{pid}/pages/stitch", json={"use_model": False})
    assert res.status_code == 200, res.text
    assert res.json()["asked_model"] == 0, "the free rules must settle this seam"

    rail = client.get(f"/api/projects/{pid}/pages").json()["pages"]
    assert rail[1]["join_prev"] == "sentence", \
        "an unfinished sentence across a page break must not become a paragraph"


def test_stitching_leaves_a_reader_decision_alone(client):
    pid = _novel(client)
    first = _upload(client, pid, data=JPEG).json()["page"]
    second = _upload(client, pid, data=JPEG2).json()["page"]
    _set(client, pid, first["id"], text="그는 천천히 문을")
    _set(client, pid, second["id"], text="열고 안으로 들어갔다.")
    _set(client, pid, second["id"], join_prev="chapter")

    client.post(f"/api/projects/{pid}/pages/stitch", json={"use_model": False})
    rail = client.get(f"/api/projects/{pid}/pages").json()["pages"]
    assert rail[1]["join_prev"] == "chapter", "the reader's own decision must stand"


# ---- building ----------------------------------------------------------------

def _two_page_chapter(client, pid):
    """Two photos dropped together — one batch, so they build as ONE chapter.

    The client threads the batch id returned by the first upload through the rest of
    the drop; omitting it deliberately starts a new batch (a separate chapter).
    """
    first_res = _upload(client, pid, data=JPEG).json()
    batch = first_res["batch"]
    first = first_res["page"]
    second = _upload(client, pid, data=JPEG2, batch=batch).json()["page"]
    _set(client, pid, first["id"], text="그는 천천히 문을", status=P.STATUS_OK)
    _set(client, pid, second["id"], text="열고 안으로 들어갔다.",
         join_prev="sentence", status=P.STATUS_OK)
    return first, second


def test_one_drop_is_one_chapter_but_separate_drops_are_not(client):
    """A batch is how the reader says 'these photos are one chapter'."""
    pid = _novel(client)
    first = _upload(client, pid, data=JPEG).json()
    _upload(client, pid, data=JPEG2, batch=first["batch"])
    _upload(client, pid, data=PNG)  # a separate drop

    for page in client.get(f"/api/projects/{pid}/pages").json()["pages"]:
        _set(client, pid, page["id"], text="본문입니다.", status=P.STATUS_OK)

    built = client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})
    assert built.json()["chapters"] == 2, "two drops make two chapters, not three"


def test_building_produces_chapters_the_pipeline_can_read(client):
    pid = _novel(client)
    _two_page_chapter(client, pid)

    res = client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})
    assert res.status_code == 200, res.text
    assert res.json()["chapters"] == 1

    # The whole point: from here it is an ordinary novel, read with no network.
    A._chapter_cache.clear()
    chapters = A.get_chapters(pid, pj.project_config(Config(), pj.get_project(pid)))
    assert len(chapters) == 1
    assert chapters[0].paragraphs == ["그는 천천히 문을 열고 안으로 들어갔다."], \
        "a sentence spanning a page break must arrive as ONE paragraph"


def test_building_reports_the_chapter_count_on_the_novel(client):
    pid = _novel(client)
    _two_page_chapter(client, pid)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})
    assert client.get(f"/api/projects/{pid}").json()["chapter_count"] == 1


def test_building_with_nothing_approved_is_refused_helpfully(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="확인 안 됨", status=P.STATUS_NEEDS_CHECK)

    res = client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})
    assert res.status_code == 400
    assert "Read the pages first" in res.text


def test_a_second_build_appends_without_renumbering(client):
    """Photograph chapter 2, build, and chapter 1 keeps its index and translation."""
    pid = _novel(client)
    _two_page_chapter(client, pid)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})

    later = _upload(client, pid, data=PNG).json()["page"]
    _set(client, pid, later["id"], text="다음 장의 본문.", status=P.STATUS_OK)
    res = client.post(f"/api/projects/{pid}/pages/build",
                      json={"mode": "batch", "append": True})
    assert res.status_code == 200, res.text
    assert res.json()["chapters"] == 2 and res.json()["added"] == 1

    A._chapter_cache.clear()
    chapters = A.get_chapters(pid, pj.project_config(Config(), pj.get_project(pid)))
    assert chapters[0].paragraphs == ["그는 천천히 문을 열고 안으로 들어갔다."], \
        "the first chapter must be untouched by a later build"
    assert chapters[1].paragraphs == ["다음 장의 본문."]


def test_an_append_keeps_the_earlier_chapters_photo_attribution(client):
    """page_map is the ONLY record of which photos a chapter was built from.

    An append only builds the new chapters, so build_chapters returns a map keyed by
    those indices alone. Storing it wholesale dropped every earlier entry, and the
    reader's Photo/Text toggle and its source images silently vanished from every
    chapter built before the append — permanently, with no error, and recoverable only
    by a full rebuild that renumbers and re-bills the whole novel.
    """
    pid = _novel(client)
    _two_page_chapter(client, pid)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})
    before = (P.load_pages(pid)["build"] or {})["page_map"]
    assert "1" in before, "the first build attributes chapter 1 to its photos"

    later = _upload(client, pid, data=PNG).json()["page"]
    _set(client, pid, later["id"], text="다음 장의 본문.", status=P.STATUS_OK)
    client.post(f"/api/projects/{pid}/pages/build",
                json={"mode": "batch", "append": True})

    after = (P.load_pages(pid)["build"] or {})["page_map"]
    assert after.get("1") == before["1"], "chapter 1 must keep its photos"
    assert "2" in after, "and the appended chapter must gain its own"

    project = pj.get_project(pid)
    assert A._chapter_page_ids(project, 1), "the reader must still find chapter 1's photos"
    assert A._chapter_page_ids(project, 2)


# ---- a correction to an already-built page must reach source.json ------------
# The append filter exists so a second build does not duplicate chapters, but it also
# dropped edits to pages already in a chapter: fix an OCR mistake, press Build, and the
# answer was "No pages are ready to build" while the chapter kept the wrong Korean
# forever — correctable only by a full rebuild, which renumbers and re-bills the novel.

def test_correcting_a_built_page_reaches_the_chapter(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="원래 본문입니다.", status=P.STATUS_OK)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})

    _set(client, pid, page["id"], text="고쳐진 본문입니다.", status=P.STATUS_OK)
    res = client.post(f"/api/projects/{pid}/pages/build",
                      json={"mode": "batch", "append": True})

    assert res.status_code == 200, res.text
    assert res.json()["refreshed"] == [1]
    A._chapter_cache.clear()
    chapters = A.get_chapters(pid, pj.project_config(Config(), pj.get_project(pid)))
    assert chapters[0].paragraphs == ["고쳐진 본문입니다."]


def test_a_correction_says_the_translation_is_now_stale(client):
    """Silently updating the Korean under a finished translation would be its own
    trap, so the build says what it did and what it means."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="원래 본문입니다.", status=P.STATUS_OK)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})
    _set(client, pid, page["id"], text="고쳐진 본문입니다.", status=P.STATUS_OK)

    warnings = client.post(f"/api/projects/{pid}/pages/build",
                           json={"mode": "batch", "append": True}).json()["warnings"]
    assert any("re-translate" in w.lower() for w in warnings)


def test_building_with_nothing_changed_is_still_refused(client):
    """The refresh must not turn "nothing to do" into a silent success."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="원래 본문입니다.", status=P.STATUS_OK)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})

    res = client.post(f"/api/projects/{pid}/pages/build",
                      json={"mode": "batch", "append": True})
    assert res.status_code == 400
    assert "No pages are ready" in res.text


def test_a_correction_does_not_disturb_the_other_chapters(client):
    pid = _novel(client)
    first = _upload(client, pid, data=JPEG).json()
    _set(client, pid, first["page"]["id"], text="첫 장.", status=P.STATUS_OK)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})
    second = _upload(client, pid, data=JPEG2).json()
    _set(client, pid, second["page"]["id"], text="둘째 장.", status=P.STATUS_OK)
    client.post(f"/api/projects/{pid}/pages/build",
                json={"mode": "batch", "append": True})

    _set(client, pid, first["page"]["id"], text="첫 장, 고침.", status=P.STATUS_OK)
    res = client.post(f"/api/projects/{pid}/pages/build",
                      json={"mode": "batch", "append": True})

    assert res.json()["refreshed"] == [1], "only the corrected chapter is rebuilt"
    A._chapter_cache.clear()
    chapters = A.get_chapters(pid, pj.project_config(Config(), pj.get_project(pid)))
    assert [c.paragraphs for c in chapters] == [["첫 장, 고침."], ["둘째 장."]]


def test_a_whole_novel_split_is_not_guessed_at(client):
    """A "single" build records page_map {"*": …} because it genuinely cannot say
    which chapter a page landed in. Refreshing from that would be a guess."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="원래 본문입니다.", status=P.STATUS_OK)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "single"})
    _set(client, pid, page["id"], text="고쳐진 본문입니다.", status=P.STATUS_OK)

    res = client.post(f"/api/projects/{pid}/pages/build",
                      json={"mode": "single", "append": True})
    assert res.status_code == 400, "nothing attributable to refresh, nothing new to add"


def test_a_full_rebuild_replaces_the_attribution_rather_than_merging_it(client):
    """The merge is append-only. A rebuild renumbers from 1, so carrying the old map
    forward would attribute a chapter to photos that are no longer under it."""
    pid = _novel(client)
    _two_page_chapter(client, pid)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})

    later = _upload(client, pid, data=PNG).json()["page"]
    _set(client, pid, later["id"], text="다음 장의 본문.", status=P.STATUS_OK)
    res = client.post(f"/api/projects/{pid}/pages/build",
                      json={"mode": "batch", "append": False, "force": True})
    assert res.status_code == 200, res.text

    build = P.load_pages(pid)["build"] or {}
    assert set(build["page_map"]) == {str(i) for i in range(1, res.json()["chapters"] + 1)}, \
        "a rebuild's map covers exactly the chapters it just built"


def test_a_gap_is_reported_in_the_build_warnings(client):
    pid = _novel(client)
    first_res = _upload(client, pid, data=JPEG).json()
    first = first_res["page"]
    second = _upload(client, pid, data=JPEG2, batch=first_res["batch"]).json()["page"]
    _set(client, pid, first["id"], text="그는 문을 열었다.", status=P.STATUS_OK)
    _set(client, pid, second["id"], text="배는 이미 떠난 뒤였다.",
         join_prev="gap", status=P.STATUS_OK)

    warnings = client.post(f"/api/projects/{pid}/pages/build",
                           json={"mode": "batch"}).json()["warnings"]
    assert warnings and "missing" in warnings[0], \
        "a page that was never photographed must be surfaced"


# ---- deleting ----------------------------------------------------------------

def test_deleting_a_page_removes_its_file(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    assert client.post(f"/api/projects/{pid}/pages/delete",
                       json={"ids": [page["id"]]}).json()["removed"] == 1
    assert client.get(f"/api/projects/{pid}/pages/{page['id']}/image").status_code == 404
    assert client.get(f"/api/projects/{pid}/pages").json()["counts"]["total"] == 0


def test_a_missing_page_is_a_clean_404(client):
    pid = _novel(client)
    assert client.get(f"/api/projects/{pid}/pages/deadbeef").status_code == 404
    assert client.get(f"/api/projects/{pid}/pages/deadbeef/image").status_code == 404


# ---- pages must never be stranded in "Queued" --------------------------------
# A page is marked `queued` when work is accepted for it. Nothing used to put it back
# when that work was dropped (Stop, a rate-limit give-up, a dead worker), and the
# default sweep only selects new/failed - so the page read "Queued" forever and
# "Read N pages" answered "There are no pages to do that to".

def test_releasing_puts_stranded_pages_back(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], status=P.STATUS_QUEUED)

    assert A._release_queued_pages(pid) == 1
    rail = client.get(f"/api/projects/{pid}/pages").json()["pages"]
    assert rail[0]["status"] == P.STATUS_NEW, "a released page must be readable again"


def test_releasing_only_touches_queued_pages(client):
    pid = _novel(client)
    a = _upload(client, pid, data=JPEG).json()["page"]
    b = _upload(client, pid, data=JPEG2).json()["page"]
    _set(client, pid, a["id"], status=P.STATUS_QUEUED)
    _set(client, pid, b["id"], text="본문", status=P.STATUS_OK)

    A._release_queued_pages(pid)
    by_id = {p["id"]: p for p in client.get(f"/api/projects/{pid}/pages").json()["pages"]}
    assert by_id[a["id"]]["status"] == P.STATUS_NEW
    assert by_id[b["id"]]["status"] == P.STATUS_OK, "an accepted page must be left alone"


def test_releasing_can_be_limited_to_specific_pages(client):
    pid = _novel(client)
    a = _upload(client, pid, data=JPEG).json()["page"]
    b = _upload(client, pid, data=JPEG2).json()["page"]
    for p in (a, b):
        _set(client, pid, p["id"], status=P.STATUS_QUEUED)

    assert A._release_queued_pages(pid, {a["seq"]}) == 1
    by_id = {p["id"]: p for p in client.get(f"/api/projects/{pid}/pages").json()["pages"]}
    assert by_id[a["id"]]["status"] == P.STATUS_NEW
    assert by_id[b["id"]]["status"] == P.STATUS_QUEUED


def test_releasing_never_creates_a_manifest_for_a_novel_with_no_pages(client):
    """Pressing Stop on a Google-Doc novel must not leave a pages.json behind."""
    text = client.post("/api/projects/text",
                       json={"name": "Pasted", "text": "본문", "split_mode": "single"})
    pid = text.json()["id"]
    assert A._release_queued_pages(pid) == 0
    assert not P.pages_file(pid).exists()


def test_only_pages_the_queue_accepted_are_marked_queued(client, monkeypatch):
    """Marking before enqueuing stranded every page the dedup rejected."""
    pid = _novel(client)
    a = _upload(client, pid, data=JPEG).json()["page"]
    b = _upload(client, pid, data=JPEG2).json()["page"]

    # Pretend the queue already held page b, so only a is accepted.
    monkeypatch.setattr(A, "_enqueue_task",
                        lambda _pid, _cfg, items: {"job_id": "j", "queued": [a["seq"]]})
    res = client.post(f"/api/projects/{pid}/pages/ocr", json={"ids": [a["id"], b["id"]]})
    assert res.status_code == 200, res.text
    assert res.json()["skipped_busy"] == 1

    by_id = {p["id"]: p for p in client.get(f"/api/projects/{pid}/pages").json()["pages"]}
    assert by_id[a["id"]]["status"] == P.STATUS_QUEUED
    assert by_id[b["id"]]["status"] == P.STATUS_NEW, \
        "a page the queue refused must stay readable, not sit at Queued forever"


def test_queueing_work_that_is_all_already_busy_says_so(client, monkeypatch):
    """Silently answering 200 with an empty queue looked like a broken button."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    monkeypatch.setattr(A, "_enqueue_task",
                        lambda _pid, _cfg, _items: {"job_id": "j", "queued": []})
    res = client.post(f"/api/projects/{pid}/pages/ocr", json={"ids": [page["id"]]})
    assert res.status_code == 409
    assert "already queued" in res.text


# ---- what a page falls back to -----------------------------------------------
# Marking a page `queued` OVERWRITES the only record of its real status. The abort
# path then read that back as "what it was before" and restored `queued` — so the
# page the user had just stopped was stuck in the one status the default sweep does
# not select, and hand-picking it was the only way out.

def _queue_without_running(client, monkeypatch, pid, page):
    """Accept OCR work for a page but never spawn a worker.

    The endpoint normally starts the job inline and a stubbed translator runs it to
    completion, so the page is finished before the test can look at it. These cases
    are about the state the page sits in WHILE queued.
    """
    monkeypatch.setattr(A, "_enqueue_task",
                        lambda _pid, _cfg, items: {"job_id": "j",
                                                   "queued": [i for i, _f, _k in items]})
    res = client.post(f"/api/projects/{pid}/pages/ocr", json={"ids": [page["id"]]})
    assert res.status_code == 200, res.text


def test_a_page_in_flight_falls_back_to_what_it_was_before_being_queued(
        client, monkeypatch):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _queue_without_running(client, monkeypatch, pid, page)

    rec = next(p for p in P.load_pages(pid)["pages"] if p["id"] == page["id"])
    assert rec["status"] == P.STATUS_QUEUED
    assert A._resting_status(rec) == P.STATUS_NEW, \
        "restoring the CURRENT status would put it back to queued, forever"


def test_stopping_a_re_read_does_not_discard_an_approval(client, monkeypatch):
    """Re-reading a page that was already accepted and then pressing Stop must leave
    it accepted. A blanket reset to "not read yet" would throw away the approval and
    the page would come back round on the next "Read all unread"."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="본문", status=P.STATUS_OK)

    _queue_without_running(client, monkeypatch, pid, page)
    assert A._release_queued_pages(pid) == 1

    rail = client.get(f"/api/projects/{pid}/pages").json()["pages"]
    assert rail[0]["status"] == P.STATUS_OK


def test_a_stale_fallback_is_ignored_once_the_page_is_at_rest(client):
    """prior_status is only meaningful while the page is in flight. A page that has
    since been read is `ok`, and an old stash saying `new` must not win."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    with P.mutate_pages(pid) as doc:
        rec = P.find_page(doc, page["id"])
        rec["prior_status"] = P.STATUS_NEW
        rec["status"] = P.STATUS_OK

    rec = next(p for p in P.load_pages(pid)["pages"] if p["id"] == page["id"])
    assert A._resting_status(rec) == P.STATUS_OK


def test_a_page_left_running_is_released_too(client):
    """The page that was mid-flight when a worker died is the ONE item not in the
    pending queue, so releasing only the queue left exactly it stranded."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], status=P.STATUS_RUNNING)

    assert A._release_queued_pages(pid) == 1
    rail = client.get(f"/api/projects/{pid}/pages").json()["pages"]
    assert rail[0]["status"] == P.STATUS_NEW


def test_releasing_clears_the_fallback_it_used(client, monkeypatch):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _queue_without_running(client, monkeypatch, pid, page)
    assert A._release_queued_pages(pid) == 1

    rec = next(p for p in P.load_pages(pid)["pages"] if p["id"] == page["id"])
    assert "prior_status" not in rec, "a consumed fallback must not linger and go stale"


# ---- the cross-novel index ---------------------------------------------------

def test_the_scans_index_lists_only_photographed_novels(client):
    client.post("/api/projects/text",
                json={"name": "Pasted", "text": "본문", "split_mode": "single"})
    pid = _novel(client)

    novels = client.get("/api/scans").json()["novels"]
    assert [n["id"] for n in novels] == [pid], "a pasted-text novel is not a scan"


def test_the_scans_index_reports_what_wants_attention(client):
    pid = _novel(client)
    first = _upload(client, pid, data=JPEG).json()["page"]
    _upload(client, pid, data=JPEG2)
    _set(client, pid, first["id"], text="확인 필요", status=P.STATUS_NEEDS_CHECK)

    row = client.get("/api/scans").json()["novels"][0]
    assert row["counts"]["total"] == 2
    assert row["counts"]["needs-check"] == 1
    assert row["counts"]["new"] == 1
    assert row["built"] is False, "nothing has been built into chapters yet"


def test_the_scans_index_reports_a_built_novel(client):
    pid = _novel(client)
    _two_page_chapter(client, pid)
    client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})

    row = client.get("/api/scans").json()["novels"][0]
    assert row["built"] is True and row["chapter_count"] == 1


def test_an_empty_library_gives_an_empty_index(client):
    assert client.get("/api/scans").json()["novels"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

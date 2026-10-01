"""End-to-end tests for the scanned-page HTTP flow.

Walks the real path a photographed novel takes: create → upload into chapter boxes →
correct → arrange → stitch → build → and out the other side as an ordinary novel the
translator can read.

No model is ever called. The OCR calls themselves are covered by the parser tests;
what matters here is the plumbing around them, and running real translations from a
test would spend the user's plan quota.

What has to hold:
- A page's type is decided by its bytes, and re-uploading the same photo is a no-op
  that says where the photo already is.
- Action routes (/pages/move, /pages/batches, /pages/build, ...) are never captured as
  a page id, and /pages/batches/order is never taken for a box id.
- Editing a page's join is marked as the reader's decision, so a later automatic pass
  cannot overwrite it.
- Every change to the pages answers with the rail as saved, under the rev it was saved
  with, so the client can keep the newest answer and drop a stale one.
- After building, ``get_chapters`` reads the built source with no network — that one
  branch is the entire integration with the translation pipeline.
- A build appends by default, so photographing a new chapter never renumbers the
  chapters already translated — while a built chapter follows later fixes to its box.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_pages_api.py``).
"""

import itertools
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from server.locks import file_lock  # noqa: E402
from translation_bot import state as state_mod  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.docs_extract import Chapter  # noqa: E402
from translation_bot.ocr import JoinDecision  # noqa: E402
from translation_bot.state import State  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 256
JPEG2 = b"\xff\xd8\xff\xe0" + b"\x01" * 256
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 256

_photos = itertools.count(1)


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


def _photo() -> bytes:
    """A JPEG no other upload shares, so it is never taken for a re-drop of one."""
    return b"\xff\xd8\xff\xe0" + next(_photos).to_bytes(8, "big") + b"\x00" * 248


def _upload(client, pid, data=JPEG, **params):
    return client.post(f"/api/projects/{pid}/pages", content=data,
                       headers={"Content-Type": "image/jpeg"}, params=params)


def _set(client, pid, page_id, **fields):
    res = client.post(f"/api/projects/{pid}/pages/{page_id}", json=fields)
    assert res.status_code == 200, res.text
    return res.json()


def _post(client, pid, action, body=None):
    return client.post(f"/api/projects/{pid}/pages/{action}", json=body or {})


def _rail(client, pid):
    return client.get(f"/api/projects/{pid}/pages").json()


def _boxes(client, pid):
    """Each box's photo ids, in order — what the Pages tab shows."""
    rail = _rail(client, pid)
    return [[p["id"] for p in rail["pages"] if p["batch"] == box["id"]]
            for box in rail["batches"]]


def _pages_by_id(client, pid):
    return {p["id"]: p for p in _rail(client, pid)["pages"]}


def _chapter(client, pid, *texts, batch=""):
    """Photos for one chapter box, read and accepted — in a new box unless ``batch``
    names one. Returns (box id, page ids)."""
    ids = []
    for text in texts:
        res = _upload(client, pid, data=_photo(), batch=batch)
        assert res.status_code == 200, res.text
        batch = res.json()["batch"]
        ids.append(res.json()["page"]["id"])
        _set(client, pid, ids[-1], text=text, status=P.STATUS_OK)
    return batch, ids


def _build(client, pid, **body):
    return client.post(f"/api/projects/{pid}/pages/build", json=body)


def _chapters(pid):
    A._chapter_cache.clear()
    return A.get_chapters(pid, pj.project_config(Config(), pj.get_project(pid)))


def _photo_ids(pid, index):
    return A._chapter_page_ids(pj.get_project(pid), index)


# ---- creating ----------------------------------------------------------------

def test_an_image_novel_starts_empty(client):
    pid = _novel(client)
    project = client.get(f"/api/projects/{pid}").json()
    assert project["source_type"] == "images"
    rail = _rail(client, pid)
    assert rail["counts"]["total"] == 0 and rail["batches"] == []
    assert rail["chapter_count"] == 0 and rail["rev"] == 0


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


def test_an_upload_with_no_box_starts_a_new_chapter_at_the_end(client):
    pid = _novel(client)
    first = _upload(client, pid, data=_photo()).json()
    second = _upload(client, pid, data=_photo(), label="제2화: 시작!").json()
    assert first["batch"] != second["batch"]
    assert (first["number"], second["number"]) == (1, 2)
    boxes = _rail(client, pid)["batches"]
    assert [b["id"] for b in boxes] == [first["batch"], second["batch"]]
    assert boxes[1]["label"] == "제2화: 시작!", "a chapter title keeps its punctuation"


def test_uploading_into_an_earlier_box_lands_at_the_end_of_that_box(client):
    """Not at the end of the novel, where it would be read, stitched and built as part
    of whichever chapter came last."""
    pid = _novel(client)
    first, (a1,) = _chapter(client, pid, "가.")
    _second, (b1,) = _chapter(client, pid, "나.")
    res = _upload(client, pid, data=_photo(), batch=first).json()
    assert res["batch"] == first and res["number"] == 1
    assert _boxes(client, pid) == [[a1, res["page"]["id"]], [b1]]


def test_uploading_into_an_unknown_box_is_refused_not_a_new_box(client):
    """An unknown box used to become a new one, so a drop whose box was deleted mid-way
    split the rest of its photos off into a chapter of their own."""
    pid = _novel(client)
    _chapter(client, pid, "가.")
    res = _upload(client, pid, data=_photo(), batch="deadbeef")
    assert res.status_code == 404
    assert "chapter box isn't in this novel" in res.text
    rail = _rail(client, pid)
    assert rail["counts"]["total"] == 1 and len(rail["batches"]) == 1


def test_a_duplicate_reports_its_box_and_chapter_without_a_batch_key(client):
    """The old client took the drop's box from each answer, so a "batch" on a duplicate
    would send the rest of a new chapter's photos into the old one."""
    pid = _novel(client)
    first, _ = _chapter(client, pid, "가.")
    _upload(client, pid, data=JPEG, batch=first)
    second, _ = _chapter(client, pid, "나.")

    res = _upload(client, pid, data=JPEG, batch=second).json()
    assert res["duplicate"] is True and "batch" not in res
    assert (res["existing_batch"], res["existing_number"]) == (first, 1)
    assert res["total"] == 3


# ---- listing -----------------------------------------------------------------

def test_the_page_list_omits_text(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="그는 문을 열었다.")

    row = _rail(client, pid)["pages"][0]
    assert "text" not in row, "page text would make the rail payload megabytes"
    assert row["chars"] == len("그는 문을 열었다.")

    full = client.get(f"/api/projects/{pid}/pages/{page['id']}").json()
    assert full["text"] == "그는 문을 열었다.", "the single-page endpoint does carry it"


def test_list_reports_chapter_count_box_numbers_and_rev(client):
    pid = _novel(client)
    _two_page_chapter(client, pid)
    _build(client, pid)
    _chapter(client, pid, "다음 장.")

    rail = _rail(client, pid)
    assert rail["chapter_count"] == 1
    assert [(b["number"], b["state"]) for b in rail["batches"]] == [(1, "built"), (2, "ready")]
    assert rail["rev"] == P.load_pages(pid)["rev"] > 0
    for box in rail["batches"]:
        assert {"id", "label", "title", "at", "count", "chapter", "number", "state",
                "blockers"} <= set(box)


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
    _box, (first, second) = _chapter(client, pid, "그는 천천히 문을", "열었다.")
    updated = _set(client, pid, second, join_prev="sentence")
    assert updated["join_prev"] == "sentence"
    assert updated["join_prev_source"] == "user"
    assert updated["join_prev_id"] == first, "decided against the photo before it"
    assert updated["rev"] == _rail(client, pid)["rev"]


def test_a_join_on_a_chapters_first_photo_has_nothing_to_join(client):
    pid = _novel(client)
    _box, (only,) = _chapter(client, pid, "본문.")
    assert _set(client, pid, only, join_prev="sentence")["join_prev"] == "", \
        "the answer is what was saved"


def test_an_unknown_status_or_join_is_refused(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    assert client.post(f"/api/projects/{pid}/pages/{page['id']}",
                       json={"status": "banana"}).status_code == 400
    assert client.post(f"/api/projects/{pid}/pages/{page['id']}",
                       json={"join_prev": "banana"}).status_code == 400


def test_skipping_a_photo_resets_the_join_that_now_spans_it(client):
    """The page after a skipped one joins onto the page before it, which is a page
    break nobody has decided."""
    pid = _novel(client)
    _box, (p1, p2, p3) = _chapter(client, pid, "가", "나", "다")
    _set(client, pid, p2, join_prev="sentence")
    _set(client, pid, p3, join_prev="sentence")

    _set(client, pid, p2, status=P.STATUS_SKIPPED)
    third = _pages_by_id(client, pid)[p3]
    assert third["join_prev"] == "" and third["join_prev_id"] == p1

    restored = _set(client, pid, p2, status=P.STATUS_OK)
    assert restored["join_prev"] == "sentence", "p2 still follows p1, so its join stands"


# ---- arranging photos --------------------------------------------------------

def test_pages_can_be_moved_within_a_box(client):
    pid = _novel(client)
    box, (a, b) = _chapter(client, pid, "가.", "나.")
    res = _post(client, pid, "move", {"ids": [b], "batch": box, "before": a})
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is True
    assert _boxes(client, pid) == [[b, a]]

    again = _post(client, pid, "move", {"ids": [b], "batch": box, "before": a})
    assert again.json()["changed"] is False, "putting it back where it is changes nothing"


def test_photos_can_be_moved_into_another_box(client):
    pid = _novel(client)
    _first, (a1, a2) = _chapter(client, pid, "가.", "나.")
    second, (b1,) = _chapter(client, pid, "다.")
    res = _post(client, pid, "move", {"ids": [a2], "batch": second, "before": b1})
    assert res.status_code == 200, res.text
    assert _boxes(client, pid) == [[a1], [a2, b1]]


def test_a_move_naming_a_missing_page_is_refused(client):
    """Moving the rest would put them somewhere the reader never asked for."""
    pid = _novel(client)
    box, (a, b) = _chapter(client, pid, "가.", "나.")
    res = _post(client, pid, "move", {"ids": [b, "deadbeef"], "batch": box, "before": a})
    assert res.status_code == 404
    assert _boxes(client, pid) == [[a, b]], "nothing moved"


def test_action_routes_are_not_captured_as_page_ids(client):
    """/pages/move must reach the move endpoint, not look up a page called 'move' —
    and /pages/batches/order must not be taken for a box called 'order'."""
    pid = _novel(client)
    for action, body in (("delete", {"ids": []}),
                         ("move", {"ids": [], "batch": "deadbeef"}),
                         ("batches", {}),
                         ("batches/order", {"ids": []}),
                         ("stitch", {"use_model": False}),
                         ("build", {"preview": True})):
        res = _post(client, pid, action, body)
        assert res.status_code != 404, f"/pages/{action} answered {res.text}"
        assert "That page isn't in this novel" not in res.text, \
            f"/pages/{action} was captured as a page id"
        assert P.BOX_GONE not in res.text, f"/pages/{action} was captured as a box id"


# ---- chapter boxes -----------------------------------------------------------

def test_creating_renaming_and_reordering_boxes(client):
    pid = _novel(client)
    first = _post(client, pid, "batches", {"label": "제1화: 시작"}).json()["batch"]
    assert (first["title"], first["number"], first["state"]) == ("제1화: 시작", 1, "empty")
    second = _post(client, pid, "batches").json()["batch"]["id"]

    renamed = _post(client, pid, f"batches/{second}", {"label": "제2화!"}).json()
    assert renamed["batch"]["label"] == "제2화!"

    ordered = _post(client, pid, "batches/order", {"ids": [second, first["id"]]})
    assert ordered.status_code == 200, ordered.text
    rows = ordered.json()["pages"]["batches"]
    assert [(r["id"], r["number"]) for r in rows] == [(second, 1), (first["id"], 2)]
    assert _post(client, pid, "batches/order", {"ids": [second]}).status_code == 400, \
        "a partial order would lose the boxes it leaves out"


def test_splitting_a_chapter_is_one_step(client):
    """The box is created and the photos moved in one request: done as two, a refused
    move left an empty box behind that blocked every later build."""
    pid = _novel(client)
    box, (p1, p2, p3) = _chapter(client, pid, "가.", "나.", "다.")
    res = _post(client, pid, "batches", {"after": box, "ids": [p2, p3]})
    assert res.status_code == 200, res.text
    assert _boxes(client, pid) == [[p1], [p2, p3]]

    refused = _post(client, pid, "batches", {"after": box, "ids": [p1, "deadbeef"]})
    assert refused.status_code == 404
    assert _boxes(client, pid) == [[p1], [p2, p3]], "a refused split creates no box"


def test_a_built_box_cannot_be_moved_or_deleted(client):
    """Its chapter number is fixed in the chapter list, translations and all."""
    pid = _novel(client)
    one, _ = _chapter(client, pid, "1장.")
    two, _ = _chapter(client, pid, "2장.")
    _build(client, pid)
    three, _ = _chapter(client, pid, "3장.")

    assert _post(client, pid, "batches/order", {"ids": [two, one, three]}).status_code == 409
    assert _post(client, pid, f"batches/{one}/delete").status_code == 409
    assert _post(client, pid, "batches", {"after": one}).status_code == 409, \
        "a new chapter can't go between two built ones"
    assert _post(client, pid, "batches", {"after": two}).status_code == 200


def test_deleting_a_box_removes_its_photos(client):
    pid = _novel(client)
    box, (p1, p2) = _chapter(client, pid, "가.", "나.")

    stale = _post(client, pid, f"batches/{box}/delete", {"expect_page_ids": [p1]})
    assert stale.status_code == 409, "a photo the reader wasn't shown is never deleted"
    assert "Look again" in stale.text

    res = _post(client, pid, f"batches/{box}/delete", {"expect_page_ids": [p1, p2]})
    assert res.status_code == 200 and res.json()["removed"] == 2
    assert client.get(f"/api/projects/{pid}/pages/{p1}/image").status_code == 404
    assert _rail(client, pid)["batches"] == []


def test_a_malformed_box_id_is_a_clean_404(client):
    pid = _novel(client)
    assert _post(client, pid, "batches/ZZZZ", {"label": "x"}).status_code == 404
    assert _post(client, pid, "batches/ZZZZ/delete").status_code == 404


def test_structural_writes_return_the_fresh_rail_with_the_saved_rev(client):
    """The client keeps whichever answer carries the newest rev, which is what stops a
    poll that left before a move landed from putting the old order back on screen."""
    pid = _novel(client)
    revs = []

    def saved(res):
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["rev"] == body["pages"]["rev"] == _rail(client, pid)["rev"]
        revs.append(body["rev"])
        return body

    up = saved(_upload(client, pid, data=_photo()))
    box, page = up["batch"], up["page"]["id"]
    second = saved(_upload(client, pid, data=_photo(), batch=box))["page"]["id"]
    saved(_upload(client, pid, data=JPEG, batch=box))
    saved(_upload(client, pid, data=JPEG, batch=box))  # a duplicate answers the same way
    saved(_post(client, pid, "move", {"ids": [second], "batch": box, "before": page}))
    other = saved(_post(client, pid, "batches"))["batch"]["id"]
    saved(_post(client, pid, f"batches/{other}", {"label": "둘째"}))
    saved(_post(client, pid, "batches/order", {"ids": [other, box]}))
    saved(_post(client, pid, f"batches/{other}/delete"))
    saved(_post(client, pid, "delete", {"ids": [page]}))
    assert revs == sorted(set(revs)), "every change is saved under a newer rev"


def test_an_unknown_request_field_is_refused(client):
    """Unknown fields are ignored by default, so a field this server doesn't know would
    be silently half-obeyed — a split that creates a box and moves nothing into it."""
    pid = _novel(client)
    box, (page,) = _chapter(client, pid, "가.")
    for action, body in (("move", {"ids": [page], "batch": box, "position": 0}),
                         ("batches", {"photos": [page]}),
                         ("batches/order", {"ids": [box], "lock": True}),
                         (f"batches/{box}", {"label": "x", "colour": "red"}),
                         (f"batches/{box}/delete", {"expect": [page]}),
                         ("build", {"separator": "---"})):
        res = _post(client, pid, action, body)
        assert res.status_code == 422, f"/pages/{action} accepted an unknown field"


# ---- stitching ---------------------------------------------------------------

def test_stitching_decides_seams_without_a_model(client):
    pid = _novel(client)
    _box, (_first, second) = _chapter(client, pid, "그는 천천히 문을", "열고 안으로 들어갔다.")

    res = _post(client, pid, "stitch", {"use_model": False})
    assert res.status_code == 200, res.text
    assert res.json()["asked_model"] == 0, "the free rules must settle this seam"

    assert _pages_by_id(client, pid)[second]["join_prev"] == "sentence", \
        "an unfinished sentence across a page break must not become a paragraph"


def test_stitching_leaves_a_reader_decision_alone(client):
    pid = _novel(client)
    _box, (_first, second) = _chapter(client, pid, "그는 천천히 문을", "열고 안으로 들어갔다.")
    _set(client, pid, second, join_prev="chapter")

    _post(client, pid, "stitch", {"use_model": False})
    assert _pages_by_id(client, pid)[second]["join_prev"] == "chapter", \
        "the reader's own decision must stand"


def test_stitching_only_pairs_pages_inside_a_box(client):
    """The first photo of a box starts a chapter. A join onto the last photo of the
    chapter before would glue two chapters together."""
    pid = _novel(client)
    _chapter(client, pid, "그는 천천히 문을")
    _box, (opener,) = _chapter(client, pid, "열고 안으로 들어갔다.")

    res = _post(client, pid, "stitch", {"use_model": False}).json()
    assert res["seams"] == 0
    assert _pages_by_id(client, pid)[opener]["join_prev"] == ""


# Each of these ends mid-sentence and the next opens a new quotation: a seam the free
# rules can't settle, so it goes to the model.
UNSURE = ("그가 조용히", "「가자」고 하자 그녀가", "「응」이라고 하며 그가", "「좋아」라고 웃으며")


def _no_model_call(monkeypatch, then=None):
    """Stand in for the model: every seam it is asked about continues the sentence."""
    calls = []

    def stitch_boundaries(_translator, pairs, hooks=None):
        calls.append(pairs)
        if then:
            then()
        return ([JoinDecision(i=i, join="sentence", note="read both")
                 for i in range(1, len(pairs) + 1)], {}, 0.0)

    monkeypatch.setattr(A, "Translator", lambda *args, **kwargs: object())
    monkeypatch.setattr(A.ocr, "stitch_boundaries", stitch_boundaries)
    return calls


def test_stitching_keeps_model_decisions_and_moves_past_the_cap(client, monkeypatch):
    """Model decisions used to be recomputed on every press, so each press re-asked the
    same first seams and the ones after the cap never reached the model at all."""
    pid = _novel(client)
    _box, ids = _chapter(client, pid, *UNSURE)
    calls = _no_model_call(monkeypatch)

    first = _post(client, pid, "stitch", {"max_model_seams": 2}).json()
    assert (first["asked_model"], first["remaining"]) == (2, 1)
    second = _post(client, pid, "stitch", {"max_model_seams": 2}).json()
    assert (second["asked_model"], second["remaining"]) == (1, 0)
    assert calls[1] == [(UNSURE[2], UNSURE[3])], "the second press asks only the seam left"

    by_id = _pages_by_id(client, pid)
    assert [(by_id[i]["join_prev"], by_id[i]["join_prev_source"]) for i in ids[1:]] == \
        [("sentence", "model")] * 3


def test_stitch_skips_a_seam_whose_neighbour_moved_during_the_model_call(client, monkeypatch):
    pid = _novel(client)
    box, (p1, p2, p3, p4) = _chapter(client, pid, *UNSURE)

    def drag_the_last_photo_to_the_front():
        with P.mutate_pages(pid) as doc:
            P.move_pages(doc, [p4], box, before=p1)

    _no_model_call(monkeypatch, then=drag_the_last_photo_to_the_front)
    res = _post(client, pid, "stitch").json()
    assert res["seams"] == 2

    by_id = _pages_by_id(client, pid)
    assert by_id[p4]["join_prev"] == "", \
        "p4 now opens the chapter; its decision was about joining p3"
    assert by_id[p1]["join_prev"] == "", "p1 now follows p4, which nobody has decided"
    assert by_id[p2]["join_prev"] == by_id[p3]["join_prev"] == "sentence"


def test_stitch_never_overwrites_what_the_reader_did_during_the_model_call(
        client, monkeypatch):
    pid = _novel(client)
    _box, (p1, p2, p3, p4) = _chapter(client, pid, *UNSURE)

    def the_reader_works_meanwhile():
        with P.mutate_pages(pid) as doc:
            P.find_page(doc, p1)["text"] = "그가 아주 조용히"
            P.set_seam(P.find_page(doc, p3), kind="paragraph", glue="space",
                       source="user", reason="set by you", prev=p2)

    _no_model_call(monkeypatch, then=the_reader_works_meanwhile)
    res = _post(client, pid, "stitch").json()
    assert res["seams"] == 1, "only the seam nobody touched is written"

    by_id = _pages_by_id(client, pid)
    assert by_id[p2]["join_prev"] == "", "decided from p1's old text"
    assert (by_id[p3]["join_prev"], by_id[p3]["join_prev_source"]) == ("paragraph", "user")
    assert by_id[p4]["join_prev"] == "sentence"


# ---- building ----------------------------------------------------------------

def _two_page_chapter(client, pid):
    """Two photos in one chapter box, read, accepted and joined mid-sentence.

    The client sends the box id returned by the first upload with the rest of the
    drop; omitting it deliberately starts a new box (a separate chapter).
    """
    first_res = _upload(client, pid, data=JPEG).json()
    batch = first_res["batch"]
    first = first_res["page"]
    second = _upload(client, pid, data=JPEG2, batch=batch).json()["page"]
    _set(client, pid, first["id"], text="그는 천천히 문을", status=P.STATUS_OK)
    _set(client, pid, second["id"], text="열고 안으로 들어갔다.",
         join_prev="sentence", status=P.STATUS_OK)
    return first, second


def test_one_box_is_one_chapter(client):
    """A box is how the reader says 'these photos are one chapter'."""
    pid = _novel(client)
    first = _upload(client, pid, data=JPEG).json()
    _upload(client, pid, data=JPEG2, batch=first["batch"])
    _upload(client, pid, data=PNG)  # a new box

    for page in _rail(client, pid)["pages"]:
        _set(client, pid, page["id"], text="본문입니다.", status=P.STATUS_OK)

    built = _build(client, pid)
    assert built.json()["chapters"] == 2, "two boxes make two chapters, not three"


def test_building_produces_chapters_the_pipeline_can_read(client):
    pid = _novel(client)
    _two_page_chapter(client, pid)

    res = _build(client, pid)
    assert res.status_code == 200, res.text
    assert res.json()["chapters"] == 1

    # The whole point: from here it is an ordinary novel, read with no network.
    chapters = _chapters(pid)
    assert len(chapters) == 1
    assert chapters[0].paragraphs == ["그는 천천히 문을 열고 안으로 들어갔다."], \
        "a sentence spanning a page break must arrive as ONE paragraph"


def test_building_reports_the_chapter_count_on_the_novel(client):
    pid = _novel(client)
    _two_page_chapter(client, pid)
    _build(client, pid)
    assert client.get(f"/api/projects/{pid}").json()["chapter_count"] == 1


def test_building_with_nothing_approved_is_refused_helpfully(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="확인 안 됨", status=P.STATUS_NEEDS_CHECK)

    res = _build(client, pid)
    assert res.status_code == 400
    assert "needs checking" in res.text, "it says which photo and what to do about it"


def test_a_second_build_appends_without_renumbering(client):
    """Photograph chapter 2, build, and chapter 1 keeps its index and translation."""
    pid = _novel(client)
    _two_page_chapter(client, pid)
    _build(client, pid)

    later = _upload(client, pid, data=PNG).json()["page"]
    _set(client, pid, later["id"], text="다음 장의 본문.", status=P.STATUS_OK)
    res = _build(client, pid, append=True)
    assert res.status_code == 200, res.text
    assert res.json()["chapters"] == 2 and res.json()["added"] == [2]

    chapters = _chapters(pid)
    assert chapters[0].paragraphs == ["그는 천천히 문을 열고 안으로 들어갔다."], \
        "the first chapter must be untouched by a later build"
    assert chapters[1].paragraphs == ["다음 장의 본문."]


def test_an_append_keeps_the_earlier_chapters_photo_attribution(client):
    """page_map is the ONLY record of which photos a chapter was built from.

    An append only builds the new chapters. Storing their map wholesale dropped every
    earlier entry, and the reader's Photo/Text toggle and its source images silently
    vanished from every chapter built before the append — permanently, with no error,
    and recoverable only by a full rebuild that renumbers and re-bills the whole novel.
    """
    pid = _novel(client)
    _two_page_chapter(client, pid)
    _build(client, pid)
    before = (P.load_pages(pid)["build"] or {})["page_map"]
    assert "1" in before, "the first build attributes chapter 1 to its photos"

    later = _upload(client, pid, data=PNG).json()["page"]
    _set(client, pid, later["id"], text="다음 장의 본문.", status=P.STATUS_OK)
    _build(client, pid, append=True)

    after = (P.load_pages(pid)["build"] or {})["page_map"]
    assert after.get("1") == before["1"], "chapter 1 must keep its photos"
    assert "2" in after, "and the appended chapter must gain its own"
    assert _photo_ids(pid, 1), "the reader must still find chapter 1's photos"
    assert _photo_ids(pid, 2)


# ---- a built chapter follows later fixes to its box --------------------------
# A built chapter used to be frozen to the photos it was built from: a correction was
# dropped ("No pages are ready to build"), a reorder did nothing, and a photo added to
# it became a new chapter at the end of the novel.

def test_correcting_a_built_page_reaches_the_chapter(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="원래 본문입니다.", status=P.STATUS_OK)
    _build(client, pid)

    _set(client, pid, page["id"], text="고쳐진 본문입니다.", status=P.STATUS_OK)
    res = _build(client, pid, append=True)

    assert res.status_code == 200, res.text
    assert res.json()["refreshed"] == [1]
    assert _chapters(pid)[0].paragraphs == ["고쳐진 본문입니다."]


def test_a_correction_says_the_translation_is_now_stale(client):
    """Silently updating the Korean under a finished translation would be its own
    trap, so the build says what it did and what it means."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="원래 본문입니다.", status=P.STATUS_OK)
    _build(client, pid)
    _set(client, pid, page["id"], text="고쳐진 본문입니다.", status=P.STATUS_OK)

    warnings = _build(client, pid, append=True).json()["warnings"]
    assert any("re-translate" in w.lower() for w in warnings)


def test_building_with_nothing_changed_is_still_refused(client):
    """The refresh must not turn "nothing to do" into a silent success."""
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    _set(client, pid, page["id"], text="원래 본문입니다.", status=P.STATUS_OK)
    _build(client, pid)

    res = _build(client, pid, append=True)
    assert res.status_code == 400
    assert "up to date" in res.text


def test_a_correction_does_not_disturb_the_other_chapters(client):
    pid = _novel(client)
    first = _upload(client, pid, data=JPEG).json()
    _set(client, pid, first["page"]["id"], text="첫 장.", status=P.STATUS_OK)
    _build(client, pid)
    second = _upload(client, pid, data=JPEG2).json()
    _set(client, pid, second["page"]["id"], text="둘째 장.", status=P.STATUS_OK)
    _build(client, pid, append=True)

    _set(client, pid, first["page"]["id"], text="첫 장, 고침.", status=P.STATUS_OK)
    res = _build(client, pid, append=True)

    assert res.json()["refreshed"] == [1], "only the corrected chapter is rebuilt"
    assert [c.paragraphs for c in _chapters(pid)] == [["첫 장, 고침."], ["둘째 장."]]


def test_a_photo_added_to_a_built_chapter_joins_that_chapter(client):
    pid = _novel(client)
    box, (p1,) = _chapter(client, pid, "첫 쪽.")
    _build(client, pid)
    _box, (p2,) = _chapter(client, pid, "둘째 쪽.", batch=box)

    res = _build(client, pid).json()
    assert (res["chapters"], res["refreshed"], res["added"]) == (1, [1], [])
    assert _chapters(pid)[0].paragraphs == ["첫 쪽.", "둘째 쪽."]
    assert _photo_ids(pid, 1) == [p1, p2]


def test_reordering_a_built_chapter_reaches_the_text_and_photo_strip(client):
    pid = _novel(client)
    box, (p1, p2) = _chapter(client, pid, "첫째 쪽.", "둘째 쪽.")
    _build(client, pid)
    _post(client, pid, "move", {"ids": [p2], "batch": box, "before": p1})
    assert _rail(client, pid)["batches"][0]["state"] == "changed"

    res = _build(client, pid).json()
    assert res["refreshed"] == [1]
    assert _chapters(pid)[0].paragraphs == ["둘째 쪽.", "첫째 쪽."]
    assert _photo_ids(pid, 1) == [p2, p1], "the reader's Photo view follows the new order"


def test_an_unchecked_page_holds_its_chapter_back(client):
    """It used to be left out of the middle of the chapter, then built later as a
    chapter of its own at the end of the novel."""
    pid = _novel(client)
    _box, (_p1, p2, _p3) = _chapter(client, pid, "그는 문을", "천천히", "닫았다.")
    _set(client, pid, p2, status=P.STATUS_NEEDS_CHECK)

    held = _build(client, pid)
    assert held.status_code == 400 and "needs checking" in held.text

    _set(client, pid, p2, status=P.STATUS_OK)
    assert _build(client, pid).json()["added"] == [1]
    assert _chapters(pid)[0].paragraphs == ["그는 문을 천천히 닫았다."]


def test_deleting_the_last_photo_of_a_built_chapter_keeps_its_text(client):
    """A built chapter keeps its number, so emptying its box can't make it vanish —
    that would renumber every chapter after it. Start over is what retires one."""
    pid = _novel(client)
    _box, (page,) = _chapter(client, pid, "본문.")
    _build(client, pid)
    _post(client, pid, "delete", {"ids": [page]})

    row = _rail(client, pid)["batches"][0]
    assert row["state"] == "changed-waiting"
    assert [b["kind"] for b in row["blockers"]] == ["empty"]
    res = _build(client, pid)
    assert res.status_code == 400 and "no photos left" in res.text
    assert _chapters(pid)[0].paragraphs == ["본문."]


def test_a_box_stays_built_after_a_build_and_a_model_free_stitch(client):
    """The build writes back the joins it decides, so working out the joins afterwards
    agrees with it instead of flagging the chapter as changed."""
    pid = _novel(client)
    _chapter(client, pid, "그는 천천히 문을", "열고 들어가며 그가 조용히", "「가자」고 말했다.")
    _build(client, pid)
    _post(client, pid, "stitch", {"use_model": False})
    assert _rail(client, pid)["batches"][0]["state"] == "built"


def test_a_gap_is_reported_in_the_build_warnings(client):
    pid = _novel(client)
    first_res = _upload(client, pid, data=JPEG).json()
    first = first_res["page"]
    second = _upload(client, pid, data=JPEG2, batch=first_res["batch"]).json()["page"]
    _set(client, pid, first["id"], text="그는 문을 열었다.", status=P.STATUS_OK)
    _set(client, pid, second["id"], text="배는 이미 떠난 뒤였다.",
         join_prev="gap", status=P.STATUS_OK)

    warnings = _build(client, pid).json()["warnings"]
    assert warnings and "missing" in warnings[0], \
        "a page that was never photographed must be surfaced"


def test_chapter_photo_ids_skip_deleted_pages(client):
    pid = _novel(client)
    _box, (p1, p2) = _chapter(client, pid, "가.", "나.")
    _build(client, pid)
    _post(client, pid, "delete", {"ids": [p1]})
    assert _photo_ids(pid, 1) == [p2], "a deleted photo would only show as a broken image"


# ---- the build endpoint ------------------------------------------------------

def test_build_preview_writes_nothing(client):
    pid = _novel(client)
    _chapter(client, pid, "본문.")
    before = P.pages_file(pid).read_bytes()

    preview = _build(client, pid, preview=True)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["preview"] is True and body["added"] == [1] and "project" not in body
    assert P.pages_file(pid).read_bytes() == before
    assert not (pj.PROJECTS_DIR / pid / "source.json").exists()

    real = _build(client, pid).json()
    assert (real["preview"], real["added"]) == (False, body["added"]), \
        "the preview said exactly what the build would do"


def test_pile_build_modes_are_refused(client):
    """Each chapter box builds one chapter; heading/separator/single split one pile
    on lines that only looked like chapter breaks."""
    pid = _novel(client)
    _chapter(client, pid, "본문.")
    for mode in ("heading", "separator", "single"):
        assert _build(client, pid, mode=mode).status_code == 400
    assert _build(client, pid, include="some").status_code == 400
    assert _build(client, pid, mode="batch").status_code == 200, \
        "'batch' always meant one chapter per box"


def test_build_refuses_when_chapters_and_boxes_disagree(client):
    """An append would otherwise put the next chapter under the wrong number."""
    pid = _novel(client)
    _chapter(client, pid, "1장.")
    _build(client, pid)
    pj.cache_source(pid, _chapters(pid) + [Chapter(2, "Chapter 2", ["어디서 왔지?"])])
    _chapter(client, pid, "2장.")

    for preview in (False, True):
        res = _build(client, pid, preview=preview)
        assert res.status_code == 409 and "Start over" in res.text
    assert _build(client, pid, append=False).status_code == 200


def test_start_over_removes_empty_boxes_and_refuses_boxes_that_arent_ready(client):
    """Stopping at a box that isn't ready would quietly drop every chapter after it."""
    pid = _novel(client)
    _chapter(client, pid, "1장.")
    _build(client, pid)
    _chapter(client, pid, "2장.")
    _post(client, pid, "batches")
    _box, (unchecked,) = _chapter(client, pid, "3장.")
    _set(client, pid, unchecked, status=P.STATUS_NEEDS_CHECK)
    before = P.pages_file(pid).read_bytes()

    refused = _build(client, pid, append=False)
    assert refused.status_code == 400 and "needs checking" in refused.text
    assert not re.search(r"already translated|renumber", refused.text, re.I), \
        "the client asks to force exactly that message, and forcing can't fix this"
    assert P.pages_file(pid).read_bytes() == before

    preview = _build(client, pid, append=False, preview=True).json()
    assert preview["added"] == [1, 2] and [w["number"] for w in preview["waiting"]] == [3]

    _set(client, pid, unchecked, status=P.STATUS_OK)
    res = _build(client, pid, append=False)
    assert res.status_code == 200 and res.json()["chapters"] == 3
    assert [b["chapter"] for b in _rail(client, pid)["batches"]] == [1, 2, 3], \
        "the empty box is gone"


def test_start_over_still_asks_before_renumbering_translated_chapters(client):
    pid = _novel(client)
    _chapter(client, pid, "1장.")
    _build(client, pid)
    cfg = pj.project_config(Config(), pj.get_project(pid))
    state = State.load(cfg.paths.state_file)
    state.update(1, status=state_mod.STATUS_VALIDATED)
    state.save(cfg.paths.state_file)

    refused = _build(client, pid, append=False)
    assert refused.status_code == 400 and "already translated" in refused.text
    preview = _build(client, pid, append=False, preview=True).json()
    assert any("already translated" in w for w in preview["warnings"])
    assert _build(client, pid, append=False, force=True).status_code == 200


def test_a_full_rebuild_replaces_the_attribution_rather_than_merging_it(client):
    """The merge is append-only. A rebuild renumbers from 1, so carrying the old map
    forward would attribute a chapter to photos that are no longer under it."""
    pid = _novel(client)
    _two_page_chapter(client, pid)
    _build(client, pid)

    later = _upload(client, pid, data=PNG).json()["page"]
    _set(client, pid, later["id"], text="다음 장의 본문.", status=P.STATUS_OK)
    res = _build(client, pid, append=False, force=True)
    assert res.status_code == 200, res.text

    build = P.load_pages(pid)["build"] or {}
    assert set(build["page_map"]) == {str(i) for i in range(1, res.json()["chapters"] + 1)}, \
        "a rebuild's map covers exactly the chapters it just built"


def test_a_v1_whole_novel_split_stays_locked(client):
    """A version-1 pile build (heading/separator/single) never recorded which photo
    went into which chapter, so its photos can't be rebuilt chapter by chapter. They
    are kept, locked, and new boxes build after its chapters."""
    pid = _novel(client)
    old = [{"id": f"{n}aaaaaaa", "seq": n, "file": f"page-000{n}.jpg", "batch": "11111111",
            "status": P.STATUS_OK, "text": text, "join_prev": ""}
           for n, text in ((1, "제1화\n\n가."), (2, "제2화\n\n나."))]
    P.pages_file(pid).write_text(json.dumps({
        "version": 1, "pages": old, "batches": [{"id": "11111111", "label": ""}],
        "build": {"mode": "heading", "chapters": 2,
                  "page_map": {"*": ["1aaaaaaa", "2aaaaaaa"]},
                  "used_page_ids": ["1aaaaaaa", "2aaaaaaa"]}}), encoding="utf-8")
    pj.cache_source(pid, [Chapter(1, "제1화", ["가."]), Chapter(2, "제2화", ["나."])])

    legacy = _rail(client, pid)["batches"][0]
    assert (legacy["state"], legacy["number"], legacy["chapters"]) == ("legacy", None, [1, 2])
    assert _upload(client, pid, data=_photo(), batch=legacy["id"]).status_code == 409

    box, _ = _chapter(client, pid, "3장.")
    assert _post(client, pid, "move", {"ids": ["1aaaaaaa"], "batch": box}).status_code == 409
    res = _build(client, pid).json()
    assert res["added"] == [3] and res["refreshed"] == []
    assert [c.paragraphs for c in _chapters(pid)] == [["가."], ["나."], ["3장."]]
    assert {"1aaaaaaa", "2aaaaaaa"} <= set(P.load_pages(pid)["build"]["used_page_ids"])


def test_a_build_holds_the_pages_lock_throughout(client, monkeypatch):
    """Built from a copy read without the lock, a photo uploaded or moved mid-build was
    recorded as built without ever reaching a chapter, and two builds at once could
    both append the same chapter."""
    pid = _novel(client)
    _chapter(client, pid, "본문.")
    held = []
    write = pj.cache_source

    def cache_source(pid_, chapters):
        held.append(file_lock(P.pages_file(pid_))._is_owned())
        write(pid_, chapters)

    monkeypatch.setattr(pj, "cache_source", cache_source)
    assert _build(client, pid).status_code == 200
    assert held == [True], "source.json is written while the pages are still locked"


def test_reading_queues_pages_in_box_order(client, monkeypatch):
    """Photos are read in story order, not upload order."""
    pid = _novel(client)
    first = _upload(client, pid, data=_photo()).json()
    second = _upload(client, pid, data=_photo()).json()
    late = _upload(client, pid, data=_photo(), batch=first["batch"]).json()
    queued = []
    monkeypatch.setattr(A, "_enqueue_task", lambda _pid, _cfg, items: queued.extend(items)
                        or {"job_id": "j", "queued": [i for i, _f, _k in items]})

    assert _post(client, pid, "ocr", {}).status_code == 200
    assert [seq for seq, _f, _k in queued] == [first["page"]["seq"], late["page"]["seq"],
                                               second["page"]["seq"]]


def test_reading_results_keep_the_page_order_and_its_seams(client):
    pid = _novel(client)
    _box, (p1, p2) = _chapter(client, pid, "가", "나")
    _set(client, pid, p2, join_prev="sentence")

    A._set_page_status(pid, p1, P.STATUS_RUNNING)
    A._apply_page_result(pid, p1, {"text": "다시 읽은 가", "status": P.STATUS_OK,
                                   "_usage": {}, "_cost": 0.0})
    assert _boxes(client, pid) == [[p1, p2]]
    assert _pages_by_id(client, pid)[p2]["join_prev"] == "sentence"


# ---- deleting ----------------------------------------------------------------

def test_deleting_a_page_removes_its_file(client):
    pid = _novel(client)
    page = _upload(client, pid).json()["page"]
    assert _post(client, pid, "delete", {"ids": [page["id"]]}).json()["removed"] == 1
    assert client.get(f"/api/projects/{pid}/pages/{page['id']}/image").status_code == 404
    assert _rail(client, pid)["counts"]["total"] == 0


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
    _build(client, pid)

    row = client.get("/api/scans").json()["novels"][0]
    assert row["built"] is True and row["chapter_count"] == 1


def test_an_empty_library_gives_an_empty_index(client):
    assert client.get("/api/scans").json()["novels"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

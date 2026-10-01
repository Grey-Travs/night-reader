"""Tests for the scanned-page manifest (projects/<pid>/pages.json).

An image novel's pages are sorted into chapter boxes — one box per chapter, in reading
order — and each page is individually re-readable, movable, and addressed by a
server-minted id. The manifest is the only thing that maps a page id to a file on disk,
so it is also the security boundary for serving those images back.

What has to hold:
- An uploaded file's type is decided by its BYTES, never by the client's header or
  filename. A path is never built from a URL segment.
- A page's filename carries its sequence number, not its position, so moving a page
  never renames a file (which would break image caching and race an in-flight OCR
  call holding that path).
- A corrupt manifest reads as empty rather than raising — one bad file must not take
  down the novel, the same rule State/Glossary/project.json already follow.
- The pages are always grouped by box, in box order, and a seam survives only while
  the page before it is the one it was decided against.
- A refused change leaves everything as it was. A move naming a missing page moves
  nothing, rather than moving the rest somewhere nobody asked for — the same lesson as
  the old page reorder, which refused a partial list because it would drop pages.
- A version-1 manifest reads as the same boxes on every load, before anything is saved.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_pages_store.py``).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.pages as P  # noqa: E402

PID = "abcdef012345"

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 32
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32


@pytest.fixture
def project(monkeypatch, tmp_path):
    """Redirect the projects root at a temp dir and create one image project."""
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    (tmp_path / PID / P.PAGES_DIRNAME).mkdir(parents=True)
    return tmp_path / PID


def _box(doc, label=""):
    return P.create_box(doc, label)["id"]


def _add(doc, *, ext="jpg", digest="d0", batch=None, name="", text="", status=None):
    """Add a page to ``batch``, or to a new box at the end when none is given."""
    page = P.add_page(doc, ext=ext, data_len=1024, digest=digest,
                      batch=batch or _box(doc), name=name)
    page["text"] = text
    if status:
        page["status"] = status
    return page


def _box_of(doc, n, *, prefix="p"):
    """One box holding ``n`` pages, each with text and read. Returns (box id, pages)."""
    box = _box(doc)
    return box, [_add(doc, batch=box, digest=f"{prefix}{i}", name=f"IMG_{i}.jpg",
                      text=f"{i}쪽 본문입니다.", status=P.STATUS_OK) for i in range(1, n + 1)]


def _join(doc, page, kind="sentence"):
    """Decide a page's seam by hand, the way the editor does."""
    P.set_seam(page, kind=kind, glue="space", source="user", reason="set by you",
               prev=P.seam_neighbours(doc)[page["id"]])


def _order(doc):
    return [p["id"] for p in doc["pages"]]


def _ids(pages):
    return [p["id"] for p in pages]


def _row(doc, box_id):
    return next(row for row in P.summary(doc)["batches"] if row["id"] == box_id)


def _refused(status, change, *args, **kwargs):
    with pytest.raises(P.BoxError) as refusal:
        change(*args, **kwargs)
    assert refusal.value.status == status, refusal.value.message
    return refusal.value.message


# ---- the bytes decide, not the client ----------------------------------------

def test_supported_formats_are_recognised_by_magic_bytes():
    assert P.sniff_image(JPEG) == "jpg"
    assert P.sniff_image(PNG) == "png"
    assert P.sniff_image(WEBP) == "webp"


def test_unsupported_formats_are_rejected():
    for blob in (HEIC, PDF, GIF, b"not an image at all"):
        assert P.sniff_image(blob) is None, "only real JPEG/PNG/WebP may be stored"


def test_heic_gets_the_iphone_explanation():
    """iPhones shoot HEIC by default; browsers can't display it, so the fix has to
    be actionable rather than a bare 'unsupported format'."""
    reason = P.unsupported_reason(HEIC)
    assert "iPhone" in reason and "Most Compatible" in reason


def test_pdf_and_gif_get_their_own_explanations():
    assert "PDF" in P.unsupported_reason(PDF)
    assert "GIF" in P.unsupported_reason(GIF)


def test_a_lying_content_type_cannot_smuggle_a_file_in():
    """The upload endpoint trusts sniff_image, not the header — so a PDF announced
    as image/jpeg still has no extension and cannot be stored."""
    assert P.sniff_image(PDF) is None


# ---- filenames and sequence --------------------------------------------------

def test_filenames_come_from_the_sequence_not_the_client():
    doc = P.new_doc()
    page = _add(doc, name="../../etc/passwd.jpg")
    assert page["file"] == "page-0001.jpg", "the on-disk name is always server-minted"
    assert "/" not in page["file"] and "\\" not in page["file"]


def test_client_filename_survives_only_as_a_display_label():
    doc = P.new_doc()
    page = _add(doc, name="../../secret/IMG_0042.jpg")
    assert "/" not in page["name"] and "\\" not in page["name"], \
        "a label must never contain path separators"
    assert "IMG_0042" in page["name"], "the useful part of the name is still shown"


def test_sequence_is_monotonic_across_deletes():
    """Reusing a freed number would collide with an image still cached in a browser."""
    doc = P.new_doc()
    first = _add(doc, digest="a")
    second = _add(doc, digest="b")
    P.delete_pages(doc, [second["id"]])
    third = _add(doc, digest="c")
    assert third["seq"] > second["seq"], "sequence numbers are never reused"
    assert third["file"] != second["file"]
    assert first["file"] == "page-0001.jpg"


def test_new_doc_is_version_2():
    doc = P.new_doc()
    assert doc["version"] == 2 and doc["rev"] == 0
    assert doc["batches"] == [] and doc["pages"] == []


def test_a_box_title_keeps_its_punctuation():
    """safe_label is a filename sanitiser: it strips the : ! ? and quotes a chapter
    title is made of, and the title is what the chapter is built under."""
    assert P.clean_title('제3화: 문이 열렸다! "정말?"') == '제3화: 문이 열렸다! "정말?"'
    assert P.clean_title("  제3화\n문이\t열렸다​  ") == "제3화 문이 열렸다", \
        "line breaks become spaces and invisible characters go"
    assert len(P.clean_title("가" * 500)) == 120


# ---- duplicate uploads -------------------------------------------------------

def test_the_same_photo_is_recognised_by_hash():
    """Re-dropping a folder must be a no-op, not a doubled novel."""
    doc = P.new_doc()
    _add(doc, digest="samehash")
    assert P.find_by_hash(doc, "samehash") is not None
    assert P.find_by_hash(doc, "otherhash") is None


# ---- chapter boxes -----------------------------------------------------------

def test_a_page_added_to_an_earlier_box_lands_at_the_end_of_that_box():
    """Not at the end of the novel: there it would be read, stitched and built as
    part of whatever chapter came last."""
    doc = P.new_doc()
    a, b = _box(doc), _box(doc)
    a1 = _add(doc, batch=a, digest="1")
    b1 = _add(doc, batch=b, digest="2")
    a2 = _add(doc, batch=a, digest="3")
    assert _order(doc) == [a1["id"], a2["id"], b1["id"]]
    assert [box["count"] for box in doc["batches"]] == [2, 1]


def test_adding_to_an_unknown_or_legacy_box_is_refused():
    """An unknown box used to become a new one, splitting the rest of a drop off into
    a chapter of its own."""
    doc = P.new_doc()
    legacy = P.create_box(doc)
    _add(doc, batch=legacy["id"], digest="old")
    legacy["legacy"] = True
    P.normalize(doc)
    before = json.dumps(doc, sort_keys=True)

    _refused(404, _add, doc, batch="deadbeef", digest="x")
    _refused(409, _add, doc, batch=legacy["id"], digest="y")
    assert json.dumps(doc, sort_keys=True) == before, "a refused upload changes nothing"


def test_pages_follow_box_order_after_reordering_boxes():
    """The OCR queue, stitching and building all walk the page list, so it has to be
    in box order too."""
    doc = P.new_doc()
    a, b, c = _box(doc), _box(doc), _box(doc)
    pa, pb, pc = (_add(doc, batch=box, digest=box) for box in (a, b, c))
    assert P.reorder_boxes(doc, [c, a, b]) is True
    assert [box["id"] for box in doc["batches"]] == [c, a, b]
    assert _order(doc) == [pc["id"], pa["id"], pb["id"]]


def test_a_box_order_must_name_every_box_once():
    """A partial list would lose the boxes it leaves out."""
    doc = P.new_doc()
    a, b = _box(doc), _box(doc)
    _refused(400, P.reorder_boxes, doc, [a])
    _refused(400, P.reorder_boxes, doc, [a, a])
    _refused(400, P.reorder_boxes, doc, [a, b, "deadbeef"])
    assert [box["id"] for box in doc["batches"]] == [a, b]


def test_built_boxes_keep_their_place():
    doc = P.new_doc()
    built, x, y = _box(doc), _box(doc), _box(doc)
    for box in (built, x, y):
        _add(doc, batch=box, digest=box)
    P.find_box(doc, built)["chapter"] = 1
    P.normalize(doc)

    _refused(409, P.reorder_boxes, doc, [x, built, y])
    assert [box["id"] for box in doc["batches"]] == [built, x, y]
    assert P.reorder_boxes(doc, [built, y, x]) is True, "the unbuilt tail can move"
    assert [box["id"] for box in doc["batches"]] == [built, y, x]


def test_creating_a_box_with_photos_is_one_step():
    """"Split chapter here" creates the box and moves the photos together. Done as two
    requests, a refused move left an empty box that blocked every later build."""
    doc = P.new_doc()
    first, pages = _box_of(doc, 4)
    last = _box(doc)
    box = P.create_box(doc, "제2화", after=first, ids=_ids(pages[2:]))
    assert [b["id"] for b in doc["batches"]] == [first, box["id"], last], \
        "the new box goes straight after the one it was split from"
    assert [p["batch"] for p in doc["pages"]] == [first, first, box["id"], box["id"]]
    assert pages[2]["join_prev"] == "", "the split photo now opens its chapter"

    before = json.dumps(doc, sort_keys=True)
    _refused(404, P.create_box, doc, ids=["deadbeef"])
    _refused(404, P.create_box, doc, after="deadbeef")
    assert json.dumps(doc, sort_keys=True) == before, "a refusal creates no box"


def test_a_new_box_never_goes_between_two_built_ones():
    doc = P.new_doc()
    one, two = _box(doc), _box(doc)
    for n, box in enumerate((one, two), start=1):
        _add(doc, batch=box, digest=box)
        P.find_box(doc, box)["chapter"] = n
    P.normalize(doc)
    _refused(409, P.create_box, doc, after=one)
    box = P.create_box(doc, after=two)
    assert [b["id"] for b in doc["batches"]] == [one, two, box["id"]], \
        "after the last built box is allowed"


def test_renaming_a_box_keeps_its_punctuation():
    doc = P.new_doc()
    box = _box(doc)
    P.rename_box(doc, box, "제3화: 문이 열렸다!")
    assert P.find_box(doc, box)["label"] == "제3화: 문이 열렸다!"
    _refused(404, P.rename_box, doc, "deadbeef", "x")


def test_deleting_a_box_removes_its_pages_but_never_a_built_box():
    doc = P.new_doc()
    built, loose = _box(doc), _box(doc)
    kept = _add(doc, batch=built, digest="1")
    gone = _add(doc, batch=loose, digest="2")
    P.find_box(doc, built)["chapter"] = 1

    _refused(409, P.delete_box, doc, built)
    assert len(doc["pages"]) == 2
    removed = P.delete_box(doc, loose, expect_page_ids=[gone["id"]])
    assert _ids(removed) == [gone["id"]], "the caller unlinks exactly these files"
    assert _order(doc) == [kept["id"]]
    assert [box["id"] for box in doc["batches"]] == [built]


def test_deleting_a_box_whose_photos_changed_is_refused():
    """A photo uploaded into the box after the reader confirmed would otherwise be
    deleted without ever being shown to them."""
    doc = P.new_doc()
    box, pages = _box_of(doc, 2)
    _refused(409, P.delete_box, doc, box, expect_page_ids=[pages[0]["id"]])
    assert len(doc["pages"]) == 2, "nothing is deleted"


# ---- moving photos -----------------------------------------------------------

def test_move_within_a_box():
    doc = P.new_doc()
    box, (p1, p2, p3) = _box_of(doc, 3)
    assert P.move_pages(doc, [p3["id"]], box, before=p1["id"]) is True
    assert _order(doc) == [p3["id"], p1["id"], p2["id"]]
    assert P.move_pages(doc, [p3["id"]], box) is True, "no anchor means the end"
    assert _order(doc) == [p1["id"], p2["id"], p3["id"]]


def test_move_across_boxes_keeps_the_given_order_and_recounts():
    doc = P.new_doc()
    a, (a1, a2, a3) = _box_of(doc, 3, prefix="a")
    b, (b1,) = _box_of(doc, 1, prefix="b")
    files = {p["id"]: p["file"] for p in doc["pages"]}

    assert P.move_pages(doc, [a3["id"], a1["id"]], b, before=b1["id"]) is True
    assert _order(doc) == [a2["id"], a3["id"], a1["id"], b1["id"]]
    assert a1["batch"] == a3["batch"] == b
    assert [box["count"] for box in doc["batches"]] == [1, 3]
    assert {p["id"]: p["file"] for p in doc["pages"]} == files, \
        "moving must never rename a file"


def test_move_to_the_same_spot_is_a_no_op_and_keeps_every_seam():
    doc = P.new_doc()
    box, (p1, p2, p3) = _box_of(doc, 3)
    _join(doc, p2)
    _join(doc, p3)
    assert P.move_pages(doc, [p2["id"]], box, before=p3["id"]) is False
    assert P.move_pages(doc, [p2["id"]], box, before=p2["id"]) is False, \
        "dropped just before itself"
    assert P.move_pages(doc, [p3["id"]], box) is False, "already last"
    assert (p2["join_prev"], p3["join_prev"]) == ("sentence", "sentence")
    assert p2["join_prev_source"] == p3["join_prev_source"] == "user"


def test_move_resets_exactly_the_seams_whose_neighbour_changed():
    """Even the reader's own: a join describes one particular page break, and after
    the move that break no longer exists."""
    doc = P.new_doc()
    box, (p1, p2, p3, p4, p5) = _box_of(doc, 5)
    for page in (p2, p3, p4, p5):
        _join(doc, page)

    P.move_pages(doc, [p2["id"]], box, before=p5["id"])
    assert _order(doc) == _ids([p1, p3, p4, p2, p5])
    assert p3["join_prev"] == "", "p3 now follows p1"
    assert p2["join_prev"] == "", "p2 now follows p4"
    assert p5["join_prev"] == "", "p5 now follows p2"
    assert p4["join_prev"] == "sentence", "p4 still follows p3, so its seam stands"


def test_move_refuses_unknown_pages_box_or_anchor():
    doc = P.new_doc()
    a, (p1, _p2) = _box_of(doc, 2, prefix="a")
    b, (q1,) = _box_of(doc, 1, prefix="b")
    before = json.dumps(doc, sort_keys=True)

    _refused(404, P.move_pages, doc, [p1["id"], "deadbeef"], b)
    _refused(404, P.move_pages, doc, [p1["id"]], "deadbeef")
    _refused(400, P.move_pages, doc, [q1["id"]], b, before=p1["id"])
    _refused(400, P.move_pages, doc, [], a)
    assert json.dumps(doc, sort_keys=True) == before, "nothing moved"


def test_a_legacy_box_is_locked():
    doc = P.new_doc()
    legacy, (old,) = _box_of(doc, 1, prefix="old")
    P.find_box(doc, legacy)["legacy"] = True
    other, (new,) = _box_of(doc, 1, prefix="new")
    P.normalize(doc)
    _refused(409, P.move_pages, doc, [new["id"]], legacy)
    _refused(409, P.move_pages, doc, [old["id"]], other)
    _refused(409, P.rename_box, doc, legacy, "x")
    _refused(409, P.delete_box, doc, legacy)


# ---- seams -------------------------------------------------------------------

def test_deleting_a_page_resets_the_seam_after_it():
    doc = P.new_doc()
    _box_id, (p1, p2, p3) = _box_of(doc, 3)
    _join(doc, p2)
    _join(doc, p3)
    P.delete_pages(doc, [p2["id"]])
    assert p3["join_prev"] == "" and p3["join_prev_id"] == p1["id"]


def test_skipping_a_page_resets_the_seam_that_now_spans_it():
    doc = P.new_doc()
    _box_id, (p1, p2, p3) = _box_of(doc, 3)
    _join(doc, p2)
    _join(doc, p3)

    p2["status"] = P.STATUS_SKIPPED
    P.normalize(doc)
    assert p3["join_prev"] == "", "p3 now joins onto p1, not onto the skipped page"

    p2["status"] = P.STATUS_OK
    P.normalize(doc)
    assert p2["join_prev"] == "sentence", "p2 still follows p1, so its seam stands"
    assert p3["join_prev"] == ""


def test_the_first_page_of_a_box_has_no_seam():
    doc = P.new_doc()
    box, (p1, p2) = _box_of(doc, 2)
    p1["join_prev"] = "sentence"
    P.normalize(doc)
    assert p1["join_prev"] == "", "there is nothing before it in its chapter"

    _join(doc, p2)
    P.move_pages(doc, [p2["id"]], box, before=p1["id"])
    assert p2["join_prev"] == "" and p2["join_prev_id"] is None


def test_normalize_is_idempotent_and_adopts_orphans_deterministically():
    """A read creates boxes for pages that lost theirs; two reads must agree on their
    ids, and every id must be one a URL can carry."""
    def orphans():
        return {"version": 2, "batches": [], "build": None, "pages": [
            {"id": "aaaaaaaa", "seq": 1, "batch": "b1"},
            {"id": "bbbbbbbb", "seq": 2, "batch": "b1"},
            {"id": "cccccccc", "seq": 3, "batch": "0123abcd"},
            {"id": "dddddddd", "seq": 4},
        ]}

    first, second = P.normalize(orphans()), P.normalize(orphans())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    ids = [box["id"] for box in first["batches"]]
    assert len(ids) == 3 and all(P.is_box_id(i) for i in ids)
    assert "0123abcd" in ids, "an orphan's box id is kept when a URL can carry it"
    by_page = {p["id"]: p["batch"] for p in first["pages"]}
    assert by_page["aaaaaaaa"] == by_page["bbbbbbbb"], "one lost box stays one box"

    again = json.dumps(first, sort_keys=True)
    assert json.dumps(P.normalize(first), sort_keys=True) == again


# ---- version 1 manifests -----------------------------------------------------
# Version 1 grouped chapters by contiguous RUNS of an upload batch, and its pile
# builds (heading/separator/single) recorded no page-to-chapter mapping at all.

def _v1_page(page_id, batch, *, status=P.STATUS_OK, text="본문입니다.", join=""):
    seq = int(page_id[0], 16)
    return {"id": page_id, "seq": seq, "file": f"page-{seq:04d}.jpg", "batch": batch,
            "status": status, "text": text, "join_prev": join, "join_prev_source": "",
            "join_glue": "space", "join_reason": "", "added_at": f"2026-01-0{seq}"}


def _write_v1(pages, batches, build=None):
    P.pages_file(PID).write_text(json.dumps({
        "version": 1, "next_seq": len(pages) + 1, "pages": pages, "batches": batches,
        "build": build, "totals": {"cost_usd": 0.0}}), encoding="utf-8")


def test_v1_runs_become_boxes_in_first_appearance_order(project):
    _write_v1(
        [_v1_page("1aaaaaaa", "11111111"), _v1_page("2aaaaaaa", "11111111"),
         _v1_page("3aaaaaaa", "22222222"), _v1_page("4aaaaaaa", "11111111")],
        [{"id": "11111111", "label": "1장"}, {"id": "22222222", "label": ""},
         {"id": "33333333", "label": "empty"}])
    doc = P.load_pages(PID)

    boxes = doc["batches"]
    assert len(boxes) == 3, "three runs, and the empty batch makes no box"
    assert [b["id"] for b in boxes[:2]] == ["11111111", "22222222"]
    assert P.is_box_id(boxes[2]["id"]) and boxes[2]["label"] == "1장", \
        "the second run of a batch is its own box, carrying the batch's title"
    assert [p["batch"] for p in doc["pages"]] == [b["id"] for b in
                                                   (boxes[0], boxes[0], boxes[1], boxes[2])]
    assert doc["version"] == 2


def test_v1_built_pages_keep_their_chapter_and_an_unchecked_one_rejoins_it(project):
    """Version 1 left a still-to-check page out of the middle of its chapter, then made
    it a chapter of its own at the end once accepted. Migrated, it is back inside."""
    _write_v1(
        [_v1_page("1aaaaaaa", "11111111"), _v1_page("2aaaaaaa", "11111111"),
         _v1_page("3aaaaaaa", "22222222"),
         _v1_page("4aaaaaaa", "22222222", status=P.STATUS_NEEDS_CHECK),
         _v1_page("5aaaaaaa", "22222222")],
        [{"id": "11111111", "label": ""}, {"id": "22222222", "label": ""}],
        {"mode": "batch", "chapters": 2,
         "page_map": {"1": ["1aaaaaaa", "2aaaaaaa"], "2": ["3aaaaaaa", "5aaaaaaa"]},
         "used_page_ids": ["1aaaaaaa", "2aaaaaaa", "3aaaaaaa", "5aaaaaaa"]})
    doc = P.load_pages(PID)

    one, two = doc["batches"]
    assert (one["chapter"], two["chapter"]) == (1, 2)
    assert _order(doc) == ["1aaaaaaa", "2aaaaaaa", "3aaaaaaa", "4aaaaaaa", "5aaaaaaa"]
    assert {p["id"]: p["batch"] for p in doc["pages"]}["4aaaaaaa"] == two["id"]
    page = P.find_page(doc, "2aaaaaaa")
    assert (page["join_prev"], page["join_prev_source"]) == ("paragraph", "migrated"), \
        "version 1 built an undecided seam as a paragraph; keeping it leaves the " \
        "chapter's text exactly as translated"
    assert _row(doc, two["id"])["state"] == "changed-waiting", "it waits for the check"


def test_v1_whole_novel_split_becomes_a_locked_legacy_box(project):
    _write_v1(
        [_v1_page("1aaaaaaa", "11111111"), _v1_page("2aaaaaaa", "11111111"),
         _v1_page("3aaaaaaa", "22222222", status=P.STATUS_NEW)],
        [{"id": "11111111", "label": ""}, {"id": "22222222", "label": ""}],
        {"mode": "heading", "chapters": 3, "page_map": {"*": ["1aaaaaaa", "2aaaaaaa"]},
         "used_page_ids": ["1aaaaaaa", "2aaaaaaa"]})
    doc = P.load_pages(PID)

    legacy, new = P.summary(doc)["batches"]
    assert legacy["legacy"] is True and legacy["state"] == "legacy"
    assert legacy["number"] is None and legacy["chapters"] == [1, 3]
    assert new["number"] == 4, "the next box follows every chapter the pile built"
    _refused(409, _add, doc, batch=legacy["id"], digest="x")
    _refused(409, P.move_pages, doc, ["1aaaaaaa"], new["id"])


def test_v1_migration_gives_the_same_ids_on_every_unsaved_load(project):
    """Nothing is saved by a read, so the migration runs again on every load until the
    next save. A client holding one load's box id must still find it in the next."""
    _write_v1([_v1_page("1aaaaaaa", "b1"), _v1_page("2aaaaaaa", "b2"),
               _v1_page("3aaaaaaa", "b1")],
              [{"id": "b1", "label": ""}, {"id": "b2", "label": ""}])
    raw = P.pages_file(PID).read_bytes()
    first, second = P.load_pages(PID), P.load_pages(PID)
    assert [b["id"] for b in first["batches"]] == [b["id"] for b in second["batches"]]
    assert all(P.is_box_id(b["id"]) for b in first["batches"])
    assert P.pages_file(PID).read_bytes() == raw, "a read writes nothing"


# ---- state, numbers and blockers ---------------------------------------------

def _built(doc, box_id, chapter):
    box = P.find_box(doc, box_id)
    box["chapter"] = chapter
    box["built_sig"] = P.box_signature(box, [p for p in doc["pages"] if p["batch"] == box_id])


def test_box_state_and_projected_number():
    doc = P.new_doc()
    one, _ = _box_of(doc, 1, prefix="one")
    _built(doc, one, 1)
    doc["build"] = {"chapters": 1}
    unread = _box(doc)
    _add(doc, batch=unread, digest="u")
    reading = _box(doc)
    _add(doc, batch=reading, digest="r", status=P.STATUS_RUNNING)
    check = _box(doc)
    _add(doc, batch=check, digest="c", text="본문", status=P.STATUS_NEEDS_CHECK)
    ready, _ = _box_of(doc, 1, prefix="ready")
    empty = _box(doc)
    P.normalize(doc)

    rows = P.summary(doc)["batches"]
    assert [r["state"] for r in rows] == ["built", "unread", "reading", "to-check",
                                          "ready", "empty"]
    assert [r["number"] for r in rows] == [1, 2, 3, 4, 5, 6], \
        "an unbuilt box is numbered on from the last built chapter"
    assert P.summary(doc)["chapter_count"] == 1
    assert rows[-1]["id"] == empty


CHANGES = {
    "text": lambda doc, a, b: a[1].update(text="고친 본문입니다."),
    "reorder": lambda doc, a, b: P.move_pages(doc, [a[2]["id"]], a[0]["batch"],
                                              before=a[0]["id"]),
    "photo moved in": lambda doc, a, b: P.move_pages(doc, [b[0]["id"]], a[0]["batch"]),
    "photo moved out": lambda doc, a, b: P.move_pages(doc, [a[2]["id"]], b[0]["batch"]),
    "skip": lambda doc, a, b: a[1].update(status=P.STATUS_SKIPPED),
    "join": lambda doc, a, b: _join(doc, a[1], "gap"),
    "title": lambda doc, a, b: P.rename_box(doc, a[0]["batch"], "제1화: 시작"),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_a_built_box_turns_changed_on_any_content_change(change):
    doc = P.new_doc()
    a, a_pages = _box_of(doc, 3, prefix="a")
    _b, b_pages = _box_of(doc, 1, prefix="b")
    _built(doc, a, 1)
    assert _row(doc, a)["state"] == "built"

    CHANGES[change](doc, a_pages, b_pages)
    P.normalize(doc)
    assert _row(doc, a)["state"] == "changed"


def test_a_status_change_leaves_a_built_box_built():
    """Marking a page "Looks good" after the build doesn't change a word of it."""
    doc = P.new_doc()
    a, pages = _box_of(doc, 2)
    _built(doc, a, 1)
    pages[0]["status"] = P.STATUS_EDITED
    assert _row(doc, a)["state"] == "built"


def test_blockers_name_every_page_that_holds_a_box_back():
    doc = P.new_doc()
    box = _box(doc)
    for n, (status, text) in enumerate([
            (P.STATUS_NEW, ""), (P.STATUS_QUEUED, ""), (P.STATUS_FAILED, ""),
            (P.STATUS_OK, "​​"), (P.STATUS_NEEDS_CHECK, "본문"),
            (P.STATUS_OK, "본문"), (P.STATUS_SKIPPED, "")], start=1):
        _add(doc, batch=box, digest=str(n), name=f"IMG_{n}.jpg", text=text, status=status)

    blockers = _row(doc, box)["blockers"]
    assert [b["kind"] for b in blockers] == ["reading", "unread", "failed", "no-text",
                                             "to-check"], "most fundamental first"
    by_kind = {b["kind"]: b for b in blockers}
    assert by_kind["no-text"]["message"] == \
        "IMG_4.jpg has no text. If it's a picture or a blank page, mark it Skip.", \
        "a page of nothing but invisible characters has no text"
    assert by_kind["failed"]["message"] == \
        "IMG_3.jpg couldn't be read. Read it again, type its text, or Skip it."
    assert "needs checking" in by_kind["to-check"]["message"]
    assert all(b["count"] == len(b["page_ids"]) == 1 for b in blockers)

    loose = [b["kind"] for b in P.box_blockers(P.find_box(doc, box), [
        p for p in doc["pages"] if p["batch"] == box], include="all")]
    assert "to-check" not in loose, "including photos to check lets them through"
    assert "unread" in loose, "but an unread photo never builds"


def test_a_box_of_nothing_but_skipped_photos_says_so():
    doc = P.new_doc()
    box = _box(doc)
    _add(doc, batch=box, status=P.STATUS_SKIPPED)
    row = _row(doc, box)
    assert row["state"] == "skipped"
    assert [b["kind"] for b in row["blockers"]] == ["all-skipped"]


# ---- serving images back -----------------------------------------------------

def test_resolve_rejects_a_malformed_page_id(project):
    for bad in ("", "../../etc", "zzzz", "a" * 40, "../pages.json"):
        assert P.resolve_page_file(PID, bad) is None, f"{bad!r} must not resolve"


def test_resolve_returns_the_real_file(project):
    doc = P.new_doc()
    page = _add(doc)
    (project / P.PAGES_DIRNAME / page["file"]).write_bytes(JPEG)
    P.save_pages(PID, doc)

    resolved = P.resolve_page_file(PID, page["id"])
    assert resolved is not None and resolved.name == page["file"]


def test_resolve_refuses_a_manifest_pointing_outside_the_pages_folder(project):
    """Belt-and-braces against a hand-edited or tampered manifest."""
    doc = P.new_doc()
    page = _add(doc)
    page["file"] = "../project.json"
    (project / "project.json").write_text("{}", encoding="utf-8")
    P.save_pages(PID, doc)
    assert P.resolve_page_file(PID, page["id"]) is None


def test_resolve_returns_none_when_the_file_is_gone(project):
    doc = P.new_doc()
    page = _add(doc)
    P.save_pages(PID, doc)
    assert P.resolve_page_file(PID, page["id"]) is None, "a listed but missing file is not served"


# ---- durability --------------------------------------------------------------

def test_missing_manifest_reads_as_empty(project):
    doc = P.load_pages(PID)
    assert doc["pages"] == [] and doc["next_seq"] == 1


def test_corrupt_manifest_reads_as_empty_instead_of_raising(project):
    P.pages_file(PID).write_text("{not json at all", encoding="utf-8")
    doc = P.load_pages(PID)
    assert doc["pages"] == [], "a truncated manifest must not take down the novel"


def test_manifest_round_trips_korean_text(project):
    doc = P.new_doc()
    page = _add(doc)
    page["text"] = "그는 천천히 문을 열었다."
    P.save_pages(PID, doc)

    reloaded = P.load_pages(PID)
    assert P.find_page(reloaded, page["id"])["text"] == "그는 천천히 문을 열었다."
    raw = P.pages_file(PID).read_text(encoding="utf-8")
    assert "그는" in raw, "Korean is stored readably, not escaped"


def test_next_seq_is_recovered_from_the_pages_when_absent(project):
    """An older or hand-edited manifest must not restart numbering at 1."""
    P.pages_file(PID).write_text(
        json.dumps({"pages": [{"id": "aaaaaaaa", "seq": 7, "file": "page-0007.jpg"}]}),
        encoding="utf-8")
    assert P.load_pages(PID)["next_seq"] == 8


def test_mutate_pages_persists(project):
    with P.mutate_pages(PID) as doc:
        _add(doc, digest="x")
    assert len(P.load_pages(PID)["pages"]) == 1


def test_every_save_bumps_rev(project):
    """The client keeps whichever answer carries the newest rev, which is what stops a
    slow poll putting an old order back on screen after a move."""
    with P.mutate_pages(PID) as doc:
        _add(doc, digest="x")
    assert P.load_pages(PID)["rev"] == 1
    with P.mutate_pages(PID) as doc:
        pass
    assert doc["rev"] == 2 and P.load_pages(PID)["rev"] == 2


def test_a_refused_change_inside_mutate_pages_is_not_saved(project):
    with P.mutate_pages(PID) as doc:
        box = _box(doc)
    with pytest.raises(P.BoxError):
        with P.mutate_pages(PID) as doc:
            P.rename_box(doc, box, "새 제목")
            P.move_pages(doc, ["deadbeef"], box)
    reloaded = P.load_pages(PID)
    assert reloaded["rev"] == 1 and P.find_box(reloaded, box)["label"] == ""


# ---- payload shaping ---------------------------------------------------------

def test_summary_omits_page_text():
    """400 pages of Korean would be several MB on every tab switch."""
    doc = P.new_doc()
    page = _add(doc)
    page["text"] = "그는 천천히 문을 열었다."
    out = P.summary(doc)
    row = out["pages"][0]
    assert "text" not in row and "raw_text" not in row
    assert row["chars"] == len(page["text"]), "the length still reaches the UI"
    assert row["has_text"] is True
    assert out["rev"] == 0 and out["chapter_count"] == 0


def test_counts_report_every_status():
    doc = P.new_doc()
    _add(doc, digest="a")
    _add(doc, digest="b")["status"] = P.STATUS_NEEDS_CHECK
    out = P.counts(doc)
    assert out["total"] == 2
    assert out[P.STATUS_NEW] == 1 and out[P.STATUS_NEEDS_CHECK] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

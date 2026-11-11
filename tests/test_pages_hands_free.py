"""Hands-free chapter boxes: the ones the phone, a share or Telegram fill.

What has to hold:
- A box still being photographed never builds. Not by hands-free, not by the desktop's
  Build, not by Start over, not when photos marked to check are let through. This is
  the one guarantee that half a chapter is never built.
- A desktop box is untouched: none of the new fields, and its row as it was.
- Creating a box with an origin it already has returns that box, so a phone resending
  after a lost answer never makes a second chapter.
- Finishing is safe to repeat and refuses only what can't be finished; a photo that
  arrives afterwards opens the box again.
- Splitting a phone chapter gives two phone chapters, but not while it is still being
  photographed: its later photos would land in the wrong half.

Runs under pytest (``pytest tests/``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.pages as P  # noqa: E402
from server import ocr_build  # noqa: E402

ORIGIN = "phone:c3f0a1b2"


def _doc():
    return P.new_doc()


def _photo(doc, box_id, n, *, status=P.STATUS_OK):
    page = P.add_page(doc, ext="jpg", data_len=10, digest=f"{box_id}-{n}", batch=box_id)
    page["text"] = f"{n}쪽 본문입니다."
    page["status"] = status
    return page


def _phone_box(doc, origin=ORIGIN, photos=2, captured=False):
    box = P.create_box(doc, origin=origin, hands_free=True)
    for n in range(1, photos + 1):
        _photo(doc, box["id"], n)
    if captured:
        P.set_captured(doc, box["id"])
    return box


def _row(doc, box_id):
    return next(row for row in P.summary(doc)["batches"] if row["id"] == box_id)


def _refused(status, change, *args, **kwargs):
    with pytest.raises(P.BoxError) as refusal:
        change(*args, **kwargs)
    assert refusal.value.status == status, refusal.value.message
    return refusal.value.message


# ---- a desktop box is untouched ---------------------------------------------------------

def test_a_desktop_box_has_none_of_the_new_fields():
    doc = _doc()
    box = P.create_box(doc, "Prologue")
    _photo(doc, box["id"], 1)
    P.normalize(doc)
    assert not {"hands_free", "origin", "captured_at", "notified", "expect_number"} & set(box)
    assert not {"hands_free", "origin", "captured_at", "expect_number"} & set(_row(doc, box["id"]))
    assert P.set_captured(doc, box["id"]) is box and "captured_at" not in box, \
        "a box that was never waiting for Done is left as it is"


# ---- creating ---------------------------------------------------------------------------

def test_an_origin_already_here_returns_its_box():
    doc = _doc()
    first = P.create_box(doc, origin=ORIGIN, hands_free=True)
    again = P.create_box(doc, origin=ORIGIN, hands_free=True)
    assert again is first
    assert len(doc["batches"]) == 1, "a resent create must not make a second chapter"
    assert first["hands_free"] is True and first["origin"] == ORIGIN


def test_an_origin_that_cant_be_used_is_refused():
    doc = _doc()
    _refused(400, P.create_box, doc, origin="phone:../../x", hands_free=True)
    _refused(400, P.create_box, doc, origin="p" * 65, hands_free=True)
    assert doc["batches"] == []


def test_a_hands_free_row_carries_its_fields():
    doc = _doc()
    box = _phone_box(doc)
    row = _row(doc, box["id"])
    assert row["hands_free"] is True
    assert row["origin"] == ORIGIN
    assert row["captured_at"] is None
    assert row["state"] == "capturing"


# ---- the capturing blocker ----------------------------------------------------------------

def test_a_box_being_photographed_is_blocked_first_whatever_is_included():
    doc = _doc()
    box = _phone_box(doc)
    pages = [p for p in doc["pages"] if p["batch"] == box["id"]]
    for include in ("approved", "all"):
        blockers = P.box_blockers(box, pages, include=include)
        assert blockers[0]["kind"] == "capturing"
        assert blockers[0]["message"] == P.CAPTURING


def test_an_empty_box_being_photographed_is_capturing_not_empty():
    doc = _doc()
    box = P.create_box(doc, origin=ORIGIN, hands_free=True)
    assert [b["kind"] for b in P.box_blockers(box, [])] == ["capturing"]
    assert _row(doc, box["id"])["state"] == "capturing"


def test_page_problems_still_show_while_photographing():
    doc = _doc()
    box = _phone_box(doc, photos=1)
    _photo(doc, box["id"], 2, status=P.STATUS_FAILED)
    pages = [p for p in doc["pages"] if p["batch"] == box["id"]]
    assert [b["kind"] for b in P.box_blockers(box, pages)] == ["capturing", "failed"], \
        "a bad photo is worth knowing about while it can still be shot again"


@pytest.mark.parametrize("include", ["approved", "all"])
def test_no_build_takes_a_box_still_being_photographed(include):
    doc = _doc()
    capturing = _phone_box(doc)
    later = _phone_box(doc, origin="phone:later", captured=True)
    result = ocr_build.build_boxes(doc, [], include=include)
    assert result.added == []
    waits = {w["batch"]: w for w in result.waiting}
    assert waits[capturing["id"]]["reasons"][0] == P.CAPTURING
    assert waits[later["id"]]["blocked_by"] == 1, "a later chapter waits for an earlier one"
    assert capturing.get("chapter") is None


def test_start_over_keeps_an_empty_box_being_photographed_and_stops_at_it():
    doc = _doc()
    done = _phone_box(doc, origin="phone:done", captured=True)
    empty = P.create_box(doc, origin=ORIGIN, hands_free=True)
    result = ocr_build.build_boxes(doc, [], append=False)
    assert P.find_box(doc, empty["id"]) is not None, "its photos are on their way"
    assert result.added == [1] and done["chapter"] == 1
    assert [w["batch"] for w in result.waiting] == [empty["id"]]


def test_a_finished_box_builds():
    doc = _doc()
    box = _phone_box(doc, captured=True)
    result = ocr_build.build_boxes(doc, [])
    assert result.added == [1] and box["chapter"] == 1


# ---- finishing and reopening ----------------------------------------------------------------

def test_finishing_is_safe_to_repeat_and_keeps_the_first_time():
    doc = _doc()
    box = _phone_box(doc)
    P.set_captured(doc, box["id"], expect_number=3)
    first = box["captured_at"]
    P.set_captured(doc, box["id"])
    assert box["captured_at"] == first
    assert box["expect_number"] == 3
    assert _row(doc, box["id"])["state"] == "ready"


def test_finishing_refuses_what_cant_be_finished():
    doc = _doc()
    assert _refused(404, P.set_captured, doc, "deadbeef") == P.BOX_GONE
    empty = P.create_box(doc, origin=ORIGIN, hands_free=True)
    _refused(409, P.set_captured, doc, empty["id"])
    assert "captured_at" not in empty


def test_a_photo_after_finishing_opens_the_box_again():
    doc = _doc()
    box = _phone_box(doc, captured=True)
    P.reopen_box(box)
    assert "captured_at" not in box
    assert _row(doc, box["id"])["state"] == "capturing"
    desktop = P.create_box(doc)
    P.reopen_box(desktop)
    assert "captured_at" not in desktop and "hands_free" not in desktop


# ---- splitting ------------------------------------------------------------------------------------

def test_splitting_a_finished_phone_chapter_gives_two_phone_chapters():
    doc = _doc()
    box = _phone_box(doc, photos=3, captured=True)
    tail = [p["id"] for p in doc["pages"] if p["batch"] == box["id"]][2:]
    new = P.create_box(doc, after=box["id"], ids=tail)
    assert new["hands_free"] is True
    assert new["captured_at"] == box["captured_at"]
    assert "origin" not in new, "an origin names one box"


def test_a_chapter_still_being_photographed_cant_be_split():
    doc = _doc()
    box = _phone_box(doc, photos=3)
    tail = [p["id"] for p in doc["pages"] if p["batch"] == box["id"]][2:]
    _refused(409, P.create_box, doc, after=box["id"], ids=tail)
    assert len(doc["batches"]) == 1, "a refused split leaves no box behind"


def test_splitting_a_desktop_chapter_makes_a_desktop_box():
    doc = _doc()
    box = P.create_box(doc)
    for n in range(1, 4):
        _photo(doc, box["id"], n)
    tail = [p["id"] for p in doc["pages"]][2:]
    new = P.create_box(doc, after=box["id"], ids=tail)
    assert "hands_free" not in new


# ---- keeping the fields straight ----------------------------------------------------------

def test_normalize_keeps_only_well_formed_fields():
    doc = _doc()
    box = _phone_box(doc, captured=True)
    box["notified"] = ["a:needs-you:1", 7, "a:needs-you:1", "b:failed:2"]
    box["expect_number"] = 0
    box["origin"] = "bad origin!"
    P.normalize(doc)
    assert box["notified"] == ["a:needs-you:1", "b:failed:2"]
    assert "expect_number" not in box and "origin" not in box
    assert box["captured_at"]

    box["captured_at"] = ""
    P.normalize(doc)
    assert "captured_at" not in box, "an empty time is no time: the box is still open"

    stray = P.create_box(doc)
    stray.update({"hands_free": "yes", "captured_at": "x", "notified": ["k"], "origin": ORIGIN})
    P.normalize(doc)
    assert not {"hands_free", "captured_at", "notified", "origin"} & set(stray), \
        "only a box written as hands-free is one"


def test_normalize_is_idempotent_with_hands_free_boxes():
    doc = _doc()
    _phone_box(doc, captured=True)["notified"] = ["k:1"]
    _phone_box(doc, origin="phone:two")
    once = P.normalize(doc)
    import copy
    snapshot = copy.deepcopy(once)
    assert P.normalize(doc) == snapshot

"""The hands-free rules, without the app: what is read, what holds a chapter, what the
phone is told.

What has to hold:
- Every photo is read as soon as it is there, the newest too: a bad photo is known about
  while the chapter can still be shot again. A photo that failed is read once more and
  only once; an unsure reading is double-checked once per reading, and so is a confident
  reading of a photo the quick look thought should be taken again.
- Nothing in a desktop box is ever touched.
- The automatic build holds a chapter a person should look at first: one sent from the
  computer, one with a chapter heading part way through, one changed after or while it
  was translated, one that would build under another number than the phone showed. A
  held chapter holds every chapter after it, and a manual build never asks.
- Only a finished, unchanged, untranslated chapter that was never tried is translated,
  and it is checked again just before it starts.
- The phone is told about each problem once, by photo position, and never given a link.

Runs under pytest (``pytest tests/``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.pages as P  # noqa: E402
from server import hands_free as H  # noqa: E402
from server import ocr_build  # noqa: E402
from translation_bot.text_source import make_chapter  # noqa: E402

ALWAYS = staticmethod(lambda page: True)


def _doc():
    return P.new_doc()


def _photo(doc, box_id, n, *, status=P.STATUS_OK, text=None, **fields):
    page = P.add_page(doc, ext="jpg", data_len=10, digest=f"{box_id}-{n}", batch=box_id)
    page["text"] = f"{n}쪽 본문입니다. 이야기가 계속된다." if text is None else text
    page["status"] = status
    page.update(fields)
    return page


def _phone(doc, photos=3, *, captured=True, origin=None, **fields):
    box = P.create_box(doc, origin=origin or f"phone:{len(doc['batches']) + 1:08d}",
                       hands_free=True)
    pages = [_photo(doc, box["id"], n, **fields) for n in range(1, photos + 1)]
    if captured:
        P.set_captured(doc, box["id"])
    return box, pages


def _never(_k):
    return False


def _hold(has_english=_never, translating=frozenset()):
    return lambda box, pages, number: H.auto_hold(box, pages, number, has_english=has_english,
                                                  translating=set(translating))


# ---- page work -----------------------------------------------------------------------------------

def test_every_new_photo_is_read_at_once_the_newest_too():
    doc = _doc()
    box, pages = _phone(doc, photos=3, captured=False, status=P.STATUS_NEW, text="")
    work = H.page_work(doc, image_exists=lambda p: True)
    assert [p["id"] for p in work.reads] == [p["id"] for p in pages]


def test_a_photo_with_no_image_is_not_read():
    doc = _doc()
    _box, pages = _phone(doc, photos=2, status=P.STATUS_NEW, text="")
    work = H.page_work(doc, image_exists=lambda p: p["id"] != pages[1]["id"])
    assert [p["id"] for p in work.reads] == [pages[0]["id"]]


def test_a_failed_photo_is_read_once_more_and_only_once():
    doc = _doc()
    _box, pages = _phone(doc, photos=2, status=P.STATUS_FAILED, text="")
    pages[1]["auto_retried"] = True
    work = H.page_work(doc, image_exists=lambda p: True)
    assert [p["id"] for p in work.retries] == [pages[0]["id"]]


def test_an_unsure_reading_is_double_checked_once():
    doc = _doc()
    _box, pages = _phone(doc, photos=3, status=P.STATUS_NEEDS_CHECK)
    pages[1]["verify"] = {"at": "t", "verdict": "discrepancies", "issues": []}
    pages[2]["text"] = ""   # nothing to check yet
    work = H.page_work(doc, image_exists=lambda p: True)
    assert [p["id"] for p in work.verifies] == [pages[0]["id"]]


def test_a_confident_reading_the_quick_look_doubted_is_double_checked():
    doc = _doc()
    _box, pages = _phone(doc, photos=2)
    pages[0]["look"] = {"verdict": "retake", "why": ["blurry"], "note": "blurry", "at": "t"}
    pages[1]["look"] = {"verdict": "ok", "why": [], "note": "", "at": "t"}
    work = H.page_work(doc, image_exists=lambda p: True)
    assert [p["id"] for p in work.verifies] == [pages[0]["id"]]
    pages[0]["verify"] = {"at": "t2", "verdict": "ok", "issues": []}
    assert H.page_work(doc, image_exists=lambda p: True).verifies == []


def test_a_desktop_box_is_never_touched():
    doc = _doc()
    box = P.create_box(doc)
    _photo(doc, box["id"], 1, status=P.STATUS_NEW, text="")
    _photo(doc, box["id"], 2, status=P.STATUS_FAILED, text="")
    _photo(doc, box["id"], 3, status=P.STATUS_NEEDS_CHECK)
    work = H.page_work(doc, image_exists=lambda p: True)
    assert work.reads == work.retries == work.verifies == []


# ---- holds ------------------------------------------------------------------------------------------

def test_a_heading_part_way_through_holds_the_chapter():
    doc = _doc()
    box, pages = _phone(doc, photos=3)
    pages[2]["heading"] = "제13화"
    reason = H.auto_hold(box, pages, 1, has_english=_never, translating=set())
    assert reason.startswith("Photo 3 opens with a chapter heading")


def test_a_join_the_reader_set_lets_it_through():
    doc = _doc()
    box, pages = _phone(doc, photos=3)
    pages[2]["heading"] = "제13화"
    pages[2].update(join_prev="paragraph", join_prev_source="user")
    assert H.auto_hold(box, pages, 1, has_english=_never, translating=set()) is None


def test_a_box_from_the_computer_is_held():
    doc = _doc()
    box = P.create_box(doc)
    pages = [_photo(doc, box["id"], 1)]
    assert H.auto_hold(box, pages, 1, has_english=_never, translating=set()) == H.NOT_FROM_PHONE


def test_a_changed_chapter_with_english_or_being_translated_is_held():
    doc = _doc()
    box, pages = _phone(doc)
    box["chapter"] = 4
    assert "after it was translated" in H.auto_hold(box, pages, 4, has_english=lambda k: k == 4,
                                                     translating=set())
    assert "while it was being translated" in H.auto_hold(box, pages, 4, has_english=_never,
                                                           translating={4})


def test_a_chapter_that_would_build_under_another_number_is_held():
    doc = _doc()
    box, pages = _phone(doc)
    box["expect_number"] = 12
    assert "Sent as chapter 12, but it would build as chapter 11" in H.auto_hold(
        box, pages, 11, has_english=_never, translating=set())
    assert H.auto_hold(box, pages, 12, has_english=_never, translating=set()) is None


def test_a_held_box_holds_every_box_after_it():
    doc = _doc()
    first, pages = _phone(doc)
    pages[1]["heading"] = "제2화"
    second, _ = _phone(doc)
    res = ocr_build.build_boxes(doc, [], hold=_hold())
    assert res.added == []
    waits = {w["batch"]: w for w in res.waiting}
    assert waits[first["id"]]["held"] is True
    assert waits[second["id"]]["blocked_by"] == 1


def test_a_held_changed_chapter_is_not_rewritten():
    doc = _doc()
    box, pages = _phone(doc)
    built = ocr_build.build_boxes(doc, [])
    sig = box["built_sig"]
    pages[0]["text"] = "고친 본문입니다."
    res = ocr_build.build_boxes(doc, built.chapters, hold=_hold(has_english=lambda k: True))
    assert res.refreshed == res.updated == []
    assert box["built_sig"] == sig
    assert res.waiting[0]["held"] and "after it was translated" in res.waiting[0]["reasons"][0]


def test_the_manual_build_never_asks():
    doc = _doc()
    box = P.create_box(doc)
    _photo(doc, box["id"], 1)
    assert ocr_build.build_boxes(doc, []).added == [1]


def test_no_hold_builds_exactly_as_before():
    plain, held = _doc(), _doc()
    for doc in (plain, held):
        _phone(doc)
        _phone(doc)
    a = ocr_build.build_boxes(plain, [])
    b = ocr_build.build_boxes(held, [], hold=_hold())
    assert a.added == b.added == [1, 2]
    assert [c.paragraphs for c in a.chapters] == [c.paragraphs for c in b.chapters]


# ---- translating ---------------------------------------------------------------------------------------

def _built(doc):
    res = ocr_build.build_boxes(doc, [])
    return {c.index: c for c in res.chapters}


def _translatable(doc, chapters, has_english=_never, state_of=lambda k: None):
    return H.to_translate(doc, chapters=chapters, is_korean=lambda c: True,
                          has_english=has_english, state_of=state_of)


def test_a_finished_unchanged_chapter_never_tried_is_translated():
    doc = _doc()
    _phone(doc)
    chapters = _built(doc)
    assert _translatable(doc, chapters) == [1]
    assert _translatable(doc, chapters, has_english=lambda k: True) == []
    assert _translatable(doc, chapters, state_of=lambda k: "failed") == [], \
        "trying again by itself could spend the plan on the same failure over and over"
    assert _translatable(doc, chapters, state_of=lambda k: "pending") == [1]


def test_a_reopened_or_changed_chapter_waits():
    doc = _doc()
    box, pages = _phone(doc)
    chapters = _built(doc)
    P.reopen_box(box)
    assert _translatable(doc, chapters) == [] and not H.still_translatable(doc, 1)
    P.set_captured(doc, box["id"])
    pages[0]["text"] = "고친 본문입니다."
    assert _translatable(doc, chapters) == [] and not H.still_translatable(doc, 1)


def test_a_desktop_chapter_is_never_translated_by_itself():
    doc = _doc()
    box = P.create_box(doc)
    _photo(doc, box["id"], 1)
    chapters = _built(doc)
    assert _translatable(doc, chapters) == [] and not H.still_translatable(doc, 1)


def test_worth_building_only_for_a_finished_phone_chapter_not_built_as_it_is():
    doc = _doc()
    box, pages = _phone(doc, captured=False)
    assert not H.worth_building(doc)
    P.set_captured(doc, box["id"])
    assert H.worth_building(doc)
    _built(doc)
    assert not H.worth_building(doc)
    pages[0]["text"] = "고친 본문입니다."
    assert H.worth_building(doc)


# ---- telling the phone -----------------------------------------------------------------------------------

def _needs(doc, hold=None):
    preview = ocr_build.build_boxes(doc, [], hold=hold or _hold())
    return H.needs_reader(doc, preview.waiting, image_exists=lambda p: True)


def test_page_problems_are_named_by_position():
    doc = _doc()
    _box, pages = _phone(doc, photos=4, captured=False)
    pages[1].update(status=P.STATUS_NEEDS_CHECK, verify={"at": "v1", "verdict": "discrepancies"})
    pages[3].update(status=P.STATUS_FAILED, auto_retried=True, text="")
    held = _needs(doc)
    assert [(h.photo, h.kind) for h in held] == [(2, "to-check"), (4, "failed")], \
        "known about while the chapter can still be shot again"
    assert held[0].key == f"{pages[1]['id']}:to-check:v1"


def test_a_page_still_on_its_way_is_not_a_problem():
    doc = _doc()
    _box, pages = _phone(doc, photos=2)
    pages[0].update(status=P.STATUS_NEEDS_CHECK, verify=None)   # its double-check is coming
    pages[1].update(status=P.STATUS_FAILED, text="")             # its retry is coming
    assert [h for h in _needs(doc) if h.photo] == []


def test_a_chapter_waiting_for_one_never_finished_says_so():
    doc = _doc()
    _phone(doc, captured=False)
    later, _ = _phone(doc)
    after = [h for h in _needs(doc) if h.kind == "after"]
    assert after and after[0].box == later["id"]
    assert after[0].message == ("Chapter 2 is waiting for chapter 1, which was never "
                                "finished. Tap Done on it, or delete it.")


def test_a_chapter_waiting_for_one_from_the_computer_says_so():
    doc = _doc()
    desktop = P.create_box(doc)
    _photo(doc, desktop["id"], 1)
    _phone(doc)
    after = [h for h in _needs(doc) if h.kind == "after"]
    assert after[0].message.endswith("which was added on the computer. Build it there.")


def test_a_heading_part_way_through_is_on_its_photo():
    doc = _doc()
    box, pages = _phone(doc)
    pages[2]["heading"] = "제2화"
    [held] = [h for h in _needs(doc) if h.kind == "heading"]
    assert held.message.startswith("Photo 3 opens with a chapter heading")
    assert (held.page_id, held.photo) == (pages[2]["id"], 3), \
        "the phone offers Split here and Keep together on that photo"
    title, body = H.notice_needs_you("N", [held])
    assert "Photo 3 opens with a chapter heading" in body


def test_a_held_finished_chapter_says_why():
    doc = _doc()
    box, pages = _phone(doc)
    box["expect_number"] = 7
    holds = [h for h in _needs(doc) if h.kind == "hold"]
    assert holds and holds[0].message.startswith("Sent as chapter 7, but it would build as chapter 1")


def test_each_problem_is_told_once():
    doc = _doc()
    _box, pages = _phone(doc, photos=2)
    pages[0].update(status=P.STATUS_NEEDS_CHECK, verify={"at": "v1"})
    held = _needs(doc)
    assert H.untold(doc, held) == held
    H.mark_told(doc, held)
    P.normalize(doc)
    assert H.untold(doc, _needs(doc)) == []
    pages[0]["verify"] = {"at": "v2"}   # read and checked again, still unsure
    assert [h.key for h in H.untold(doc, _needs(doc))] == [f"{pages[0]['id']}:to-check:v2"]


def test_the_notice_names_photos_and_carries_no_link():
    doc = _doc()
    _box, pages = _phone(doc, photos=12)
    for i in (3, 8):
        pages[i].update(status=P.STATUS_NEEDS_CHECK, verify={"at": "v"})
    pages[11].update(status=P.STATUS_FAILED, auto_retried=True, text="")
    title, body = H.notice_needs_you("달빛 소설", _needs(doc))
    assert title == "Chapter 1 needs you"
    assert "photos 4 and 9 need checking" in body and "photo 12 couldn't be read" in body
    assert body.startswith("달빛 소설: ")
    assert "http" not in body and "?k=" not in body
    assert title.isascii()


def test_two_chapters_in_one_notice():
    doc = _doc()
    _b1, first = _phone(doc)
    _b2, second = _phone(doc)
    first[0].update(status=P.STATUS_NEEDS_CHECK, verify={"at": "v"})
    second[1].update(status=P.STATUS_NEEDS_CHECK, verify={"at": "v"})
    title, _body = H.notice_needs_you("Novel", _needs(doc))
    assert title == "2 chapters need you"


@pytest.mark.parametrize("positions,said", [([4], "photo 4"), ([9, 4], "photos 4 and 9"),
                                            ([4, 9, 12], "photos 4, 9 and 12")])
def test_photo_names(positions, said):
    assert H.photo_names(positions) == said


def test_other_notices():
    assert H.notice_translated("N", 3, "제3화")[0] == "Chapter 3 is translated"
    assert "flagged 2 things" in H.notice_translated("N", 3, "제3화", flagged=2)[1]
    assert H.notice_failed("N", 3, "boom\nmore")[1] == "N: boom. Open Night Reader to try again."
    assert "about 15:40" in H.notice_waiting("N", "15:40")[1]
    assert "about 21:10" in H.notice_gave_up("N", "21:10")[1]
    assert H.notice_back("N", 6, 2)[1] == "N: carrying on with 6 photos to read and 2 chapters to translate."


# ---- where each chapter stands ---------------------------------------------------------------------------

def _rows(doc, *, translating=frozenset(), queued=frozenset(), state_of=lambda k: None,
          has_english=_never, chapters=()):
    preview = ocr_build.build_boxes(doc, list(chapters), hold=_hold(has_english))
    held = H.needs_reader(doc, preview.waiting, image_exists=lambda p: True)
    return H.status_rows(doc, preview.waiting, held, translating=set(translating),
                         queued=set(queued), state_of=state_of, has_english=has_english)


def test_status_steps():
    doc = _doc()
    _phone(doc, captured=False)
    assert _rows(doc)[0]["step"] == "capturing"

    doc = _doc()
    _phone(doc, status=P.STATUS_NEW, text="")
    assert _rows(doc)[0]["step"] == "reading"

    doc = _doc()
    _phone(doc, status=P.STATUS_NEEDS_CHECK)
    assert _rows(doc)[0]["step"] == "checking"

    doc = _doc()
    _b, pages = _phone(doc)
    pages[0].update(status=P.STATUS_NEEDS_CHECK, verify={"at": "v"})
    row = _rows(doc)[0]
    assert row["step"] == "needs-you" and row["needs_you"][0]["photo"] == 1

    doc = _doc()
    _phone(doc)
    assert _rows(doc)[0]["step"] == "building"

    doc = _doc()
    _phone(doc)
    chapters = list(_built(doc).values())
    assert _rows(doc, chapters=chapters, queued={1})[0]["step"] == "translating"
    assert _rows(doc, chapters=chapters, has_english=lambda k: True)[0]["step"] == "translated"
    assert _rows(doc, chapters=chapters, state_of=lambda k: "failed")[0]["step"] == "failed"


def test_status_waiting_turn_and_counts():
    doc = _doc()
    _phone(doc, status=P.STATUS_NEEDS_CHECK)
    _phone(doc)
    rows = _rows(doc)
    assert rows[1]["step"] == "waiting-turn"
    assert rows[0]["pages"]["to_check"] == 3 and rows[0]["photos"] == 3


def test_make_chapter_stays_importable():
    # The chapters to_translate looks up are ordinary Chapter objects.
    assert make_chapter(1, "제1화", "본문").index == 1

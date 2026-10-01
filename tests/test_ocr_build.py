"""Tests for turning transcribed pages into chapters.

This is where the page-seam decisions finally land. The failure this guards against
is subtle and pervasive: a sentence that ran across a page break becomes two
paragraphs, the translator faithfully preserves that paragraph structure, and the
English reads wrong at every single page boundary in the novel.

What has to hold:
- A ``sentence`` join introduces NO blank line and no line break.
- A ``paragraph`` join introduces exactly one blank line.
- A ``gap`` is reported rather than silently smoothed over — it is the only place a
  page that was never photographed can surface.
- Each chapter box builds exactly one chapter, in box order, WITHOUT round-tripping
  through the text splitter, because a box may legitimately contain no heading at all.
- A photo that isn't ready holds its box back. It is never left out of the middle of
  its chapter, to come back later as a chapter of its own at the end of the novel.
- Building an append leaves earlier chapter indices — and therefore their existing
  translations — untouched, while a built chapter whose photos changed is rebuilt in
  place.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_ocr_build.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.pages as P  # noqa: E402
from server.ocr_build import assemble_pages, build_boxes  # noqa: E402
from server.pages import STATUS_NEEDS_CHECK, STATUS_NEW, STATUS_OK, STATUS_SKIPPED  # noqa: E402
from translation_bot.docs_extract import Chapter  # noqa: E402

_seq = [0]


def page(text, *, join="paragraph", glue="space", status=STATUS_OK, heading=None, name=""):
    _seq[0] += 1
    return {"id": f"{_seq[0]:08x}", "seq": _seq[0], "text": text, "join_prev": join,
            "join_glue": glue, "join_prev_source": "user" if join else "",
            "join_reason": "", "status": status, "heading": heading, "name": name}


def boxes(*groups, labels=()):
    """A normalized manifest with one chapter box per group of pages, in order.

    Each page's seam is recorded against the page before it in its group, the way the
    editor and stitching record it — normalize would otherwise rightly clear it.
    """
    doc = {"version": 2, "pages": [], "batches": [], "build": None}
    for n, group in enumerate(groups):
        box_id = f"b{n:07x}"
        doc["batches"].append({"id": box_id, "label": labels[n] if n < len(labels) else ""})
        prev = None
        for p in group:
            p["batch"] = box_id
            if p["status"] != STATUS_SKIPPED:
                p["join_prev_id"] = prev
                prev = p["id"]
            doc["pages"].append(p)
    return P.normalize(doc)


def build(doc, existing=(), **kwargs):
    """Build, and record it on the manifest the way the endpoint does."""
    res = build_boxes(doc, list(existing), **kwargs)
    doc["build"] = {"chapters": len(res.chapters), "page_map": res.page_map}
    P.normalize(doc)
    return res


def chapters(n):
    return [Chapter(i, f"Chapter {i}", [f"{i}장의 본문."]) for i in range(1, n + 1)]


# ---- the core case -----------------------------------------------------------

def test_sentence_join_makes_no_paragraph_break():
    """The whole reason this module exists."""
    text, _ = assemble_pages([
        page("그는 천천히 문을"),
        page("열고 안으로 들어갔다.", join="sentence"),
    ])
    assert text == "그는 천천히 문을 열고 안으로 들어갔다."
    assert "\n" not in text, "a page break inside a sentence must leave no line break"


def test_mid_word_sentence_join_inserts_nothing():
    text, _ = assemble_pages([
        page("그는 문을 열"),
        page("고 나갔다.", join="sentence", glue="none"),
    ])
    assert text == "그는 문을 열고 나갔다.", "a split word must rejoin with no space"


def test_paragraph_join_makes_exactly_one_blank_line():
    text, _ = assemble_pages([page("첫 문단이다."), page("둘째 문단이다.")])
    assert text == "첫 문단이다.\n\n둘째 문단이다."


def test_a_gap_is_reported_not_smoothed_over():
    text, warnings = assemble_pages([
        page("그는 문을 열었다."),
        page("배는 이미 항구를 떠난 뒤였다.", join="gap", name="IMG_0042.jpg"),
    ])
    assert len(warnings) == 1, "a missing page must be surfaced to the reader"
    assert "IMG_0042.jpg" in warnings[0], "the warning must say where to look"
    assert "\n\n" in text, "the text is still joined so the novel stays readable"


def test_chapter_join_separates_and_keeps_the_heading():
    text, _ = assemble_pages([
        page("이야기가 끝났다."),
        page("제4화 새로운 아침\n\n해가 떴다.", join="chapter", heading="제4화 새로운 아침"),
    ])
    assert "제4화 새로운 아침" in text
    assert text.count("제4화 새로운 아침") == 1, "the heading must not be duplicated"


def test_chapter_join_restores_a_heading_missing_from_the_page_text():
    text, _ = assemble_pages([
        page("이야기가 끝났다."),
        page("해가 떴다.", join="chapter", heading="제4화 새로운 아침"),
    ])
    assert "제4화 새로운 아침" in text, "a reported heading must reach the built text"


def test_a_heading_inside_a_box_is_warned_about():
    """A box builds one chapter, so a heading in the middle of one is the next
    chapter's photos in the wrong box — or a line that only looks like a heading.
    Either way the reader has to look, and has to be told where."""
    doc = boxes([page("이야기가 끝났다."),
                 page("제4화 새로운 아침\n\n해가 떴다.", join="", name="IMG_9.jpg")])
    res = build(doc)
    assert doc["pages"][1]["join_prev"] == "chapter", "the free rules see the heading"
    assert len(res.chapters) == 1, "it still builds as one chapter"
    assert any(w.startswith("Chapter 1: IMG_9.jpg") and "their own box" in w
               for w in res.warnings)


# ---- what holds a box back ---------------------------------------------------

def test_skipped_pages_are_left_out_without_blocking():
    doc = boxes([page("본문"), page("표지", status=STATUS_SKIPPED)])
    res = build(doc)
    assert res.added == [1] and res.chapters[0].paragraphs == ["본문"], \
        "a cover or blank page is not part of the novel"
    assert res.page_map["1"] == [doc["pages"][0]["id"]]


def test_an_unchecked_page_holds_its_box_back_instead_of_vanishing():
    """It used to be left out of the middle of its chapter, gluing the pages either
    side of it together — then built later as a chapter of its own at the end."""
    doc = boxes([page("그는 문을"), page("확인 안 됨", status=STATUS_NEEDS_CHECK),
                 page("닫았다.")])
    res = build(doc)
    assert res.chapters == [] and res.added == []
    assert res.waiting[0]["number"] == 1
    assert "needs checking" in res.waiting[0]["reasons"][0]


def test_a_page_with_no_text_blocks_its_box():
    doc = boxes([page("본문"), page("   "), page("​")])
    res = build(doc)
    assert res.added == []
    assert res.waiting[0]["reasons"] == [
        "Photo 2 and Photo 3 have no text. If they're pictures or blank pages, mark "
        "them Skip."]


def test_include_all_accepts_unchecked_but_never_unread_pages():
    doc = boxes([page("확인됨"), page("미확인", status=STATUS_NEEDS_CHECK)],
                [page("아직", status=STATUS_NEW)])
    res = build(doc, include="all")
    assert res.added == [1]
    assert res.chapters[0].paragraphs == ["확인됨", "미확인"]
    assert res.waiting[0]["number"] == 2
    assert "hasn't been read" in res.waiting[0]["reasons"][0]


# ---- one box, one chapter ----------------------------------------------------

def test_each_box_builds_one_chapter_in_box_order():
    doc = boxes([page("1쪽"), page("2쪽")], [page("3쪽"), page("4쪽")])
    res = build(doc)
    assert [c.index for c in res.chapters] == [1, 2]
    assert res.added == [1, 2]
    assert len(res.page_map["1"]) == 2, "the map records which photos made each chapter"
    assert [b["chapter"] for b in doc["batches"]] == [1, 2]


def test_a_box_needs_no_heading_or_separator():
    """A photographed chapter often has no heading on any page. Round-tripping
    through the splitter would collapse every box into one chapter."""
    doc = boxes([page("본문만 있는 쪽")], [page("헤딩 없는 쪽")])
    assert len(build(doc).chapters) == 2, "boxes split even with nothing to split on"


def test_the_box_title_becomes_the_chapter_title_punctuation_and_all():
    doc = boxes([page("본문")], labels=["제12화: 끝!"])
    assert build(doc).chapters[0].title == "제12화: 끝!"


def test_a_box_falls_back_to_its_first_photos_heading_for_its_title():
    doc = boxes([page("본문", heading="제3화 문이 열렸다")], [page("본문")])
    res = build(doc)
    assert res.chapters[0].title == "제3화 문이 열렸다"
    assert res.chapters[1].title == "Chapter 2"


def test_sentence_joins_survive_inside_a_box():
    doc = boxes([page("그는 천천히 문을"), page("열었다.", join="sentence")])
    assert build(doc).chapters[0].paragraphs == ["그는 천천히 문을 열었다."], \
        "a seam inside a box must still join seamlessly"


def test_box_membership_not_adjacency_decides_the_chapter():
    """Version 1 grouped chapters by contiguous runs, so one photo dragged in among
    another chapter's photos split that chapter into three."""
    a, b, c = page("가."), page("나."), page("다.")
    a["batch"], b["batch"], c["batch"] = "b0000001", "b0000000", "b0000001"
    doc = P.normalize({"version": 2, "build": None, "pages": [a, b, c],
                       "batches": [{"id": "b0000000"}, {"id": "b0000001"}]})
    res = build(doc)
    assert [ch.paragraphs for ch in res.chapters] == [["나."], ["가.", "다."]]


def test_nothing_ready_builds_nothing():
    res = build(boxes())
    assert res.chapters == [] and res.added == [] and res.waiting == []


def test_appending_starts_after_the_existing_chapters():
    """Photograph chapter 13, build, translate — indices 1..12 and their finished
    translations must not move."""
    existing = chapters(12)
    doc = boxes([page("새 장의 본문")])
    doc["build"] = {"chapters": 12, "page_map": {}}
    res = build_boxes(doc, existing)
    assert res.added == [13], "an append must not renumber existing chapters"
    assert res.chapters[12].index == 13 and res.chapters[:12] == existing
    assert "13" in res.page_map


def test_building_stops_at_the_first_box_that_isnt_ready():
    """Box N is chapter N. Building box 3 while box 2 waits would give it number 2,
    and box 2 would later be appended after it, out of order."""
    doc = boxes([page("1장")], [page("2장", status=STATUS_NEEDS_CHECK)], [page("3장")])
    res = build(doc)
    assert res.added == [1]
    assert [(w["number"], w["blocked_by"]) for w in res.waiting] == [(2, None), (3, 2)]
    assert res.waiting[1]["reasons"] == ["Comes after chapter 2, which isn't ready yet."]
    assert doc["batches"][2]["chapter"] is None


def test_an_empty_or_all_skipped_box_blocks_the_boxes_after_it():
    doc = boxes([page("1장")], [], [page("3장")])
    res = build(doc)
    assert res.added == [1] and [w["number"] for w in res.waiting] == [2, 3]
    assert res.waiting[0]["reasons"][0].startswith("No photos yet.")

    doc = boxes([page("1장")], [page("표지", status=STATUS_SKIPPED)], [page("3장")])
    res = build(doc)
    assert res.added == [1] and "marked Skip" in res.waiting[0]["reasons"][0]
    assert res.waiting[1]["blocked_by"] == 2


# ---- built chapters follow their boxes ---------------------------------------

def test_a_changed_built_box_is_rebuilt_in_place_from_its_current_pages():
    doc = boxes([page("첫째."), page("둘째.")], [page("다음 장.")])
    first = build(doc)
    p1, p2 = doc["pages"][:2]
    P.move_pages(doc, [p2["id"]], p2["batch"], before=p1["id"])

    res = build(doc, first.chapters)
    assert res.refreshed == [1] and res.added == [], "rebuilt in place, not appended"
    assert res.chapters[0].paragraphs == ["둘째.", "첫째."]
    assert res.page_map["1"] == [p2["id"], p1["id"]], \
        "the reader's photo strip follows the new order"
    assert res.chapters[1] == first.chapters[1], "the other chapter is untouched"


def test_a_page_moved_between_built_boxes_moves_its_text():
    doc = boxes([page("1장 첫째."), page("1장 둘째.")], [page("2장 첫째.")])
    first = build(doc)
    stray, opener = doc["pages"][1], doc["pages"][2]
    P.move_pages(doc, [stray["id"]], opener["batch"], before=opener["id"])

    res = build(doc, first.chapters)
    assert res.refreshed == [1, 2]
    assert [ch.paragraphs for ch in res.chapters] == [["1장 첫째."],
                                                      ["1장 둘째.", "2장 첫째."]]


def test_a_photo_never_feeds_two_chapters():
    """Chapter 1 can't be rebuilt yet — a photo still needs checking — so its text
    still holds the photo that moved out. Building chapter 2 with it as well would
    print that page twice."""
    doc = boxes([page("1장."), page("옮길 쪽."), page("확인할 쪽.")], [page("2장.")])
    first = build(doc)
    stray, check = doc["pages"][1], doc["pages"][2]
    check["status"] = STATUS_NEEDS_CHECK
    P.move_pages(doc, [stray["id"]], doc["batches"][1]["id"])

    res = build(doc, first.chapters)
    assert (res.refreshed, res.updated) == ([], [])
    waiting = {w["number"]: w for w in res.waiting}
    assert "needs checking" in waiting[1]["reasons"][0]
    assert "still part of chapter 1" in waiting[2]["reasons"][0]
    assert res.chapters == first.chapters


def test_unchanged_built_boxes_are_left_alone():
    """Even when a photo in one has since been marked to check: a status doesn't
    change a word, and rebuilding without that photo is exactly how pages used to go
    missing from built chapters."""
    doc = boxes([page("1장.")], [page("2장.")])
    first = build(doc)
    doc["pages"][0]["status"] = STATUS_NEEDS_CHECK
    res = build(doc, first.chapters)
    assert (res.added, res.refreshed, res.updated, res.waiting) == ([], [], [], [])
    assert res.chapters == first.chapters


def test_a_title_only_change_is_updated_without_a_retranslate():
    """A translation is checked against the paragraphs only, so a new title doesn't
    make it stale — but the title still has to reach the chapter."""
    doc = boxes([page("본문.")])
    first = build(doc)
    P.rename_box(doc, doc["batches"][0]["id"], "제1화: 시작")
    res = build(doc, first.chapters)
    assert res.updated == [1] and res.refreshed == []
    assert res.chapters[0].title == "제1화: 시작"


def test_a_built_box_with_no_chapter_left_to_rebuild_waits():
    doc = boxes([page("본문.")])
    build(doc)
    doc["pages"][0]["text"] = "고친 본문."
    res = build_boxes(doc, [])
    assert res.waiting[0]["number"] == 1 and res.waiting[0]["built"] is True
    assert "Start over" in res.waiting[0]["reasons"][0]


# ---- seams the build decides -------------------------------------------------

def test_undecided_seams_are_filled_by_the_free_rules_and_written_back():
    """Left undecided, every page break would build as a paragraph break."""
    doc = boxes([page("그는 천천히 문을"), page("열고 들어갔다.", join="")])
    res = build(doc)
    second = doc["pages"][1]
    assert res.chapters[0].paragraphs == ["그는 천천히 문을 열고 들어갔다."]
    assert (second["join_prev"], second["join_prev_source"]) == ("sentence", "auto")
    assert second["join_prev_id"] == doc["pages"][0]["id"]
    assert res.unworked_seams == 0 and res.warnings == []


def test_an_unsure_seam_becomes_a_paragraph_with_one_warning_per_chapter():
    doc = boxes([page("그가 조용히"),
                 page("「가자」고 하자 그녀가", join="", name="IMG_2.jpg"),
                 page("「응」이라고 답했다.", join="", name="IMG_3.jpg")])
    res = build(doc)
    assert res.chapters[0].paragraphs == ["그가 조용히", "「가자」고 하자 그녀가",
                                          "「응」이라고 답했다."]
    assert res.unworked_seams == 2
    assert len(res.warnings) == 1, "one warning per chapter, not one per seam"
    assert "IMG_2.jpg and IMG_3.jpg" in res.warnings[0]
    assert "Work out the joins" in res.warnings[0]
    assert doc["pages"][1]["join_prev_source"] == "auto", \
        "left for 'Work out the joins' to check"


def test_the_box_reads_built_right_after_its_build():
    """Its signature is taken after the build fills its seams. Taken before, every
    freshly built box would read "changed" and ask to be built again."""
    doc = boxes([page("그는 천천히 문을"), page("열고 들어가며 그가 조용히", join=""),
                 page("「가자」고 말했다.", join="")])
    build(doc)
    assert P.summary(doc)["batches"][0]["state"] == "built"


# ---- starting over and legacy piles ------------------------------------------

def test_start_over_renumbers_every_box_from_one():
    """And it removes empty boxes, built ones too: an empty box would stop the build
    at itself, and starting over is the only way to retire a built chapter."""
    doc = boxes([page("1장.")], [page("2장.")], [page("3장.")])
    first = build(doc)
    P.delete_pages(doc, [doc["pages"][0]["id"]])

    res = build(doc, first.chapters, append=False)
    assert res.added == [1, 2]
    assert [ch.paragraphs for ch in res.chapters] == [["2장."], ["3장."]]
    assert [b["chapter"] for b in doc["batches"]] == [1, 2], "the emptied box is gone"
    assert set(res.page_map) == {"1", "2"}


def test_legacy_boxes_are_never_rebuilt():
    """A version-1 pile build never said which page landed in which chapter, so
    rebuilding one of its chapters from the box would be a guess."""
    doc = boxes([page("옛 본문.")], [page("새 장.")])
    legacy = doc["batches"][0]
    legacy["legacy"] = True
    doc["build"] = {"chapters": 3, "page_map": {}}
    P.normalize(doc)
    existing = chapters(3)
    doc["pages"][0]["text"] = "고친 옛 본문."

    res = build_boxes(doc, existing)
    assert res.added == [4] and res.chapters[:3] == existing
    assert legacy.get("legacy") is True and legacy["chapter"] is None

    res = build_boxes(doc, existing, append=False)
    assert res.added == [1, 2], "starting over builds the pile as one chapter"
    assert res.chapters[0].paragraphs == ["고친 옛 본문."]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

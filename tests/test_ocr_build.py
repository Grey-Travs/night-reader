"""Tests for assembling transcribed pages into chapters.

This is where the page-seam decisions finally land. The failure this guards against
is subtle and pervasive: a sentence that ran across a page break becomes two
paragraphs, the translator faithfully preserves that paragraph structure, and the
English reads wrong at every single page boundary in the novel.

What has to hold:
- A ``sentence`` join introduces NO blank line and no line break.
- A ``paragraph`` join introduces exactly one blank line.
- A ``gap`` is reported rather than silently smoothed over — it is the only place a
  page that was never photographed can surface.
- ``batch`` mode makes one chapter per upload batch WITHOUT round-tripping through
  the text splitter, because a batch may legitimately contain no heading at all.
- Building an append leaves earlier chapter indices — and therefore their existing
  translations — untouched.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_ocr_build.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server.ocr_build import assemble_pages, build_chapters, usable_pages  # noqa: E402
from server.pages import STATUS_EDITED, STATUS_NEEDS_CHECK, STATUS_OK, STATUS_SKIPPED  # noqa: E402

_seq = [0]


def page(text, *, join="paragraph", glue="space", status=STATUS_OK,
         batch="b1", heading=None, name=""):
    _seq[0] += 1
    return {"id": f"{_seq[0]:08x}", "seq": _seq[0], "text": text, "join_prev": join,
            "join_glue": glue, "status": status, "batch": batch, "heading": heading,
            "name": name}


def doc(pages, batches=None):
    return {"version": 1, "pages": pages,
            "batches": batches or [{"id": "b1", "label": ""}]}


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


def test_chapter_separator_is_inserted_when_asked_for():
    text, _ = assemble_pages(
        [page("끝."), page("새 장.", join="chapter")], chapter_separator="---")
    assert "\n---\n" in text


# ---- which pages count -------------------------------------------------------

def test_skipped_pages_never_contribute():
    pages = [page("본문"), page("표지", status=STATUS_SKIPPED)]
    assert len(usable_pages(doc(pages), include="all")) == 1, \
        "a cover or blank page is not part of the novel"


def test_approved_filter_excludes_unchecked_pages():
    pages = [page("확인됨", status=STATUS_OK),
             page("편집됨", status=STATUS_EDITED),
             page("미확인", status=STATUS_NEEDS_CHECK)]
    approved = usable_pages(doc(pages), include="approved")
    assert len(approved) == 2, "only accepted or edited pages build by default"
    assert len(usable_pages(doc(pages), include="all")) == 3


def test_empty_pages_never_contribute():
    assert usable_pages(doc([page("   ")]), include="all") == []


# ---- batch mode --------------------------------------------------------------

def test_batch_mode_makes_one_chapter_per_batch():
    pages = [page("1쪽", batch="b1"), page("2쪽", batch="b1"),
             page("3쪽", batch="b2"), page("4쪽", batch="b2")]
    chapters, _, page_map = build_chapters(
        doc(pages, [{"id": "b1", "label": ""}, {"id": "b2", "label": ""}]), mode="batch")
    assert len(chapters) == 2, "each upload batch is one chapter"
    assert [c.index for c in chapters] == [1, 2]
    assert len(page_map["1"]) == 2, "the map records which pages made each chapter"


def test_batch_mode_needs_no_heading_or_separator():
    """A photographed chapter often has no heading on any page. Round-tripping
    through the splitter would collapse every batch into one chapter."""
    pages = [page("본문만 있는 쪽", batch="b1"), page("헤딩 없는 쪽", batch="b2")]
    chapters, _, _ = build_chapters(
        doc(pages, [{"id": "b1"}, {"id": "b2"}]), mode="batch")
    assert len(chapters) == 2, "batches split even with nothing to split on"


def test_batch_label_becomes_the_chapter_title():
    pages = [page("본문", batch="b1")]
    chapters, _, _ = build_chapters(
        doc(pages, [{"id": "b1", "label": "Chapter 12"}]), mode="batch")
    assert chapters[0].title == "Chapter 12"


def test_batch_falls_back_to_a_page_heading_for_its_title():
    pages = [page("본문", batch="b1", heading="제3화 문이 열렸다")]
    chapters, _, _ = build_chapters(doc(pages, [{"id": "b1", "label": ""}]), mode="batch")
    assert chapters[0].title == "제3화 문이 열렸다"


def test_batch_mode_preserves_sentence_joins_inside_a_batch():
    pages = [page("그는 천천히 문을", batch="b1"),
             page("열었다.", join="sentence", batch="b1")]
    chapters, _, _ = build_chapters(doc(pages), mode="batch")
    assert chapters[0].paragraphs == ["그는 천천히 문을 열었다."], \
        "a seam inside a batch must still join seamlessly"


def test_reordered_pages_group_by_arrangement_not_upload_order():
    """If the reader dragged pages around, their arrangement is the truth."""
    pages = [page("a", batch="b2"), page("b", batch="b1"), page("c", batch="b2")]
    chapters, _, _ = build_chapters(
        doc(pages, [{"id": "b1"}, {"id": "b2"}]), mode="batch")
    assert len(chapters) == 3, "three contiguous runs make three chapters"


# ---- pile modes --------------------------------------------------------------

def test_heading_mode_splits_one_pile_into_chapters():
    pages = [page("제1화 시작\n\n첫 장의 본문."),
             page("제2화 계속\n\n둘째 장의 본문.", join="chapter", heading="제2화 계속")]
    chapters, _, _ = build_chapters(doc(pages), mode="heading")
    assert len(chapters) == 2, "one pile splits on detected headings"
    assert chapters[0].title.startswith("제1화")


def test_single_mode_makes_one_chapter():
    pages = [page("첫 쪽"), page("둘째 쪽")]
    chapters, _, _ = build_chapters(doc(pages), mode="single")
    assert len(chapters) == 1


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        build_chapters(doc([page("본문")]), mode="nonsense")


def test_no_usable_pages_builds_nothing():
    chapters, warnings, page_map = build_chapters(doc([]), mode="batch")
    assert chapters == [] and page_map == {}


# ---- appending ---------------------------------------------------------------

def test_appending_starts_after_the_existing_chapters():
    """Photograph chapter 13, build, translate — indices 1..12 and their finished
    translations must not move."""
    pages = [page("새 장의 본문", batch="b9")]
    chapters, _, page_map = build_chapters(
        doc(pages, [{"id": "b9", "label": ""}]), mode="batch", start_index=13)
    assert chapters[0].index == 13, "an append must not renumber existing chapters"
    assert "13" in page_map


def test_appending_works_for_pile_modes_too():
    pages = [page("제1화 시작\n\n본문.")]
    chapters, _, _ = build_chapters(doc(pages), mode="heading", start_index=5)
    assert chapters[0].index == 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

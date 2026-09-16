"""Tests for building a Google Doc from a novel.

The module is pure on purpose, so all of this runs with no credentials and no network --
which matters because the alternative is finding out what a request shape does by sending
it to somebody's real Drive.

The property worth stating up front, because getting it wrong is what made an earlier
measurement of this feature wrong by a whole chapter: ``insertText`` does not insert a
PARAGRAPH, it inserts text, and Google starts a new paragraph at every newline. A stored
paragraph that already contains a line break therefore comes back as two.

Runs under pytest (``pytest tests/``) and standalone.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from translation_bot import docsmake  # noqa: E402
from translation_bot.docs_extract import Chapter  # noqa: E402


def _chapter(index, paragraphs, title=None):
    return Chapter(index=index, title=title or f"Chapter {index}",
                   paragraphs=list(paragraphs))


# ---- what the document will read back as ----------------------------------


def test_ordinary_prose_comes_back_exactly_as_it_went_in():
    paragraphs = ["He turned around.", "The room was empty.", "Nobody answered."]
    assert docsmake.reads_back_as(paragraphs) == paragraphs


def test_a_paragraph_holding_a_line_break_comes_back_as_two():
    # The measured case. One paragraph in, two out -- which is exactly how a round trip
    # reported as 165 of 165 was really 164.
    assert docsmake.reads_back_as(["one\ntwo"]) == ["one", "two"]


def test_a_blank_paragraph_is_dropped_on_the_way_back():
    # Whitespace-only paragraphs do not survive a read, so counting on one is counting on
    # something that is not there.
    assert docsmake.reads_back_as(["real", "   ", "also real"]) == ["real", "also real"]


def test_nothing_is_reported_for_a_novel_that_round_trips():
    chapters = [_chapter(1, ["Fine.", "Also fine."]), _chapter(2, ["Likewise."])]
    assert docsmake.round_trip_losses(chapters) == []


def test_the_chapter_with_a_line_break_is_named_and_counted():
    chapters = [_chapter(1, ["clean"]), _chapter(2, ["a\nb\nc", "d"])]
    losses = docsmake.round_trip_losses(chapters)
    assert len(losses) == 1
    assert losses[0]["index"] == 2
    assert losses[0]["title"] == "Chapter 2"
    # Named with the size of the difference, not just flagged.
    assert losses[0]["paragraphs"] == 2 and losses[0]["reads_back_as"] == 4


# ---- the document body ----------------------------------------------------


def test_a_tab_holds_the_chapter_in_order():
    body = docsmake.document_body([_chapter(1, ["first", "second"])])
    assert body[0]["title"] == "Chapter 1"
    assert body[0]["paragraphs"] == ["first", "second"]
    assert body[0]["chars"] == len("first\n\nsecond")


def test_an_empty_paragraph_never_reaches_the_document():
    body = docsmake.document_body([_chapter(1, ["kept", "  ", "also kept"])])
    assert body[0]["paragraphs"] == ["kept", "also kept"]


# ---- splitting across documents -------------------------------------------


def test_a_novel_that_fits_stays_in_one_document():
    body = docsmake.document_body([_chapter(i, ["x" * 100]) for i in range(1, 6)])
    assert len(docsmake.split_for_export(body)) == 1


def test_a_novel_over_the_cap_spans_several():
    # Harmless precisely because nothing points at an export. A novel's own SOURCE
    # document could never be split like this -- one project is one document.
    body = docsmake.document_body([_chapter(i, ["x" * 400]) for i in range(1, 11)])
    parts = docsmake.split_for_export(body, cap=1000)
    assert len(parts) > 1
    # Nothing lost in the splitting, and the order is preserved.
    assert sum(len(p) for p in parts) == 10
    assert [t["title"] for p in parts for t in p] == [f"Chapter {i}" for i in range(1, 11)]


def test_a_chapter_bigger_than_a_whole_document_is_not_dropped():
    # It will be refused by Google, which is the right place for that to fail. Silently
    # omitting a chapter is the one outcome nobody would notice.
    body = docsmake.document_body([_chapter(1, ["x" * 50]), _chapter(2, ["y" * 5000])])
    parts = docsmake.split_for_export(body, cap=1000)
    assert [t["title"] for p in parts for t in p] == ["Chapter 1", "Chapter 2"]


# ---- the requests themselves ----------------------------------------------


def test_one_tab_is_created_per_chapter_in_order():
    body = docsmake.document_body([_chapter(1, ["a"]), _chapter(2, ["b"])])
    reqs = docsmake.tab_requests(body)
    assert [r["addDocumentTab"]["tabProperties"]["index"] for r in reqs] == [0, 1]
    assert [r["addDocumentTab"]["tabProperties"]["title"] for r in reqs] \
        == ["Chapter 1", "Chapter 2"]


def test_no_tab_is_ever_nested():
    # flatten_child_tabs defaults to true, so a nested tab is merged into its parent on
    # read and its title discarded -- the document would come back short of chapters.
    body = docsmake.document_body([_chapter(1, ["a"])])
    assert "parentTabId" not in body and all(
        "parentTabId" not in r["addDocumentTab"]["tabProperties"]
        for r in docsmake.tab_requests(body))


def test_text_is_inserted_at_the_start_of_its_own_tab():
    body = docsmake.document_body([_chapter(1, ["a"]), _chapter(2, ["b"])])
    batches = docsmake.insert_batches(body, ["t1", "t2"])
    inserts = [r["insertText"] for b in batches for r in b]
    assert [i["location"]["tabId"] for i in inserts] == ["t1", "t2"]
    # Index 1 is where a tab's body begins, and inserts into different tabs do not shift
    # each other's indices.
    assert {i["location"]["index"] for i in inserts} == {1}


def test_a_long_novel_is_sent_in_several_calls():
    body = docsmake.document_body([_chapter(i, ["x" * 300]) for i in range(1, 11)])
    batches = docsmake.insert_batches(body, [f"t{i}" for i in range(1, 11)],
                                      chunk_chars=1000)
    assert len(batches) > 1
    assert sum(len(b) for b in batches) == 10


def test_a_mismatch_between_tabs_and_chapters_refuses_to_fill_anything():
    # If the tab ids do not line up with the chapters, filling would put each chapter's
    # text into the wrong tab. Better to write nothing.
    body = docsmake.document_body([_chapter(1, ["a"]), _chapter(2, ["b"])])
    with pytest.raises(ValueError):
        docsmake.insert_batches(body, ["only-one"])


def test_the_new_tab_ids_are_read_back_in_request_order():
    replies = [
        {"addDocumentTab": {"tabProperties": {"tabId": "t1"}}},
        {},  # a reply for some other request kind
        {"addDocumentTab": {"tabProperties": {"tabId": "t2"}}},
    ]
    assert docsmake.tab_ids_from_replies(replies) == ["t1", "t2"]


def test_no_replies_at_all_is_not_an_error_here():
    # It becomes one in insert_batches, where it can say something useful.
    assert docsmake.tab_ids_from_replies(None) == []


# ---- checking what came back ----------------------------------------------


def test_a_clean_document_checks_out():
    body = docsmake.document_body([_chapter(1, ["a", "b"])])
    fetched = [_chapter(1, ["a", "b"])]
    assert docsmake.check(body, fetched)["ok"] is True


def test_a_missing_chapter_is_a_problem():
    body = docsmake.document_body([_chapter(1, ["a"]), _chapter(2, ["b"])])
    out = docsmake.check(body, [_chapter(1, ["a"])])
    assert out["ok"] is False
    assert any("not 2" in p for p in out["problems"])


def test_a_title_that_never_took_is_a_problem():
    body = docsmake.document_body([_chapter(1, ["a"], title="Real Title")])
    out = docsmake.check(body, [_chapter(1, ["a"], title="Chapter 1")])
    assert out["ok"] is False
    assert any("Real Title" in p for p in out["problems"])


def test_text_that_came_back_different_is_reported_but_not_a_problem():
    # The distinction that matters for an export: nothing points at it, so a difference
    # costs a paragraph mark in a document nobody reads back, not a finished chapter.
    body = docsmake.document_body([_chapter(1, ["a\nb"])])
    out = docsmake.check(body, [_chapter(1, ["a", "b"])])
    assert out["ok"] is False
    assert out["problems"] == []
    assert out["differed"] == ["Chapter 1"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

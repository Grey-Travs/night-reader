"""Tests for resolving each tab's real (global) chapter number.

A long novel is split across several Google Docs, so chapter 101 restarts at tab 1 and
reading or posting it needs the global number. Every obvious source for that number is a
trap in the real library, and each test below pins one of them, named for the trap it
guards:

* tab titles are "Tab N" where N is CREATION ORDER -- not position, not the chapter;
* one document's headers restart at 1 even though it holds chapters 101+;
* one runs 101-146 and then drops to 1-7, because the tail is side stories;
* two documents contain byte-identical duplicate tabs that were both translated and paid
  for;
* one is genuinely missing a chapter, and one project NAME states a range that is stale.

The fixtures reproduce the real header shapes (a ridibooks URL line, then a title line
ending in "NNN화") rather than reading the cached source.json files, so the suite stays
hermetic and does not depend on anyone's library being present.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_chapter_numbers.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.chapter_numbers import (  # noqa: E402
    KIND_CHAPTER,
    KIND_DUPLICATE,
    KIND_SIDE,
    detect_gaps_duplicates,
    infer_mapping,
    parse_chapter_number,
    parse_tab_title,
)
from translation_bot.docs_extract import Chapter  # noqa: E402

TITLE = "모두가 수상할 정도로 나를 노린다"
# Long enough, and Korean enough, not to be classified as front/back matter.
BODY = "그는 천천히 고개를 돌려 나를 바라보았다. " * 12


def _chapter(index, *, number=None, suffix="", body=BODY, title=None, marker="화"):
    """One tab in the shape the source export actually produces."""
    head = [f"ridibooks.com/books/{2336019343 + index}/view", ""]
    if number is not None:
        head.append(f"{TITLE} {number}{marker}{suffix}")
    else:
        head.append(TITLE)
    return Chapter(
        index=index,
        title=title if title is not None else f"Tab {index}",
        paragraphs=["\n\n".join(head), body],
    )


def _doc(numbers, **kw):
    return [_chapter(i, number=n, **kw) for i, n in enumerate(numbers, 1)]


# ---- the number lives in the text, not the tab title -----------------------


def test_generic_tab_title_is_treated_as_no_signal():
    # "Tab 41" is creation order. Reading it as a position or a chapter number is worse
    # than having no tab title at all, so it must come back as None.
    assert parse_tab_title("Tab 41") is None
    assert parse_tab_title("Tab 1") is None
    assert parse_tab_title("") is None


def test_a_real_tab_title_still_yields_its_number():
    assert parse_tab_title("101") == 101
    assert parse_tab_title("Chapter 12") == 12
    assert parse_tab_title("12화") == 12


def test_number_is_read_from_the_export_header():
    assert parse_chapter_number(_chapter(1, number=57).text) == 57


def test_number_survives_a_suffix_after_the_marker():
    # The final chapter is titled "...101화 (완결)". The end-anchored header pattern in
    # sanitize misses it because of the trailing suffix; the fallback must catch it, or
    # a document's last chapter silently loses its number.
    assert parse_chapter_number(_chapter(1, number=101, suffix=" (완결)").text) == 101


def test_a_number_in_the_body_is_not_mistaken_for_a_chapter_number():
    # Only the opening blocks are scanned. "3화" occurs in dialogue often enough that
    # scanning a whole chapter would invent numbers.
    ch = Chapter(index=1, title="Tab 1", paragraphs=[TITLE, "그는 3화를 읽었다. " * 20])
    assert parse_chapter_number(ch.text) is None


# ---- resolving a whole document -------------------------------------------


def test_a_first_document_resolves_to_its_own_numbering():
    rows = infer_mapping(_doc(range(1, 21)))
    assert [r.global_number for r in rows] == list(range(1, 21))
    assert {r.confidence for r in rows} == {"high"}


def test_a_sequel_document_keeps_its_global_numbers():
    # The common, happy case: the export already numbers part 2 from 101.
    rows = infer_mapping(_doc(range(101, 121)), start_hint=101)
    assert [r.global_number for r in rows] == list(range(101, 121))
    assert {r.kind for r in rows} == {KIND_CHAPTER}


def test_tabs_without_a_header_are_filled_from_the_run():
    # Only 30-85% of tabs carry a header. A miss is absence of signal, not absence of a
    # chapter, so it is filled from its run's offset and marked as inferred.
    numbers = [1, None, None, 4, 5]
    rows = infer_mapping(_doc(numbers))
    assert [r.global_number for r in rows] == [1, 2, 3, 4, 5]
    assert [r.confidence for r in rows] == ["high", "inferred", "inferred", "high", "high"]


def test_a_document_whose_headers_restart_at_one_is_corrected_by_the_hint():
    # THE important case. This document holds chapters 101+ but its own headers read
    # 1..20, so the measured offset is -1 and taking it would number the whole document
    # wrongly. The series position must win.
    rows = infer_mapping(_doc(range(1, 21)), start_hint=101)
    assert [r.global_number for r in rows] == list(range(101, 121))


def test_the_hint_does_not_fight_headers_that_already_agree():
    rows = infer_mapping(_doc(range(101, 121)), start_hint=101)
    assert [r.global_number for r in rows] == list(range(101, 121))


def test_the_hint_does_not_close_a_gap_ahead_of_it():
    # Regression: the hint used to be applied unconditionally, which forced stated numbers
    # onto a contiguous count and silently erased a real gap. Here the series expects this
    # document to start at 4 but it states 5 -- chapter 4 is genuinely missing, and
    # renumbering to 4,5 would hide that and desynchronise everything after it.
    rows = infer_mapping(_doc([5, 6]), start_hint=4)
    assert [r.global_number for r in rows] == [5, 6]


def test_a_one_chapter_overlap_is_believed_rather_than_shifted():
    # A sequel that repeats the previous document's last chapter states a real global
    # number and sits one below what the series expects. That is not a restart.
    rows = infer_mapping(_doc([100, 101, 102]), start_hint=101)
    assert [r.global_number for r in rows] == [100, 101, 102]


# ---- the anomalies --------------------------------------------------------


def test_a_mid_document_restart_becomes_side_stories():
    # "I Possessed a Character 2" runs 101-146 and then drops to 1-7. Those trailing tabs
    # are extras, not chapters 1-7, and must not take chapter numbers.
    rows = infer_mapping(_doc([101, 102, 103, 1, 2]), start_hint=101)
    assert [r.global_number for r in rows] == [101, 102, 103, None, None]
    assert [r.kind for r in rows[3:]] == [KIND_SIDE, KIND_SIDE]
    assert {r.confidence for r in rows[3:]} == {"low"}  # forced into review


def test_a_side_story_marker_in_the_title_is_respected():
    # "외전" (side story) appears in the header of real tabs that are numbered from 1
    # alongside a final chapter. The word alone is enough.
    chapters = [
        _chapter(1, number=101, suffix=" (완결)"),
        _chapter(2, number=1, marker="화", title="Tab 2"),
    ]
    chapters[1] = Chapter(
        index=2,
        title="Tab 2",
        paragraphs=[f"ridibooks.com/books/2336023697/view\n\n{TITLE} 외전 1화", BODY],
    )
    rows = infer_mapping(chapters, start_hint=101)
    assert rows[0].global_number == 101 and rows[0].kind == KIND_CHAPTER
    assert rows[1].kind == KIND_SIDE and rows[1].global_number is None


def test_byte_identical_duplicate_tab_is_dropped_from_the_sequence_only():
    # Both copies were translated and billed. The later one leaves the reading sequence,
    # but it keeps its index -- its chapter file, state, previous/ and variants/ are all
    # keyed on that and must not move.
    chapters = _doc([64, 64, 66])
    chapters[1] = Chapter(index=2, title="Tab 2", paragraphs=list(chapters[0].paragraphs))
    rows = infer_mapping(chapters)
    assert rows[1].kind == KIND_DUPLICATE
    assert rows[1].duplicate_of == 1
    assert rows[1].global_number is None
    assert rows[1].index == 2  # never renumbered


def test_a_real_gap_is_reported_not_silently_closed():
    # One document genuinely has no chapter 65. Closing the gap would renumber every
    # chapter after it and desynchronise the whole series.
    rows = infer_mapping(_doc([63, 64, 66, 67]))
    info = detect_gaps_duplicates(rows)
    assert info["gaps"] == [65]
    assert info["first"] == 63 and info["last"] == 67


def test_gaps_and_duplicates_report_cleanly_on_a_tidy_document():
    info = detect_gaps_duplicates(infer_mapping(_doc(range(1, 11))))
    assert info == {"gaps": [], "duplicates": [], "first": 1, "last": 10}


def test_an_empty_document_does_not_explode():
    assert infer_mapping([]) == []
    assert detect_gaps_duplicates([])["gaps"] == []


def test_a_document_with_no_headers_at_all_falls_back_to_position():
    # Nothing stated anywhere: number by position but mark every row low, so a human
    # confirms rather than the app inventing a numbering silently.
    rows = infer_mapping(_doc([None] * 5))
    assert [r.global_number for r in rows] == [1, 2, 3, 4, 5]
    assert {r.confidence for r in rows} == {"low"}


def test_index_is_never_renumbered():
    # The hard constraint: index is the contract with state.json keys, chapter-NNN.md,
    # previous/, audit/ and variants/. Only `global` is new.
    rows = infer_mapping(_doc([101, 102, 1, 2]), start_hint=101)
    assert [r.index for r in rows] == [1, 2, 3, 4]


if __name__ == "__main__":
    import traceback

    failures = 0
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  ok  {_name}")
            except Exception:
                failures += 1
                print(f"FAIL  {_name}")
                traceback.print_exc()
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)

"""Export cruft that leaked all the way into the saved translation.

A RIDI export tab arrives wrapped in boilerplate:

    ridibooks.com/books/2847008759/view
    체리, 팝! (CHERRY POP!) 29화
    4-5 minutes
    29.
    …the actual chapter…
    ※ 본 저작물의 권리는 저작권자에게 있습니다…

``strip_source_header`` removed the first three but deliberately kept the bare ``29.``,
and nothing anywhere removed the closing copyright notice, so both were handed to the
model and came back inside the English. 329 saved chapters begin with that number and 132
end with a translated copyright notice.

None of it is visible in the reader — ``29.`` alone on a line is a valid EMPTY ordered
list item, so Markdown renders it as ``<ol start="29"><li></li></ol>`` and the reading CSS
defines no list rules, so it occupies no space. The text is still there, which is why it
turns up the moment a chapter is copied or edited.

The bare number is only junk when it repeats the chapter number the header already gave
us. A part marker that does NOT match is in-story content and must survive.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_export_artifacts.py``).
"""

import os
import sys

# ---- a Korean novel title must not 500 the download -------------------------
# HTTP headers are latin-1, and _safe_name uses \w, which in Python is Unicode-aware
# and therefore KEEPS Hangul. A hand-built filename="밥만_했는데.epub" raised
# UnicodeEncodeError inside the server and the export returned 500 — which is nearly
# every novel in this library.


def test_attachment_header_survives_a_korean_title():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import server.app as A

    header = A._attachment(f"{A._safe_name('밥만 했는데 주인공들한테 고백받았다')}.epub")
    header.encode("latin-1")  # raises if the bug is back
    assert "filename*=UTF-8''" in header, "the real name must still reach the browser"
    assert "%" in header.split("filename*=UTF-8''", 1)[1], "it should be percent-encoded"


def test_attachment_header_keeps_a_plain_ascii_name_readable():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import server.app as A

    header = A._attachment("My_Novel.epub")
    header.encode("latin-1")
    assert 'filename="My_Novel.epub"' in header


def test_attachment_header_always_has_an_ascii_fallback():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import server.app as A

    # A title with no transliterable characters at all must still yield something.
    header = A._attachment("한국어.csv")
    header.encode("latin-1")
    assert 'filename="' in header and 'filename=""' not in header

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.docs_extract import Chapter  # noqa: E402
from translation_bot.pipeline import stripped_chapter  # noqa: E402
from translation_bot.sanitize import (  # noqa: E402
    strip_export_footer,
    strip_source_header,
)

HEADER = "\n\n".join([
    "ridibooks.com/books/2847008759/view",
    "체리, 팝! (CHERRY POP!) 29화",
    "4-5 minutes",
])

NOTICE = ("※ 본 저작물의 권리는 저작권자에게 있습니다. 저작물을 복사, 복제, 수정, 배포할 "
          "경우 형사상 처벌 및 민사상 책임을 질 수 있습니다.")

NOTICE_EN = ("The rights to this work belong to the copyright holder. Copying, "
             "reproducing, modifying, or distributing this work may result in criminal "
             "punishment and civil liability.")


# ---- the leading bare number -------------------------------------------------

def test_a_bare_number_repeating_the_chapter_number_is_dropped():
    # "29화" in the header and "29." right under it are the same fact twice. The second
    # is what ends up at the top of the saved English.
    text = f"{HEADER}\n\n29.\n\nThe first mission had barely ended."
    clean, number = strip_source_header(text)
    assert number == "29"
    assert clean == "The first mission had barely ended."


def test_a_part_marker_that_is_not_the_chapter_number_survives():
    # The behaviour the old docstring promised, and the reason we compare against the
    # header's number instead of just deleting any leading digits: this "33." is an
    # in-story section marker inside chapter 29.
    text = f"{HEADER}\n\n33.\n\nThe first mission had barely ended."
    clean, number = strip_source_header(text)
    assert number == "29"
    assert clean.startswith("33.")


def test_a_bare_number_with_no_header_to_compare_against_survives():
    # Without a "NNN화" line there is nothing establishing that the number is the
    # chapter's, so removing it would be a guess.
    text = "12.\n\nThe first mission had barely ended."
    clean, number = strip_source_header(text)
    assert number is None
    assert clean.startswith("12.")


def test_only_the_first_block_is_considered():
    # A number deeper in the chapter is prose, however much it looks like a marker.
    text = f"{HEADER}\n\nThe first mission had barely ended.\n\n29.\n\nMore prose."
    clean, _ = strip_source_header(text)
    assert "29." in clean


# ---- the trailing copyright notice ------------------------------------------

def test_the_korean_copyright_notice_is_removed():
    text = f"The setup for the second mission was complete.\n\n{NOTICE}"
    assert strip_export_footer(text) == "The setup for the second mission was complete."


def test_the_translated_copyright_notice_is_removed():
    # 132 chapters have the model's English rendering of it rather than the Korean.
    text = f"The setup for the second mission was complete.\n\n{NOTICE_EN}"
    assert strip_export_footer(text) == "The setup for the second mission was complete."


def test_ordinary_closing_prose_is_left_alone():
    text = "He closed the door behind him, and the corridor went quiet."
    assert strip_export_footer(text) == text


def test_a_notice_mid_chapter_is_not_touched():
    # Only trailing blocks are footer. A line about rights inside the story is story.
    text = (f"{NOTICE_EN}\n\nHe closed the door behind him.\n\n"
            "The corridor went quiet.")
    assert strip_export_footer(text) == text


def test_both_ends_come_off_together():
    text = f"{HEADER}\n\n29.\n\nThe first mission had barely ended.\n\n{NOTICE}"
    clean, number = strip_source_header(text)
    assert number == "29"
    assert strip_export_footer(clean) == "The first mission had barely ended."


# ---- one chapter, one paragraph count ---------------------------------------

def _tab(*paragraphs) -> Chapter:
    return Chapter(index=29, title="Tab 29", paragraphs=list(paragraphs))


def test_stripped_chapter_counts_only_the_prose():
    # The raw tab is 3 header blocks + the repeated number + 2 of prose + the notice.
    # Everything that judges the translation must see the 2, or a faithful chapter
    # measures as short against a source full of boilerplate.
    raw = _tab("ridibooks.com/books/2847008759/view",
               "체리, 팝! (CHERRY POP!) 29화",
               "4-5 minutes",
               "29.",
               "The first mission had barely ended.",
               "The setup was complete.",
               NOTICE)
    assert raw.metrics.paragraph_count == 7
    assert stripped_chapter(raw).metrics.paragraph_count == 2


def test_stripped_chapter_returns_the_original_when_there_is_nothing_to_strip():
    clean = _tab("He closed the door.", "The corridor went quiet.")
    assert stripped_chapter(clean) is clean


# ---- inferring a chapter number the header never gave -----------------------
# tools/clean_export_artifacts.py has to decide whether a saved chapter's leading "147."
# is the chapter number or in-story content. Where the tab's own header is missing, it
# fills the gap from the neighbouring tabs — and getting that wrong either leaves cruft
# behind or deletes a real part marker.

def _numbers(monkeypatch, mapping, count=None):
    """Run source_numbers over a fake project whose tabs yield `mapping` index -> number."""
    from tools import clean_export_artifacts as T
    from translation_bot.docs_extract import Chapter

    n = count or max(mapping)
    chapters = [Chapter(index=i, title=f"Tab {i}", paragraphs=["x"]) for i in range(1, n + 1)]
    monkeypatch.setattr(T, "records_to_chapters", lambda records: chapters)
    monkeypatch.setattr(T, "strip_source_header",
                        lambda text: ("", mapping.get(int(text), None)))
    # feed each chapter its own index as the "text" the fake stripper reads
    for c in chapters:
        c.paragraphs[:] = [str(c.index)]

    class _P:
        name = "fake"

        def __truediv__(self, other):
            return _F()

    class _F:
        def exists(self):
            return True

        def read_text(self, encoding=None):
            return "[]"

    return T.source_numbers(_P())


def test_a_gap_between_two_known_tabs_is_filled(monkeypatch):
    # 146 _ 148 -> the one between them is 147.
    got = _numbers(monkeypatch, {1: "145", 2: "146", 4: "148", 5: "149"}, count=5)
    assert got[3] == "147"


def test_a_gap_continuing_a_run_is_filled(monkeypatch):
    # 145, 146, _  with nothing usable after -> 147.
    got = _numbers(monkeypatch, {1: "145", 2: "146"}, count=3)
    assert got[3] == "147"


def test_a_run_continues_across_a_restart(monkeypatch):
    # The real shape: one source doc holding two works, numbered …145, 146, then
    # restarting at 1. The tab in the gap has no header of its own. The run before it
    # says 147; the tabs after it say nothing, because a restart is not a continuation.
    got = _numbers(monkeypatch, {1: "145", 2: "146", 4: "1", 5: "2"}, count=5)
    assert got[3] == "147"


def test_an_isolated_gap_is_left_unknown(monkeypatch):
    got = _numbers(monkeypatch, {1: "145", 5: "200"}, count=5)
    assert 3 not in got


def test_a_predicted_number_only_deletes_a_block_that_matches_it():
    """The property that makes the inference safe.

    A predicted number is never trusted on its own: it only removes a leading block that
    IS that number. So a wrong prediction is inert — it deletes nothing — and the guess
    can only act where it turns out to be right.
    """
    from tools.clean_export_artifacts import clean_prose

    prose = "147.\n\nI told him over and over not to get close to humans."
    # Prediction matches the block -> the repeated chapter number comes off.
    assert clean_prose(prose, "147")[1] is True
    # Prediction is wrong -> nothing is touched, the block survives.
    assert clean_prose(prose, "1")[1] is False
    assert clean_prose(prose, None)[1] is False
    assert clean_prose(prose, "1")[0].startswith("147.")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

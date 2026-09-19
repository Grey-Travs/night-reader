"""The browser and the server must decide "same chapter?" identically.

Posting the same chapter twice is the expensive mistake in this app, and every guard
against it is ultimately a string comparison. The server decides what may post
(``plan_run``'s blockers); the extension decides what it will actually create
(``buildQueue``, and the last-moment check before a create). If those two normalise a
title differently, one of two things happens, and both are silent:

* the extension thinks a chapter is absent that the server thinks is posted -- a duplicate
  readers can buy twice;
* the extension thinks a chapter is present that the server thinks is ready -- a chapter
  that can never be posted at all.

So there is one rule, implemented twice, and this stands over both.

Nothing else in the suite shells out, so this skips cleanly when Node isn't on PATH; it is
a drift alarm, not a build requirement.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_title_parity.py``).
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server.titles import site_title_key  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLES_JS = os.path.join(REPO, "extension", "titles.js")

# Real shapes from the site, plus the ones most likely to expose a difference between two
# regex engines. Several of these are transcribed from actual snapshots on disk -- the
# "Side Chapter" family is what `I Possessed a Character` really carries, and "ch" is a
# junk title a human really typed into `Becoming the Top's Older Brother`.
CASES = [
    "Chapter 61",
    "chapter 61",
    "CHAPTER 61",
    "  Chapter 61  ",
    "Chapter  61",                 # a double space, which collapses
    "Side Chapter 1",
    "Side Chapter  8",
    "Side Stories 1",
    "Prologue",
    "ch",
    "",
    "   ",
    "Chapter 118: The Duel",
    "この章",          # a CJK title
    "Chapter 61 55",          # NBSP between the name and its coin badge
    " Chapter 61",            # leading NBSP
    "Chapter​61",             # zero-width space mid-title: reads as one word
    "﻿Chapter 61",            # BOM, which survives a copy-paste
    "Chapter 61﻿",
    "Ｃhapter ６１",   # fullwidth letters and digits, folded by NFKC
    "Chapter 1",              # thin space
    "Chapter　1",              # ideographic space
    "Ch. 61 — the duel",      # em dash
    "Café Chapter 1",         # precomposed e-acute
    "Café Chapter 1",        # decomposed: NFKC makes these the same title
]

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is not installed")


def _js_keys() -> list[str]:
    script = (
        "const { nrTitleKey } = require("
        + json.dumps(TITLES_JS.replace(os.sep, "/"))
        + ")\n"
        "const cases = " + json.dumps(CASES) + "\n"
        "console.log(JSON.stringify(cases.map(nrTitleKey)))\n"
    )
    result = subprocess.run([_NODE, "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def test_the_two_implementations_agree_exactly():
    for title, js in zip(CASES, _js_keys()):
        assert site_title_key(title) == js, (
            f"titles.js and titles.py disagree on {title!r} — one side would post a "
            f"duplicate, or block a chapter that has never been posted"
        )


# The cases above prove the two AGREE. These prove they agree on the RIGHT answer: both
# returning the raw string would be agreement too, and would defeat the whole guard. This
# is the lesson recorded in tests/test_paragraph_split_parity.py, where a parity test
# passed for months while the two implementations genuinely disagreed.

SAME = [
    ("Chapter 61", "chapter 61", "case is not a difference"),
    ("Chapter 61", "  Chapter 61 ", "surrounding space is not a difference"),
    ("Chapter 61", "Chapter  61", "a double space is not a difference"),
    ("Chapter 61", "Chapter 61", "a no-break space is not a difference"),
    ("Chapter 61", "﻿Chapter 61", "a byte-order mark is not a difference"),
    ("Chapter 61", "Ｃhapter ６１", "fullwidth forms are the same title"),
    ("Café 1", "Café 1", "composed and decomposed accents are the same title"),
]


@pytest.mark.parametrize("a,b,why", SAME, ids=[w for _, _, w in SAME])
def test_titles_that_must_compare_equal(a, b, why):
    assert site_title_key(a) == site_title_key(b), why


DIFFERENT = [
    ("Chapter 61", "Chapter 610", "a longer number is a different chapter"),
    ("Chapter 61", "Chapter 6", "a shorter number is a different chapter"),
    ("Side Chapter 1", "Side Stories 1", "two naming conventions are not the same title"),
    ("Chapter 61", "Chapter 61 55", "the coin badge is NOT stripped — see below"),
    ("Prologue", "Chapter 1", "a prologue is not chapter one"),
]


@pytest.mark.parametrize("a,b,why", DIFFERENT, ids=[w for _, _, w in DIFFERENT])
def test_titles_that_must_stay_different(a, b, why):
    assert site_title_key(a) != site_title_key(b), why


def test_the_coin_badge_is_left_alone():
    """The site renders a chapter's name and its coin badge as one text run, so a scraped
    title can arrive as "Chapter 61 55". It is tempting to strip a trailing number here.

    That would make "Side Chapter 1" and "Side Chapter" the same title — the exact trap
    adapters/meiko.toml warns about — so the badge stays in the key, and the real fix is
    to read the site's own records instead of scraping the DOM.
    """
    assert site_title_key("Chapter 61 55") == "chapter 61 55"
    assert site_title_key("Side Chapter 1") != site_title_key("Side Chapter")


def test_a_zero_width_space_does_not_split_a_word():
    # Removed, not turned into a space: it has zero width, so it reads as one word.
    assert site_title_key("Chapter​61") == "chapter61"


def test_nothing_becomes_the_empty_key_by_accident():
    """The empty key is how "this row had no title" is spelled, so only genuinely empty
    input may produce it. A title that collapses to nothing would silently match every
    other blank row."""
    assert site_title_key("") == ""
    assert site_title_key("   ") == ""
    assert site_title_key(None) == ""
    assert site_title_key("ch") == "ch"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

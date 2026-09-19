"""One answer to "are these two chapter titles the same one?".

Why this module exists
----------------------
Posting the same chapter twice is the expensive mistake in this app: readers can buy a
duplicate, and removing one on meiko means retyping its name to confirm. Everything that
prevents it is ultimately a string comparison -- the ledger, the site snapshot, and the
extension's own last-moment check -- and until this module there were FIVE spellings of
that comparison across two languages:

    server/posting.py     item["title"] in posted                exact
    server/posting.py     item["title"].casefold() in on_site    casefold
    extension/content.js  .toLowerCase()                         in four places
    extension/content.js  i.title === pending.title              exact

Any two of those disagreeing is a duplicate posted, or a chapter permanently blocked. So
there is now one rule, implemented twice -- here and in ``extension/titles.js`` -- with
``tests/test_title_parity.py`` standing over both.

The rule
--------
NFKC, drop zero-width characters, collapse whitespace runs to one space, strip, lowercase.

Three decisions worth keeping:

* **Lowercase, not casefold.** JavaScript has no ``casefold``. Rather than approximate one
  and drift, the rule is defined as the plain Unicode lowercase mapping, which both
  languages implement identically. This is very slightly narrower than the ``casefold()``
  it replaces in ``plan_run`` -- German sharp s no longer folds to "ss" -- which cannot
  matter for titles of the form "Chapter 61" and is worth the parity.

* **An explicit whitespace class, never the shorthand.** The two engines genuinely
  disagree about what ``\\s`` means: Python matches the four separator controls and NEL,
  JavaScript matches the byte-order mark. That exact difference already cost this project
  a silent bug -- see the note in ``tests/test_paragraph_split_parity.py``, where a parity
  test passed for months while the two implementations disagreed, because every case in it
  was built from ordinary spaces.

* **No coin-badge stripping.** It is tempting to drop a trailing number, because the site
  renders a chapter's name and its coin badge as one text run ("Chapter 61 55"). Doing it
  here would make "Side Chapter 1" and "Side Chapter" the same title, which is the exact
  trap ``adapters/meiko.toml`` warns about. The badge is an artefact of scraping the DOM;
  the cure is to stop scraping it, not to launder it in the comparison.
"""

from __future__ import annotations

import re
import unicodedata

# Zero-width and invisible: these carry no meaning in a title and survive a copy-paste, so
# a title differing only by one of them is the same title. Removed rather than turned into
# a space -- they are zero WIDTH, so a word split by one reads as one word, not two.
_INVISIBLE = "​‌‍﻿"

# Spelled out rather than written as the shorthand class, because the shorthand is
# precisely where the two regex engines differ. Every character here means the same thing
# in both. NFKC already folds most of these to a plain space; they are listed anyway so
# the rule does not depend on which ones it happens to fold.
_SPACE = (
    " \t\n\r\f\v"
    ""           # NEL
    " "           # no-break space
    " "           # ogham space mark
    " - "    # en quad .. hair space
    "  "     # line / paragraph separator
    " "           # narrow no-break space
    " "           # medium mathematical space
    "　"           # ideographic space
)

_INVISIBLE_RE = re.compile("[" + _INVISIBLE + "]")
_SPACE_RE = re.compile("[" + _SPACE + "]+")


def site_title_key(title: str | None) -> str:
    """The comparison key for a chapter title, on the site or in our own records.

    Compare keys, never raw titles.
    """
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = _INVISIBLE_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text)
    return text.strip().lower()

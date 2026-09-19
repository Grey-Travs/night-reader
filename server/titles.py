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


# ---------------------------------------------------------------------------
# What this novel calls its chapters
# ---------------------------------------------------------------------------

# A family needs this many members before it is a naming convention rather than a
# coincidence. Two chapters happening to end in a number is not a pattern; three is.
MIN_FAMILY = 3

# Greedy prefix, so the LAST run of digits is the one taken: "Ep. 2 Part 3" is part 3 of
# episode 2, not episode 2. The suffix cannot contain digits, which is what lets
# "Chapter 118: The Duel" resolve to 118 with ": The Duel" as the suffix.
_TAIL_NUMBER = re.compile(r"^(.*?)(\d+)(\D*)$")


def learn_naming(titles: list[str], chapter_template: str | None = None) -> dict:
    """What this novel calls its chapters and its side stories, read off the site.

    The problem it solves, measured: ``I Possessed a Character`` has eight side stories
    live as "Side Chapter 1".."Side Chapter 8", while this app generates "Side Stories N".
    Nothing matches, so the duplicate guard cannot see them and all eight would post a
    second time. Every series with side stories has the same zero overlap.

    Rather than hard-code a second convention -- the site's naming is whatever a person
    typed, and it differs per novel -- the names are learned from the site's own list, and
    the same learned template is used to GENERATE titles and to match them. That is the
    point: once generation agrees with the site, matching is just the shared title key and
    there is no second code path to drift.

    Returns families keyed by their (prefix, suffix), plus the digit-free titles.
    """
    families: dict[tuple[str, str], set[int]] = {}
    singletons: list[str] = []
    for raw in titles or []:
        text = str(raw or "").strip()
        if not text:
            continue
        m = _TAIL_NUMBER.match(text)
        if not m:
            singletons.append(site_title_key(text))
            continue
        prefix, number, suffix = m.group(1), int(m.group(2)), m.group(3)
        families.setdefault((prefix.strip(), suffix.strip()), set()).add(number)

    real = {key: nums for key, nums in families.items() if len(nums) >= MIN_FAMILY}

    # Which family is the CHAPTERS is not a guess when the adapter already says what they
    # are called. Anchoring on it matters: picking the largest family instead would, on a
    # novel with few chapters and many side stories, label the side stories as chapters
    # and then generate every real chapter under the side name.
    chapter_key = None
    if chapter_template:
        head, _, tail = str(chapter_template).partition("{n}")
        wanted = (head.strip(), tail.strip())
        if wanted in families:
            chapter_key = wanted

    def coverage(nums: set[int]) -> float:
        """How much of 1..max this family actually fills. The chapter family of a novel
        runs 1, 2, 3 ...; a side family is smaller and often does not start at 1."""
        top = max(nums)
        return len(nums) / top if top else 0.0

    # No template, or the site does not use it: fall back to the fullest-looking family.
    if chapter_key is None:
        chapter_key = max(real, key=lambda k: (len(real[k]), coverage(real[k])),
                          default=None)

    def described(key):
        if key is None or key not in families:
            return {"template": "", "seen": 0, "numbers": [], "learned": False}
        prefix, suffix = key
        return {"template": f"{prefix} {{n}}{(' ' + suffix) if suffix else ''}".strip(),
                "seen": len(families[key]), "numbers": sorted(families[key]),
                "learned": True}

    # Every other numeric family is side content. The largest wins when there are several,
    # which is the only one worth generating into.
    side_keys = [k for k in real if k != chapter_key]
    side_key = max(side_keys, key=lambda k: len(real[k]), default=None)

    return {"chapter": described(chapter_key), "side": described(side_key),
            "singletons": sorted(set(singletons))}

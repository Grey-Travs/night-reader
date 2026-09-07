"""The browser and the server must split paragraphs identically.

The reader addresses a paragraph by the ordinal ``web/src/blocks.js`` produces, and
the server resolves that ordinal with ``translation_bot/paragraphs.py``. If the two
splitters ever disagree, a rewrite lands on the wrong paragraph — silently, and only
for the chapters whose formatting happens to expose the difference.

Nothing else in the suite shells out, so this skips cleanly when Node isn't on PATH;
it is a drift alarm, not a build requirement.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_paragraph_split_parity.py``).
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from translation_bot.paragraphs import split_blocks  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKS_JS = os.path.join(REPO, "web", "src", "blocks.js")

# Shapes that actually occur in these chapters, plus the ones most likely to expose a
# difference between two regex engines.
CASES = [
    "one\n\ntwo\n\nthree",
    "one\n\ntwo\n\nthree\n",                      # chapters always end in a newline
    "The door slid open.\n\n“Are you coming?” she asked.\n\nHe said nothing.\n",
    "a\n\n   \n\nb",                              # a whitespace-only block
    "a\n   \nb\n\n\n\nc",                         # odd separators must survive a splice
    "",
    "   ",
    "single",
    "trailing   \n\nnext",
    "그는 문을 열었다.\n\n밖에는 아무도 없었다.",       # Korean, in the source pane
    "text\n\n33.\n\nmore",                        # the author's own part divider
    "a\n\n***\n\nb",
    "\n\n\nlead\n\ntail\n\n\n",
    # The six characters where `\s` itself differs between the two engines. Every case
    # above is built from ordinary spaces and newlines, so the parity test passed for
    # months while the two splitters genuinely disagreed. Python's `\s` matches
    # \x1c-\x1f and \x85; JavaScript's matches ﻿. A blank line carrying one of
    # these split on one side only, and every rewrite on such a chapter was refused
    # with a 409 that nothing explained.
    "a\n﻿\nb",                               # BOM — survives a copy-paste
    "a\n\x85\nb",                                 # NEL
    "a\n\x1c\nb",
    "a\n\x1d\nb",
    "a\n\x1e\nb",
    "a\n\x1f\nb",
    "a\n ﻿ \nb",                             # mixed with ordinary spaces
    "one\n\ntwo\n﻿\nthree\n\nfour",          # mid-chapter, so ordinals shift
    "﻿one\n\ntwo",                           # a BOM at the very start of a file
]

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is not installed")


def _js_blocks() -> list[list[list]]:
    script = (
        f"import {{ splitBlocks }} from {json.dumps('file:///' + BLOCKS_JS.replace(os.sep, '/'))}\n"
        f"const cases = {json.dumps(CASES)}\n"
        "console.log(JSON.stringify(cases.map((c) => "
        "splitBlocks(c).map((b) => [b.i, b.start, b.end, b.text]))))\n"
    )
    result = subprocess.run([_NODE, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def test_the_two_splitters_agree_exactly():
    for text, js in zip(CASES, _js_blocks()):
        py = [[b.i, b.start, b.end, b.text] for b in split_blocks(text)]
        assert py == [list(row) for row in js], (
            f"blocks.js and paragraphs.py disagree on {text!r} — a rewrite would land "
            f"on the wrong paragraph"
        )


# The six characters are worth their own test as well as their entry in CASES: the
# list above proves the two agree, this proves they agree on the RIGHT answer. Both
# treating an invisible character as content would be "agreement" too, and would still
# be wrong — a line that looks blank to the reader is a paragraph break.

DIVERGENT = {
    "﻿": "BOM (JavaScript's \\s matches it, Python's does not)",
    "\x85": "NEL (Python's \\s matches it, JavaScript's does not)",
    "\x1c": "file separator (Python only)",
    "\x1d": "group separator (Python only)",
    "\x1e": "record separator (Python only)",
    "\x1f": "unit separator (Python only)",
}


@pytest.mark.parametrize("char,why", list(DIVERGENT.items()), ids=list(DIVERGENT.values()))
def test_an_invisible_character_on_a_blank_line_still_ends_the_paragraph(char, why):
    assert [b.text for b in split_blocks(f"a\n{char}\nb")] == ["a", "b"], why


def test_the_same_character_inside_a_line_is_content_not_a_break():
    """Only a whole blank LINE separates paragraphs. Widening the class must not start
    splitting mid-sentence on a stray control character."""
    assert [b.text for b in split_blocks("a\x1cb\n\nc")] == ["a\x1cb", "c"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

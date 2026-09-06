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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

"""The browser and the server must render a chapter to the same HTML.

The reader's Copy button renders in ``web/src/mdhtml.js``; the posting payload renders in
``translation_bot/mdhtml.py``. If the two drift, then what the user sees when they copy a
chapter stops matching what actually gets published — and the difference shows up on a
live page, after the fact, rather than anywhere they could have checked.

Nothing else in the suite shells out, so this skips cleanly when Node isn't on PATH; it is
a drift alarm, not a build requirement. Same shape as
``tests/test_paragraph_split_parity.py``.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_mdhtml_parity.py``).
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from translation_bot.mdhtml import markdown_to_html, strip_leading_heading  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MDHTML_JS = os.path.join(REPO, "web", "src", "mdhtml.js")

# Shapes that occur in these chapters, plus the ones most likely to expose a difference
# between two regex engines.
CASES = [
    "one\n\ntwo\n\nthree",
    "one\n\ntwo\n\nthree\n",                      # chapters always end in a newline
    "",
    "   ",
    "single",
    # emphasis, including the two forms the Python side used to get wrong
    "a *b* c",
    "a **b** c",
    "a ***b*** c",
    "a _b_ c",
    "snake_case_word stays put",
    "a `code` b",
    "***",
    "---",
    "- - -",                                      # the looser rule spelling
    # escaping: & < > must be escaped, apostrophes must NOT be (see mdhtml.py::_inline)
    "5 < 6 & 7 > 4",
    "I'll not \"quote\" it",
    "“Smart quotes,” he said — and an em dash.",
    # headings and quotes
    "# Title\n\nbody",
    "###### deep\n\nbody",
    "####### seven hashes is not a heading",
    "> quoted\n> second line",
    ">nospace",
    # hard line breaks inside one paragraph
    "line one\nline two\n\nnext",
    # the author's own part markers, live in 7 real novels
    "text\n\n33.\n\nmore",
    "29\n\nbody",
    # Korean, which is most of the source pane
    "그는 문을 열었다.\n\n밖에는 아무도 없었다.",
    # the six characters where \s itself differs between the two engines
    "a\n\x85\nb",
    "a\n\x1c\nb",
    "a\n\x1d\nb",
    "a\n\x1e\nb",
    "a\n\x1f\nb",
    "a\n﻿\nb",
    "﻿one\n\ntwo",
    "a\n ﻿ \nb",
]

HEADING_CASES = [
    "# 12\n\nbody",
    "﻿# 12\n\nbody",
    "body with no heading",
    "#nospace is not a heading",
    "## Two\n\n# One",
]

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is not installed")


def _js(fn_body: str, cases: list[str]) -> list[str]:
    url = json.dumps("file:///" + MDHTML_JS.replace(os.sep, "/"))
    script = (
        f"import {{ markdownToHtml, stripLeadingHeading }} from {url}\n"
        f"const cases = {json.dumps(cases)}\n"
        f"console.log(JSON.stringify(cases.map((c) => {fn_body})))\n"
    )
    result = subprocess.run([_NODE, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def test_the_two_renderers_agree_exactly():
    for text, js in zip(CASES, _js("markdownToHtml(c)", CASES)):
        py = markdown_to_html(text)
        assert py == js, (
            f"mdhtml.js and mdhtml.py disagree on {text!r}\n  js: {js!r}\n  py: {py!r}\n"
            f"— the Copy button and the posting payload would send different HTML"
        )


def test_they_agree_with_part_markers_stripped():
    js = _js("markdownToHtml(c, { stripPartMarkers: true })", CASES)
    for text, expected in zip(CASES, js):
        assert markdown_to_html(text, strip_part_markers=True) == expected, text


def test_they_agree_on_stripping_a_leading_heading():
    js = _js("stripLeadingHeading(c)", HEADING_CASES)
    for text, expected in zip(HEADING_CASES, js):
        assert strip_leading_heading(text) == expected, text


# ---- what the output has to BE, not just that both agree ------------------
#
# Agreement alone is not enough: both sides emitting the same wrong thing would pass.


def test_a_paragraph_becomes_one_p_with_no_attributes():
    # An attribute-free sequence of blocks is what makes this paste cleanly into a
    # ProseMirror/TipTap editor with its default schema and no custom parse rules.
    out = markdown_to_html("one\n\ntwo")
    assert out == "<p>one</p>\n<p>two</p>"
    assert "class=" not in out and "style=" not in out


def test_part_markers_are_kept_by_default_and_dropped_for_posting():
    # 44 of these exist across 7 novels, 21 at the very top of a chapter. They are
    # invisible in the reader, so publishing them would show a number never seen before.
    assert markdown_to_html("33.\n\nbody") == "<p>33.</p>\n<p>body</p>"
    assert markdown_to_html("33.\n\nbody", strip_part_markers=True) == "<p>body</p>"


def test_a_number_that_is_part_of_a_sentence_is_never_stripped():
    assert markdown_to_html("33. And then he left.", strip_part_markers=True) == (
        "<p>33. And then he left.</p>"
    )


def test_apostrophes_are_left_alone_but_angle_brackets_are_escaped():
    assert markdown_to_html("I'll go") == "<p>I'll go</p>"
    assert markdown_to_html("a < b") == "<p>a &lt; b</p>"


def test_the_epub_gets_closed_void_tags():
    # The EPUB body must be valid XML; the web editors want the HTML spelling.
    assert markdown_to_html("a\nb", xhtml=True) == "<p>a<br/>b</p>"
    assert markdown_to_html("a\nb") == "<p>a<br>b</p>"
    assert markdown_to_html("***", xhtml=True) == "<hr/>"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

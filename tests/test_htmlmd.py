"""Tests for HTML back to Markdown — the import direction.

A novel already published on the site arrives as HTML and has to become the Markdown Night
Reader stores. The strongest available check is a round trip, and the direction matters:

    html -> markdown -> html      must reproduce the html

not the other way round. Both ``***`` and ``---`` legitimately render to ``<hr>``, and
``**a**`` and ``__a__`` would both give ``<strong>``, so Markdown is not recoverable
character-for-character — but the *meaning* is, and that is what has to survive. Asserting
the wrong direction would fail on correct behaviour.

The corpus is the one ``tests/test_mdhtml_parity.py`` already uses: 35 fixtures of real
chapter shapes, including the six characters where ``\\s`` differs between Python and
JavaScript. Reusing it means the two converters pin each other over real prose rather than
over cases invented to pass.

Runs under pytest (``pytest tests/``) and standalone (``python tests/test_htmlmd.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from translation_bot.htmlmd import html_to_markdown, round_trips  # noqa: E402
from translation_bot.mdhtml import markdown_to_html  # noqa: E402

from test_mdhtml_parity import CASES  # noqa: E402

# Prose that Markdown itself would read as markup. There is no escape hatch --
# markdown_to_html has no backslash support -- so the honest behaviour is to convert it,
# notice, and say so.
#
# Note these are constructed from HTML, not from Markdown. The first version of this test
# used the ">nospace" fixture and expected it to be ambiguous, which was wrong:
# markdown_to_html turns that into a blockquote, so it is a quote on both sides and round
# trips perfectly. The ambiguity only exists when a LITERAL > or * or # sits in prose,
# which in HTML means an escaped entity or a plain character.
AMBIGUOUS_HTML = [
    "<p>&gt;quoted looking, but prose</p>",
    "<p>*not emphasis, just asterisks*</p>",
    "<p># not a heading either</p>",
]


@pytest.mark.parametrize("md", CASES)
def test_the_meaning_survives_a_round_trip(md):
    html = markdown_to_html(md)
    ok, again = round_trips(html)
    assert ok, f"{html!r} came back as {again!r}"


@pytest.mark.parametrize("html", AMBIGUOUS_HTML)
def test_prose_markdown_would_reinterpret_is_reported_not_hidden(html):
    # Nothing can represent these faithfully, so the requirement is that the importer can
    # find out. Silently changing a chapter is the only unacceptable outcome.
    ok, _ = round_trips(html)
    assert ok is False
    # And the text itself is never mangled -- only its interpretation is at risk.
    md, _ = html_to_markdown(html)
    assert md.strip()


# ---- the markup a real novel actually carries ------------------------------

# Measured off the site: p, em, hr and a bare attribute-less span, no entities at all.
REAL = ('<p>She lifted her brows at the sudden movement.</p><p>“Why? Did you feel '
        'something?”</p><p>A hint of <em>expectation</em> shone there — and then '
        'it was gone…</p><hr><p><span>He was used to hiding.</span></p>')


def test_a_real_chapter_converts_cleanly():
    md, losses = html_to_markdown(REAL)
    assert losses == []
    assert md.split("\n\n") == [
        "She lifted her brows at the sudden movement.",
        "“Why? Did you feel something?”",
        "A hint of *expectation* shone there — and then it was gone…",
        "---",
        "He was used to hiding.",
    ]
    assert round_trips(REAL)[0]


def test_curly_quotes_and_dashes_are_left_exactly_alone():
    # pipeline.py is explicit that nothing in this app normalises smart quotes, and the
    # published text is the authority — "fixing" punctuation would edit what readers saw.
    md, _ = html_to_markdown('<p>“don’t” — he said…</p>')
    assert md == "“don’t” — he said…"


def test_a_bare_span_is_unwrapped_without_complaint():
    # An attribute-less span is what the site's editor leaves behind and means nothing.
    md, losses = html_to_markdown("<p>one <span>two</span> three</p>")
    assert md == "one two three" and losses == []


def test_a_line_break_stays_inside_its_paragraph():
    # markdown_to_html turns a newline inside a block into <br>, so this is its exact
    # inverse. Making it a paragraph break instead would silently double the paragraph
    # count, which is the thing validate.py measures.
    md, _ = html_to_markdown("<p>line one<br>line two</p>")
    assert md == "line one\nline two"
    assert markdown_to_html(md) == "<p>line one<br>line two</p>"


def test_a_non_breaking_space_becomes_an_ordinary_one():
    md, _ = html_to_markdown("<p>one&nbsp;two</p>")
    assert md == "one two"


def test_entities_arrive_decoded_and_are_re_escaped_on_the_way_out():
    md, _ = html_to_markdown("<p>5 &lt; 6 &amp; 7 &gt; 4</p>")
    assert md == "5 < 6 & 7 > 4"
    assert markdown_to_html(md) == "<p>5 &lt; 6 &amp; 7 &gt; 4</p>"


def test_headings_and_quotes_come_back():
    assert html_to_markdown("<h3>Title</h3>")[0] == "### Title"
    assert html_to_markdown(
        "<blockquote><p>quoted<br>second</p></blockquote>")[0] == "> quoted\n> second"


def test_emphasis_in_both_spellings():
    assert html_to_markdown("<p><strong>a</strong> <b>b</b></p>")[0] == "**a** **b**"
    assert html_to_markdown("<p><em>a</em> <i>b</i></p>")[0] == "*a* *b*"
    assert html_to_markdown("<p><strong><em>a</em></strong></p>")[0] == "***a***"


# ---- never corrupt prose --------------------------------------------------


def test_what_cannot_be_represented_is_named_not_dropped():
    md, losses = html_to_markdown(
        "<p>before <u>under</u> <s>struck</s> <code>coded</code> after</p>")
    # The text survives in full; only the formatting is gone.
    assert md == "before under struck coded after"
    assert losses == ["code formatting", "strikethrough", "underline"]


def test_an_image_or_a_table_is_reported():
    _, losses = html_to_markdown("<p>text</p><img src='x.png'><table><tr><td>a</td></tr></table>")
    assert "an image" in losses and "a table" in losses


def test_a_link_keeps_its_words():
    # There is no link spelling in this Markdown subset, so emitting one would render as
    # literal brackets. The words are what matter in prose.
    md, _ = html_to_markdown('<p>see <a href="http://x">the note</a> here</p>')
    assert md == "see the note here"


def test_an_unknown_tag_keeps_its_text():
    md, losses = html_to_markdown("<p>a <weird-thing>b</weird-thing> c</p>")
    assert md == "a b c" and losses == []


def test_a_list_becomes_visible_paragraphs_rather_than_vanishing():
    md, _ = html_to_markdown("<ul><li>first</li><li>second</li></ul>")
    assert md == "- first\n\n- second"


def test_script_and_style_are_discarded_content_and_all():
    md, _ = html_to_markdown("<p>keep</p><script>var x = 1</script><style>p{color:red}</style>")
    assert md == "keep"


def test_unclosed_emphasis_is_closed_rather_than_left_dangling():
    # An editor that opens an <em> and never closes it would otherwise leave a lone
    # marker in the prose. Refusing to parse somebody else's markup is not an option,
    # so the block closes what it opened.
    md, _ = html_to_markdown("<p>one<p>two<em>three")
    assert md.split(chr(10) + chr(10)) == ["one", "two*three*"]


def test_a_stray_closing_tag_never_invents_emphasis():
    md, _ = html_to_markdown("<p>a</em>b")
    assert md == "ab"


def test_nothing_in_gives_nothing_out():
    assert html_to_markdown("") == ("", [])
    assert html_to_markdown("   ") == ("", [])
    assert html_to_markdown(None) == ("", [])
    assert html_to_markdown("<p></p><p>   </p>") == ("", [])


def test_text_with_no_tags_at_all_is_still_a_paragraph():
    assert html_to_markdown("just words")[0] == "just words"


def test_paragraph_count_is_preserved_which_is_what_validation_measures():
    # validate.py compares the paragraph count against the source. An import writes the
    # same text on both sides, so a converter that merged or split blocks would make every
    # imported chapter look wrong.
    html = "".join(f"<p>paragraph {n}</p>" for n in range(1, 13))
    md, _ = html_to_markdown(html)
    assert len(md.split("\n\n")) == 12


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

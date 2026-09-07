"""Tests for the deterministic page-seam rules (Tier 1 of OCR stitching).

Photographing a print book, a sentence routinely continues across the page break.
If each page is split into paragraphs independently, every one of those seams becomes
a false paragraph break — and the translator preserves paragraph structure, so the
break survives into the English.

What has to hold:
- A sentence running across the seam joins with NO blank line. This is the whole point.
- A chapter heading on the next page wins over whatever the previous page looked like.
- An unclosed quotation at a page end means the speech continues.
- A clean sentence end followed by a clean start is a real paragraph break.
- Genuinely ambiguous seams are reported as low-confidence so a model call is spent
  only where it buys something.
- ``propose_join`` never claims a gap — only the model can spot a missing page.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_ocr_join.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.ocr_join import (  # noqa: E402
    LOW_CONFIDENCE,
    ends_sentence,
    has_unclosed_quote,
    join_text,
    looks_like_heading,
    propose_join,
)


# ---- the primitives ----------------------------------------------------------

def test_ends_sentence_sees_through_closing_marks():
    assert ends_sentence("그는 말했다."), "a plain full stop ends a sentence"
    assert ends_sentence("「가자.」"), "a terminator behind a closing bracket still counts"
    assert ends_sentence('"정말이야?"'), "a question mark behind a straight quote counts"
    assert ends_sentence("뭐라고…"), "an ellipsis ends a sentence"
    assert not ends_sentence("그는 천천히 문을"), "a dangling clause does not end a sentence"
    assert not ends_sentence(""), "empty text ends nothing"


def test_unclosed_quote_detection():
    assert has_unclosed_quote("「그게 무슨 말이야"), "an opened corner bracket is still open"
    assert not has_unclosed_quote("「그게 무슨 말이야」"), "a closed pair is closed"
    assert has_unclosed_quote('"어디 가는 거야'), "odd straight quotes mean an open quote"
    assert not has_unclosed_quote('"어디 가?" 그가 물었다.'), "an even count is closed"


def test_heading_detection_requires_a_short_standalone_line():
    assert looks_like_heading("제3화 문이 열렸다"), "a Korean chapter heading is a heading"
    assert looks_like_heading("프롤로그"), "a prologue marker is a heading"
    assert looks_like_heading("Chapter 12"), "an English chapter heading is a heading"
    long_para = "3화를 읽고 나서 그는 " + "생각에 잠겼다 " * 20
    assert not looks_like_heading(long_para), \
        "a paragraph that merely opens with a chapter-like token is not a heading"


# ---- the seam rules ----------------------------------------------------------

def test_unfinished_sentence_joins_without_a_paragraph_break():
    """The core case: a print page ends mid-sentence."""
    join = propose_join(
        "그는 천천히 문을", "열고 안으로 들어갔다.",
        {"ends_mid_sentence": True}, {"starts_mid_sentence": True})
    assert join.kind == "sentence", "an unfinished sentence must not become a paragraph break"
    assert not join.needs_model, "this seam is certain enough to decide for free"


def test_heading_on_the_next_page_starts_a_chapter():
    join = propose_join("…그렇게 하루가 끝났다.", "제4화 새로운 아침",
                        {"ends_mid_sentence": False}, {})
    assert join.kind == "chapter", "a heading on the next page starts a chapter"


def test_heading_wins_even_when_the_sentence_looks_unfinished():
    """OCR sometimes drops the final punctuation; a heading is the stronger signal."""
    join = propose_join("그는 문을 열었다", "제5화 재회",
                        {"ends_mid_sentence": True}, {"starts_mid_sentence": True})
    assert join.kind == "chapter", "a chapter heading outranks an apparently open sentence"


def test_open_quotation_continues_across_the_seam():
    join = propose_join("「내가 그때 말했잖아", "그러니까 이제 그만해.」", {}, {})
    assert join.kind == "sentence", "speech left open at a page end continues"
    assert not join.needs_model, "an unclosed quote is a confident signal"


def test_open_quotation_does_not_override_a_new_quote():
    """If the next page opens its own quotation, this is new dialogue, not a run-on."""
    join = propose_join("「내가 그때 말했잖아", "「무슨 소리야?」", {}, {})
    assert join.kind != "sentence", "a fresh opening quote is not a continuation"


def test_clean_sentence_end_and_clean_start_is_a_paragraph():
    join = propose_join("그는 조용히 문을 닫았다.", "다음 날 아침이 밝았다.", {}, {})
    assert join.kind == "paragraph", "two complete sentences are separate paragraphs"
    assert not join.needs_model, "this seam is clear enough to decide for free"


def test_missing_terminator_implies_the_sentence_runs_on():
    """No page metadata at all — the text alone still settles it."""
    join = propose_join("그는 천천히 문을", "열었다.", {}, {})
    assert join.kind == "sentence", "a page ending without a terminator continues"


def test_a_blank_page_seam_is_not_guessed_at():
    join = propose_join("", "다음 날 아침이 밝았다.", {}, {})
    assert join.needs_model, "a seam next to an empty page cannot be decided from text"


def test_never_claims_a_gap():
    """A missing page can only be spotted by reading both sides — the model's job."""
    for prev, nxt in (("그는 말했다.", "전혀 다른 이야기였다."),
                      ("문을 열고", "그녀는 웃었다."),
                      ("", "")):
        assert propose_join(prev, nxt, {}, {}).kind != "gap", \
            "Tier 1 must never claim a gap it cannot actually detect"


# `needs_model` IS `confidence < LOW_CONFIDENCE` — a property whose whole body is that
# comparison. Asserting the two against each other is a tautology that no change can
# break, and it hid the fact that its own fixture ("그는 말했다." / "…") scores 0.65,
# which is not a low-confidence seam at all. What matters is that the threshold cuts
# in the right place on BOTH sides, so these assert outcomes instead.

def test_an_ambiguous_seam_is_escalated_to_the_model():
    """A quotation opening after a page that never finished its sentence is genuinely
    two-ways: the quote may be the object of the running sentence ("그가 조용히" /
    "「가자」고 말했다"), or a fresh line of speech.

    If this stopped being escalated it would silently keep its deterministic guess —
    a sentence that ran across the page break becomes a paragraph break, the
    translator faithfully preserves it, and the English gains a break that is not in
    the book.
    """
    join = propose_join("그가 조용히", "「가자」고 말했다.", {}, {})
    assert join.confidence < LOW_CONFIDENCE
    assert join.needs_model is True


def test_a_confident_seam_is_decided_without_the_model():
    """The threshold has to cut both ways. If a clear seam also asked the model,
    /pages/stitch would spend a call on every boundary in the novel."""
    join = propose_join("그는 천천히 문을 열", "고 안으로 들어갔다.",
                        {"ends_mid_sentence": True, "ends_mid_word": True},
                        {"starts_mid_sentence": True})
    assert join.confidence >= LOW_CONFIDENCE
    assert join.needs_model is False


def test_every_escalating_rule_actually_scores_below_the_threshold():
    """The three paths that exist to reach the model. A tuning change that lifted any
    of them above 0.6 would stop escalating it, with no test failing."""
    escalating = [
        ("", "다음 날 아침이 밝았다.", {}, {}),                    # a blank page
        ("그가 조용히", "「가자」고 말했다.", {}, {}),               # a fresh quotation
    ]
    for prev, nxt, m1, m2 in escalating:
        join = propose_join(prev, nxt, m1, m2)
        assert join.needs_model is True, f"{join.reason!r} scored {join.confidence}"


# ---- glue --------------------------------------------------------------------

def test_mid_word_break_joins_with_no_space():
    join = propose_join("그는 천천히 문을 열", "고 안으로 들어갔다.",
                        {"ends_mid_sentence": True, "ends_mid_word": True},
                        {"starts_mid_sentence": True})
    assert join.glue == "none", "a word split across pages must rejoin with no space"


def test_word_boundary_break_joins_with_a_space():
    join = propose_join("그는 천천히 문을", "열고 안으로 들어갔다.",
                        {"ends_mid_sentence": True, "ends_mid_word": False},
                        {"starts_mid_sentence": True})
    assert join.glue == "space", "a break between words keeps the space"


def test_trailing_hyphen_implies_a_split_word():
    join = propose_join("the corri-", "dor was empty.",
                        {"ends_mid_sentence": True}, {"starts_mid_sentence": True})
    assert join.glue == "none", "a trailing hyphen means the word was split"


# ---- applying the decision ---------------------------------------------------

def test_join_text_sentence_makes_no_blank_line():
    out = join_text("그는 천천히 문을", "열었다.", "sentence", "space")
    assert out == "그는 천천히 문을 열었다.", "a sentence join is seamless"
    assert "\n" not in out, "a sentence join must never introduce a line break"


def test_join_text_sentence_with_no_glue():
    assert join_text("문을 열", "고 나갔다.", "sentence", "none") == "문을 열고 나갔다.", \
        "a mid-word join inserts nothing"


def test_join_text_paragraph_makes_exactly_one_blank_line():
    out = join_text("첫 문단이다.", "둘째 문단이다.", "paragraph")
    assert out == "첫 문단이다.\n\n둘째 문단이다.", "a paragraph join is one blank line"


def test_join_text_tolerates_an_empty_side():
    assert join_text("", "본문", "paragraph") == "본문", "an empty page contributes nothing"
    assert join_text("본문", "", "sentence") == "본문", "an empty page adds no separator"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

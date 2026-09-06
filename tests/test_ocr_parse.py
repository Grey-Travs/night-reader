"""Tests for parsing what the model returns from an OCR call.

These parsers are the fragile seam between a model's free-form reply and stored
source text, so they follow the same discipline as ``translator.parse_response``: a
malformed reply degrades into a usable result rather than raising. A page we
transcribed but could not classify is still worth keeping — the UI flags it.

What has to hold:
- The Korean transcription survives sanitization intact. The page text is entirely
  Korean, and the leak-stripper must never mistake it for an untranslated echo.
- A missing or broken metadata block never loses the page text.
- An empty transcription (cover, blank page, illustration) is a valid outcome and
  never looks confident.
- A verify issue whose anchor is not in the transcription verbatim cannot be applied
  automatically — it is kept for the human but its anchor is cleared.
- Stitch decisions are clamped to the boundaries actually asked about.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_ocr_parse.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.ocr import (  # noqa: E402
    format_seams,
    parse_page_response,
    parse_stitch_response,
    parse_verify_response,
    split_page_meta,
)
from translation_bot.prompts import PAGE_META_DELIMITER  # noqa: E402

KO = "그는 천천히 문을 열었다.\n\n밖에는 아무도 없었다."

META = ('{"confidence": "high", "heading": null, "starts_mid_sentence": false, '
        '"ends_mid_sentence": true, "ends_mid_word": false, "notes": []}')


def _reply(text: str, meta: str) -> str:
    return f"{text}\n{PAGE_META_DELIMITER}\n{meta}"


# ---- splitting ---------------------------------------------------------------

def test_split_separates_text_from_metadata():
    text, meta = split_page_meta(_reply(KO, META))
    assert text == KO, "the page text must come back exactly as transcribed"
    assert meta["confidence"] == "high", "the metadata object must parse"


def test_split_without_a_delimiter_keeps_everything_as_text():
    text, meta = split_page_meta(KO)
    assert text == KO, "a reply with no metadata block is still a page"
    assert meta is None, "no metadata means no metadata — not a guess"


def test_split_does_not_mistake_prose_braces_for_metadata():
    prose = "그는 말했다. {이건 본문이다} 그리고 걸었다."
    text, meta = split_page_meta(prose)
    assert text == prose, "braces inside prose must not be torn off as metadata"
    assert meta is None


# ---- the page ----------------------------------------------------------------

def test_korean_transcription_survives_sanitization():
    """The load-bearing case: the leak-stripper must not eat an all-Korean page."""
    page = parse_page_response(_reply(KO, META))
    assert page.text == KO, "an all-Korean page must pass through untouched"


def test_english_preamble_is_stripped_but_the_page_is_kept():
    page = parse_page_response(_reply("Here is the translation:\n\n" + KO, META))
    assert page.text == KO, "a model preamble must never reach the stored source"


def test_metadata_fields_are_read():
    page = parse_page_response(_reply(KO, META))
    assert page.confidence == "high"
    assert page.ends_mid_sentence is True, "the seam booleans drive chapter stitching"
    assert page.starts_mid_sentence is False
    assert page.ends_mid_word is False
    assert page.heading is None


def test_heading_is_captured():
    meta = META.replace('"heading": null', '"heading": "제3화 문이 열렸다"')
    page = parse_page_response(_reply("제3화 문이 열렸다\n\n" + KO, meta))
    assert page.heading == "제3화 문이 열렸다", "an in-story heading must be reported"


def test_missing_metadata_degrades_but_keeps_the_text():
    page = parse_page_response(KO)
    assert page.text == KO, "losing the metadata must never lose the page"
    assert page.confidence == "low", "an unassessed page must not look confident"
    assert page.notes, "the reason must be visible to the reader"


def test_broken_metadata_json_degrades_but_keeps_the_text():
    page = parse_page_response(_reply(KO, '{"confidence": "high", oops'))
    assert page.text == KO, "malformed metadata must never lose the page"
    assert page.confidence == "low"


def test_unknown_confidence_value_falls_back_to_low():
    page = parse_page_response(_reply(KO, '{"confidence": "excellent"}'))
    assert page.confidence == "low", "only high/medium/low are trusted"


def test_empty_page_is_valid_but_never_confident():
    page = parse_page_response(_reply("", '{"confidence": "high", "notes": ["blank page"]}'))
    assert page.text == "", "a blank page transcribes to nothing"
    assert page.confidence == "low", "an empty transcription must be flagged, not trusted"
    assert page.notes == ["blank page"]


def test_string_booleans_are_tolerated():
    page = parse_page_response(_reply(KO, '{"ends_mid_sentence": "true"}'))
    assert page.ends_mid_sentence is True, "a stringified boolean must still be read"


def test_notes_given_as_a_bare_string_are_accepted():
    page = parse_page_response(_reply(KO, '{"notes": "glare on the last line"}'))
    assert page.notes == ["glare on the last line"]


# ---- verification ------------------------------------------------------------

def test_clean_page_verifies_as_ok():
    result = parse_verify_response("[]", KO)
    assert result.verdict == "ok"
    assert result.issues == []


def test_issue_anchored_in_the_transcription_is_applicable():
    raw = ('[{"kind": "wrong", "where": "밖에는 아무도 없었다.", '
           '"page_says": "밖에는 아무도 없었다!", "suggest": "밖에는 아무도 없었다!"}]')
    result = parse_verify_response(raw, KO)
    assert result.verdict == "discrepancies"
    assert result.issues[0].where == "밖에는 아무도 없었다.", "a verbatim anchor is kept"


def test_unanchored_issue_is_kept_but_not_applicable():
    """Never fuzzy-match a replacement into the user's source text."""
    raw = ('[{"kind": "missing", "where": "이 문장은 전사본에 없다", '
           '"page_says": "새로운 줄", "suggest": "새로운 줄"}]')
    result = parse_verify_response(raw, KO)
    assert len(result.issues) == 1, "the human still needs to see the discrepancy"
    assert result.issues[0].where == "", "an anchor that isn't present must be cleared"


def test_unknown_issue_kind_is_normalized():
    raw = '[{"kind": "typo", "where": "", "page_says": "x", "suggest": "y"}]'
    assert parse_verify_response(raw, KO).issues[0].kind == "wrong"


def test_unparseable_verify_reply_is_treated_as_ok():
    """A broken proof-read must not invent problems with the page."""
    assert parse_verify_response("I could not read the image.", KO).verdict == "ok"


# ---- stitching ---------------------------------------------------------------

def test_stitch_decisions_are_read_in_order():
    raw = ('[{"i": 1, "join": "sentence", "glue": "none"}, '
           '{"i": 2, "join": "chapter", "glue": "space", "note": "new arc"}]')
    out = parse_stitch_response(raw, 2)
    assert [d.i for d in out] == [1, 2]
    assert out[0].join == "sentence" and out[0].glue == "none"
    assert out[1].join == "chapter"


def test_gap_is_a_valid_decision():
    """Only the model can spot a page that was never photographed."""
    out = parse_stitch_response('[{"i": 1, "join": "gap", "note": "scene jumps"}]', 1)
    assert out[0].join == "gap", "a missing page must survive parsing"


def test_out_of_range_and_duplicate_boundaries_are_dropped():
    raw = ('[{"i": 1, "join": "sentence"}, {"i": 1, "join": "chapter"}, '
           '{"i": 9, "join": "gap"}, {"i": 0, "join": "gap"}]')
    out = parse_stitch_response(raw, 2)
    assert [d.i for d in out] == [1], "only the first decision for a real boundary counts"


def test_invalid_join_kind_is_dropped_not_guessed():
    out = parse_stitch_response('[{"i": 1, "join": "maybe"}]', 1)
    assert out == [], "an unrecognised join must fall back to the deterministic guess"


def test_invalid_glue_defaults_to_space():
    out = parse_stitch_response('[{"i": 1, "join": "sentence", "glue": "wat"}]', 1)
    assert out[0].glue == "space"


def test_unparseable_stitch_reply_changes_nothing():
    assert parse_stitch_response("sorry", 3) == [], \
        "a broken reply must leave every seam on its deterministic guess"


def test_format_seams_numbers_and_trims_each_boundary():
    text = format_seams([("a" * 500, "b" * 500)], seam_chars=50)
    assert "Boundary 1" in text
    assert "a" * 50 in text and "a" * 51 not in text, "page ends are trimmed to the window"
    assert "b" * 50 in text and "b" * 51 not in text, "page starts are trimmed to the window"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

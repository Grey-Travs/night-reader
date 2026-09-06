"""Tests for addressing, aligning and replacing a single translated paragraph.

A translated chapter is one Markdown file, and that file stays the source of truth for
export, EPUB, search, the consistency scan, previous/ and whole-chapter edits. So a
paragraph carries no stored id: it is addressed by ordinal, PROVED by its exact text,
and replaced by character span.

What has to hold:
- A splice is lossless. Replacing a paragraph with itself must return the file
  byte-for-byte, including its trailing newline and any odd spacing.
- A splice can never change the chapter's paragraph count. That single property is
  what keeps the validator's paragraph check, every other paragraph's address, and
  the EPUB structure intact.
- An address that no longer matches resolves to None, so the caller can answer 409
  instead of overwriting the wrong paragraph.
- Alignment returns a WINDOW, never a bare point — and admits low confidence rather
  than guessing.
- The result check rejects a reply that is two paragraphs, or commentary, or leaks
  Korean — but only WARNS about a dropped name, since a good rephrase may
  legitimately pronominalize one.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_paragraphs.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from translation_bot.glossary import GlossaryEntry  # noqa: E402
from translation_bot.paragraphs import (  # noqa: E402
    align_korean,
    block_texts,
    check_paragraph_result,
    korean_window,
    locate_block,
    normalize_paragraph,
    split_blocks,
    splice_block,
)

CHAPTER = (
    "The door slid open.\n\n"
    "“Are you coming?” she asked.\n\n"
    "He said nothing for a long moment.\n"
)


# ---- splitting ---------------------------------------------------------------

def test_blocks_carry_exact_spans():
    blocks = split_blocks(CHAPTER)
    assert len(blocks) == 3
    for block in blocks:
        assert CHAPTER[block.start:block.end] == block.text, \
            "a block's span must reproduce its text exactly"


def test_blank_and_whitespace_only_blocks_are_dropped():
    assert len(split_blocks("one\n\n   \n\ntwo")) == 2


def test_empty_text_has_no_blocks():
    assert split_blocks("") == [] and split_blocks(None) == []


def test_a_horizontal_rule_is_its_own_block():
    """The reader must not offer a rewrite handle on one, so it has to be visible."""
    assert block_texts("a\n\n***\n\nb") == ["a", "***", "b"]


def test_a_bare_part_marker_survives_as_a_block():
    """The prompt explicitly preserves standalone number lines like `33.`."""
    assert block_texts("text\n\n33.\n\nmore") == ["text", "33.", "more"]


# ---- splicing ----------------------------------------------------------------

def test_replacing_a_paragraph_with_itself_is_byte_identical():
    for k in range(3):
        assert splice_block(CHAPTER, k, block_texts(CHAPTER)[k]) == CHAPTER, \
            "a no-op splice must not perturb the file at all"


def test_splice_replaces_only_the_target():
    out = splice_block(CHAPTER, 1, "“Well?” she said.")
    assert block_texts(out) == [
        "The door slid open.", "“Well?” she said.", "He said nothing for a long moment."]


def test_splice_preserves_the_trailing_newline():
    assert splice_block(CHAPTER, 0, "The door opened.").endswith("moment.\n")


def test_splice_preserves_unusual_separators():
    weird = "one\n   \ntwo\n\n\n\nthree"
    out = splice_block(weird, 1, "TWO")
    assert out == "one\n   \nTWO\n\n\n\nthree", "separator characters are untouched"


def test_splice_refuses_a_two_paragraph_replacement():
    """Silently joining them would change the meaning and break the count invariant."""
    with pytest.raises(ValueError):
        splice_block(CHAPTER, 0, "First half.\n\nSecond half.")


def test_splice_refuses_an_empty_replacement():
    with pytest.raises(ValueError):
        splice_block(CHAPTER, 0, "   ")


def test_splice_refuses_an_out_of_range_paragraph():
    with pytest.raises(ValueError):
        splice_block(CHAPTER, 9, "text")


def test_splice_never_changes_the_paragraph_count():
    """The property the validator, the addresses and the EPUB all depend on."""
    before = len(split_blocks(CHAPTER))
    for k in range(before):
        assert len(split_blocks(splice_block(CHAPTER, k, "Replaced."))) == before


def test_a_replacement_with_a_line_break_stays_one_paragraph():
    out = splice_block(CHAPTER, 0, "Line one.\nLine two.")
    assert len(split_blocks(out)) == 3, "a single newline is not a paragraph break"


# ---- addressing --------------------------------------------------------------

def test_the_ordinal_wins_when_the_text_matches():
    blocks = split_blocks(CHAPTER)
    assert locate_block(blocks, "“Are you coming?” she asked.", 1) == 1


def test_a_moved_paragraph_is_still_found_when_unique():
    """An unrelated earlier edit shifts every ordinal; a unique match rescues it."""
    blocks = split_blocks(CHAPTER)
    assert locate_block(blocks, "He said nothing for a long moment.", 0) == 2


def test_a_changed_paragraph_resolves_to_nothing():
    blocks = split_blocks(CHAPTER)
    assert locate_block(blocks, "Something else entirely.", 1) is None, \
        "the caller must 409 rather than overwrite the wrong paragraph"


def test_an_ambiguous_address_resolves_to_nothing():
    doubled = "Same.\n\nMiddle.\n\nSame."
    assert locate_block(split_blocks(doubled), "Same.", 1) is None


def test_an_ambiguous_address_still_resolves_at_its_own_ordinal():
    doubled = "Same.\n\nMiddle.\n\nSame."
    assert locate_block(split_blocks(doubled), "Same.", 0) == 0


# ---- normalizing -------------------------------------------------------------

def test_normalize_collapses_blank_lines_to_one_paragraph():
    assert len(split_blocks(normalize_paragraph("a\n\nb"))) == 1


def test_normalize_trims_trailing_spaces():
    assert normalize_paragraph("line   \nnext") == "line\nnext"


# ---- alignment ---------------------------------------------------------------

def test_equal_counts_align_one_to_one():
    en = ["a", "b", "c"]
    ko = ["가", "나", "다"]
    a = align_korean(en, 1, ko)
    assert (a.index, a.method) == (1, "exact") and a.confidence == 1.0


def test_a_merged_paragraph_still_lands_near_the_right_place():
    en = [f"paragraph {i}" for i in range(10)]
    ko = [f"문단 {i}" for i in range(9)]
    a = align_korean(en, 5, ko)
    assert 3 <= a.index <= 7, "an approximate anchor must still be in the neighbourhood"


def test_dialogue_parity_anchors_across_a_count_mismatch():
    """Counts must differ, or the exact 1:1 path handles it before this ever runs."""
    en = ['narration', '"one"', 'narration', '"two"', 'narration', '"three"', 'tail']
    ko = ['서술', '서술', '「하나」', '서술', '「둘」', '서술', '서술', '「셋」']
    assert len(en) != len(ko), "this fixture only exercises alignment when counts differ"
    a = align_korean(en, 5, ko)
    assert a.method == "dialogue"
    assert '셋' in ko[a.index], "the third quoted line must map to the third quoted line"


def test_no_korean_means_no_alignment():
    a = align_korean(["a"], 0, [])
    assert a.index == -1 and a.confidence == 0.0, \
        "retranslate must be refused, not guessed at"


def test_a_wildly_different_count_reports_low_confidence():
    a = align_korean([f"p{i}" for i in range(40)], 20, ["단락"])
    assert a.confidence < 0.4, "the UI must be able to disable retranslate here"


def test_the_window_contains_the_target_and_says_where_it_is():
    ko = [f"문단 {i}" for i in range(10)]
    a = align_korean([f"p{i}" for i in range(10)], 5, ko)
    window, focus = korean_window(ko, a)
    assert window[focus] == ko[a.index], "focus must point at the target inside the window"
    assert len(window) > 1, "a window, never a bare point"


def test_the_window_is_clamped_at_the_edges():
    ko = ["가", "나", "다"]
    window, focus = korean_window(ko, align_korean(["a", "b", "c"], 0, ko))
    assert window[focus] == "가" and focus == 0


def test_no_alignment_yields_an_empty_window():
    assert korean_window([], align_korean(["a"], 0, [])) == ([], 0)


# ---- checking the reply ------------------------------------------------------

ORIGINAL = "The door slid open, and Yuna stepped through without looking back."


def test_a_clean_rewrite_is_accepted():
    result = check_paragraph_result(
        ORIGINAL, "The door slid open and Yuna walked through, never looking back.")
    assert result.ok and result.reasons == []


def test_two_paragraphs_are_rejected():
    result = check_paragraph_result(ORIGINAL, "First half.\n\nSecond half.")
    assert not result.ok
    assert any("paragraphs" in r for r in result.reasons)


def test_a_commentary_preamble_is_rejected():
    result = check_paragraph_result(ORIGINAL, "Sure! The door slid open again.")
    assert not result.ok


def test_leftover_korean_is_rejected():
    result = check_paragraph_result(ORIGINAL, "문이 천천히 열렸고 그녀는 걸어 나갔다.")
    assert not result.ok
    assert any("Korean" in r for r in result.reasons)


def test_an_emoticon_survives_a_rephrase():
    """A chat/SNS paragraph legitimately keeps ㅋㅋㅋ — the threshold is relative."""
    before = "she typed back ㅋㅋㅋ and put the phone down"
    result = check_paragraph_result(before, "She typed ㅋㅋㅋ back and set the phone down.")
    assert result.ok, result.reasons


def test_a_wildly_short_reply_is_rejected():
    assert not check_paragraph_result(ORIGINAL, "Open.").ok


def test_a_wildly_long_reply_is_rejected():
    assert not check_paragraph_result(ORIGINAL, "The door slid open. " * 40).ok


def test_new_markdown_structure_is_rejected():
    assert not check_paragraph_result(ORIGINAL, "# The door slid open").ok


def test_a_dropped_name_warns_but_is_accepted():
    """A good rephrase may turn a name into a pronoun — but the reader should see it."""
    glossary = [GlossaryEntry(korean="유나", english="Yuna", type="name")]
    result = check_paragraph_result(
        ORIGINAL, "The door slid open and she stepped through without looking back.",
        glossary=glossary)
    assert result.ok, "this must not be rejected"
    assert any("Yuna" in w for w in result.warnings), "but it must be flagged"


def test_an_identical_reply_is_marked_duplicate():
    result = check_paragraph_result(ORIGINAL, ORIGINAL)
    assert result.duplicate, "the panel says 'same text' rather than showing two cards"


def test_a_repeat_of_an_earlier_variant_is_marked_duplicate():
    earlier = "The door opened and Yuna went through."
    result = check_paragraph_result(ORIGINAL, earlier, prior=(earlier,))
    assert result.duplicate


def test_an_empty_reply_is_rejected_cleanly():
    result = check_paragraph_result(ORIGINAL, "   ")
    assert not result.ok and result.text == ""


def test_retranslate_allows_a_wider_length_band_than_rephrase():
    long_reply = "The heavy door slid slowly open, and Yuna stepped through it without ever once looking back over her shoulder at what she was leaving."
    assert check_paragraph_result(ORIGINAL, long_reply, mode="retranslate").ok


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

"""Regression tests for the prose-DELETING sanitizer and the Korean-leak detector.

These regexes remove text from real chapters, so the safety contract is three-sided:
  (1) genuine AI meta-commentary / mapping leaks that are pure junk ARE removed;
  (2) a leak marker riding on REAL translated English is FLAGGED for review, never
      silently deleted (deleting prose is not recoverable — losing a leak is);
  (3) legitimate prose, in-story dialogue, kept text-emoticons (ㅠㅠ/ㅋㅋ), and a single
      intentional short Korean sound-effect are NEVER removed or flagged.

Every leak string below is a real (or minimally paraphrased) example mined from the
project corpus (projects/*/chapters* and audit/). Every "keep" string is either a real
legitimate line from the corpus or a documented false-positive we must protect against.

Runs under pytest (``pytest tests/``) and standalone (``python tests/test_sanitize.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.sanitize import (  # noqa: E402
    find_leaks,
    has_korean_leak,
    remove_korean_echoes,
    strip_reasoning,
    strip_source_header,
)

# --- PURE-meta leaks: pure junk, safe to DELETE (no real prose to lose) -----------------
STRIPPABLE_META = [
    "Let me translate.",                      # bare standalone announcement (top leak pattern)
    "let me translate.",
    "Let me translate this chapter.",
    "Let me translate the full chapter.",
    "Let me translate the whole chapter.",
    "let me render the chapter.",
    "Let me translate the chapter content.",
    "역시 이상해 → Something's still off.",     # source -> target arrow mapping
    "형. 안녕. → He opened his mouth.",         # arrow mapping with Korean punctuation
    "The name 승연 = Seung Yeon.",              # name = spelling mapping
    "wait, the glossary says 고원 -> Go Won.",   # glossary chatter + mapping
    "The narrator here is Seungyeon.",          # narrator bookkeeping
    "Wait, let me reset the names. The narrator here is Seungyeon.",
]

# --- MIXED leaks: a marker riding on REAL translated English -> FLAG, do NOT delete ------
# (Korean-echo-plus-marker openers. The English between them is genuine translation.)
MIXED_FLAG_ONLY = [
    (
        "입술 사이로 파고든 도톰한 혀가 입 안을 훑는 감각이 선연하다. "
        "The thick tongue that pushed between my lips left a vivid sensation. Let me translate.",
        "The thick tongue that pushed between my lips left a vivid sensation.",
    ),
    (
        "숙소에 돌아온 한사원... let me translate.",
        None,  # no real English to protect — but still must be flagged, not silently passed
    ),
    (
        "분명 꺼림칙해야 할 텐데. Let me translate.",
        None,
    ),
]

# --- Things that MUST be kept (find_leaks empty AND strip_reasoning leaves text intact) --
LEGIT_KEEP = [
    "Let me translate their language for you, she offered with a small smile.",
    "“Let me redo my makeup,” she said, reaching for her compact.",
    "I'll write you a letter every week, he promised.",
    "In the original story, the villain dies in the third act.",
    "The original is far better than the film adaptation, he thought.",
    "She kept re-reading his last message, unsure what it meant.",
    "Wait, I need to think about this more carefully, he told himself.",
    "Hmm, that couldn't be right.",
    "He drew the link →→↓↔↗ on the whiteboard.",
]

# --- Legitimate short Korean the user may want kept -------------------------------------
LEGIT_KOREAN_KEEP = [
    "[Cha Narin: oppa are you doing okayyy ㅠㅠㅠㅠㅠ]",  # ㅠㅠ tears
    "I saw Han Sawon showing up there ㅋㅋㅋㅋㅋㅋ",   # ㅋㅋ laughter
    "**Say all you want, he's gonna keep killing it ㅇㅇ**",           # ㅇㅇ yep
    "ㅠㅠㅠㅠㅠㅠㅠㅠㅠㅠ",            # a whole line of tears
    "쿵! The sound echoed down the corridor.",                              # a single short SFX syllable
]

# --- Pure untranslated Korean sentences (source echoes) that MUST be removable ----------
KOREAN_ECHOES = [
    "박 기사는 거울 너머로 보이는 눈동자를 가만히 바라봤다.",
    "산다는 건 곧 버틴다는 의미다.",
]


def test_strippable_meta_is_detected():
    for s in STRIPPABLE_META:
        assert find_leaks(s), f"leak not detected: {s!r}"


def test_strippable_meta_is_removed_body_preserved():
    for s in STRIPPABLE_META:
        body = "This is the real translated chapter body that must survive."
        cleaned, removed = strip_reasoning(f"{s}\n\n{body}")
        assert removed, f"nothing removed for: {s!r}"
        assert body in cleaned, f"real body lost while stripping: {s!r}"


def test_mixed_leaks_are_flagged_for_review():
    # A Korean echo (with or without a trailing marker) must be caught so validation
    # routes the chapter to needs-review instead of silently accepting it.
    for s, _protected in MIXED_FLAG_ONLY:
        assert has_korean_leak(s), f"mixed leak not flagged: {s!r}"


def test_mixed_leaks_do_not_delete_real_english():
    # The safety guarantee: a marker riding on real translated English must NOT cause that
    # English to be deleted. Strip may leave the block untouched (it will be flagged).
    for s, protected in MIXED_FLAG_ONLY:
        if protected is None:
            continue
        cleaned, _ = strip_reasoning(s)
        assert protected in cleaned, f"real English deleted from mixed leak: {s!r}"


def test_legit_prose_is_never_flagged():
    for s in LEGIT_KEEP:
        assert not find_leaks(s), f"false positive (meta) on legit prose: {s!r}"


def test_legit_prose_is_never_stripped():
    for s in LEGIT_KEEP:
        cleaned, removed = strip_reasoning(s)
        assert not removed, f"legit prose stripped: {s!r}"
        assert cleaned == s, f"legit prose altered: {s!r}"


def test_emoticons_and_short_sfx_are_kept():
    for s in LEGIT_KOREAN_KEEP:
        assert not has_korean_leak(s), f"emoticon/SFX wrongly flagged as leak: {s!r}"
        kept, removed = remove_korean_echoes(s)
        assert removed == 0, f"emoticon/SFX block wrongly removed: {s!r}"
        assert kept.strip() == s.strip(), f"emoticon/SFX text altered: {s!r}"


def test_pure_korean_echoes_are_removed():
    for s in KOREAN_ECHOES:
        _, removed = remove_korean_echoes(s)
        assert removed == 1, f"pure Korean echo not removed: {s!r}"


def test_korean_phrase_is_detected_but_single_short_token_is_not():
    assert has_korean_leak("숙소에 돌아온 한사원")   # multi-word phrase = leaked sentence
    assert not has_korean_leak("쿵")               # lone SFX syllable -> kept
    assert not has_korean_leak("The bell went 땅.")  # lone inline Korean token -> kept


def test_strip_reasoning_preserves_a_clean_chapter():
    clean = "# Serenade on Stage\n\nThe curtain rose.\n\nShe stepped into the light."
    cleaned, removed = strip_reasoning(clean)
    assert removed == []
    assert cleaned == clean


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)



# ---- the export header, which must never reach the translator ------------

# The shape that prompted these: a RIDI export repeats the novel's title on a line of
# its own, and repeats the chapter number too. Whatever survives here is fed to the
# model, and the model sometimes TRANSLATES it instead of dropping it -- which is how a
# saved chapter came to open with the novel's title and "Chapter 161", to be deleted by
# hand. Measured on the corpus: 207 of 4,110 source chapters carried a stray title line.

SEP = chr(10) + chr(10)


def _strip(*blocks):
    return strip_source_header(SEP.join(blocks))


def test_the_repeated_title_and_number_are_both_dropped():
    clean, number = _strip(
        "ridibooks.com/books/2259040008/view",
        "공의 형이 된다는 건 161화",
        "5-6 minutes",
        "공의 형이 된다는 건",      # the title, again, on its own
        "161화",                    # and the number, again
        "그는 문을 열었다.",
    )
    assert number == "161"
    assert clean == "그는 문을 열었다."


def test_a_finale_marker_after_the_number_still_reads_as_a_header():
    # "147화 (완결)" -- anchoring hard at the end missed these, so the number was never
    # read AND the whole line survived into the chapter.
    clean, number = _strip(
        "ridibooks.com/books/3302054804/view",
        "무슠 버스에 빙의했다 147화 (완결)",
        "4 minutes",
        "진짜 본문이다.",
    )
    assert number == "147"
    assert clean == "진짜 본문이다."


def test_a_title_above_the_chapter_line_is_still_a_header():
    # One real export puts the title FIRST. The scan used to break on it and strip
    # nothing at all, so the entire header went to the model.
    clean, number = _strip(
        "Are You Out of Your Mind? (제정신이니?)",
        "Chapter 16",
        "He opened the door.",
    )
    assert number == "16"
    assert clean == "He opened the door."


# ---- and the other side of the contract: prose is never eaten -------------


def test_a_short_opening_line_that_is_not_the_title_is_kept():
    # A real line from the corpus: an in-story section heading. It is short, Korean and
    # carries no sentence punctuation -- exactly the shape a title has -- so the ONLY
    # thing separating them is that this one does not repeat the header's title.
    clean, _ = _strip(
        "공의 형이 된다는 건 43화",
        "6-7 minutes",
        "[7. 파티에서 생긴 일]",
        "그는 문을 열었다.",
    )
    assert clean.startswith("[7. 파티에서 생긴 일]")


def test_a_sentence_that_merely_contains_the_chapter_marker_is_not_a_header():
    clean, number = _strip("그는 100화를 보았다.", "다음 문장.")
    assert number is None
    assert clean.startswith("그는 100화를 보았다.")


def test_a_number_that_differs_from_the_chapter_is_in_story_content():
    # The existing contract, re-asserted because the pattern that decides it was widened.
    clean, number = _strip("제목 29화", "33.", "본문.")
    assert number == "29"
    assert clean.startswith("33.")

# ---- the same header, surviving into the TRANSLATION ---------------------

# strip_source_header runs before the model sees the chapter, so everything below is the
# case where it could NOT recognise the header -- a tab with no "NNN화" line, or a title
# the source stripper does not look for. The model then translates the header along with
# the prose and the chapter is saved opening with it.
#
# Every input here is the real leading block of a real file in projects/, because the
# rule they pin is the one that failed: each earlier repair needed the chapter's own
# number before it would remove anything, and these are precisely the chapters whose
# number could not be read. The cleanup kept reporting "0 remaining" while the headings
# were still on screen.

from translation_bot.sanitize import strip_export_heading  # noqa: E402

BODY = "He opened the door and stepped into the corridor."


def _head(*blocks, number=None):
    return strip_export_heading(SEP.join(blocks), number)


def test_the_title_and_the_chapter_line_both_come_off_without_a_known_number():
    # projects/756cf21e6aeb/chapters/chapter-03.md
    clean, dropped = _head("# What It Means to Become Gong's Older Brother",
                           "Chapter 203", BODY)
    assert dropped is True
    assert clean == BODY


def test_a_bold_chapter_line_is_the_same_line():
    # chapter-08.md -- the model emphasises it about half the time.
    clean, dropped = _head("# On Becoming the Top's Older Brother", "**Chapter 208**", BODY)
    assert dropped is True
    assert clean == BODY


def test_the_title_is_matched_by_shape_not_by_name():
    # The reason the title cannot be matched against the novel's name: the model
    # re-translates that one source line differently in every chapter. These four are
    # the same Korean header from four consecutive files.
    for title in ("What It Means to Be the Top's Hyung",
                  "On Becoming the Lead's Older Brother",
                  "What It Means to Be the Duke's Older Brother",
                  "**On Becoming Gong's Older Brother**"):
        clean, dropped = _head(title, "Chapter 204", BODY)
        assert dropped is True, title
        assert clean == BODY, title


def test_a_chapter_line_with_no_title_over_it_still_comes_off():
    # chapter-02.md opens straight onto the number, with no title block at all.
    clean, dropped = _head("Chapter 202", BODY)
    assert dropped is True
    assert clean == BODY


def test_episode_counts_as_a_chapter_marker():
    # projects/b65beb851d22/chapters/chapter-088.md
    clean, dropped = _head("Episode 188", BODY)
    assert dropped is True
    assert clean == BODY


def test_a_number_carrying_heading_marks_is_export_cruft():
    # "# 101" / "#61": eleven projects carry one of these, each at a constant offset from
    # the chapter index and covering essentially every file in the project.
    for block in ("# 101", "#61", "**160**", "#101."):
        clean, dropped = _head(block, BODY)
        assert dropped is True, block
        assert clean == BODY, block


def test_an_unmarked_bare_number_is_still_in_story_content():
    # The documented false positive, and the one shape the marks rule exists to spare:
    # chapter-17 of one project opens "33.", chapter-79 opens "55.". Without marks and
    # without a number to match, this stays.
    clean, dropped = _head("33.", BODY)
    assert dropped is False
    assert clean.startswith("33.")


def test_an_unmarked_bare_number_still_comes_off_when_it_is_the_chapter_number():
    clean, dropped = _head("33.", BODY, number="33")
    assert dropped is True
    assert clean == BODY


def test_a_title_with_no_heading_under_it_is_left_alone():
    # A short opening line on its own is an in-story section heading. The chapter line
    # beneath it is the only thing that makes it a header.
    clean, dropped = _head("[7. What Happened at the Party]", BODY)
    assert dropped is False
    assert clean.startswith("[7. What Happened at the Party]")


def test_a_sentence_that_merely_mentions_a_chapter_is_not_a_heading():
    opening = "Chapter 203 of the manual said nothing about this."
    clean, dropped = _head(opening, BODY)
    assert dropped is False
    assert clean.startswith(opening)


def test_a_chapter_line_deeper_in_the_prose_is_untouched():
    clean, dropped = _head(BODY, "Chapter 203", "More prose.")
    assert dropped is False
    assert "Chapter 203" in clean


def test_nothing_to_strip_returns_the_text_byte_for_byte():
    # This rewrites files in place, so a no-op must not renormalise the prose it was not
    # asked to touch -- splitting on blank lines and rejoining would do exactly that.
    original = "First line." + (chr(10) * 3) + "Second, after a wide gap." + chr(10)
    clean, dropped = strip_export_heading(original)
    assert dropped is False
    assert clean == original


def test_the_body_below_a_dropped_heading_keeps_its_own_spacing():
    original = SEP.join(["Chapter 202", "First." + (chr(10) * 3) + "Second."])
    clean, dropped = strip_export_heading(original)
    assert dropped is True
    assert clean == "First." + (chr(10) * 3) + "Second."

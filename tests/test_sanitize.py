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

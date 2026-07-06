"""Regression tests for validation FLAGGING of leaks.

A leak (AI meta-commentary or stray untranslated Korean) must route a chapter to
needs-review instead of being silently accepted — while a clean translation, and one that
legitimately keeps Korean text-emoticons (ㅠㅠ/ㅋㅋ) in a chat scene, must still pass.

We assert on the SPECIFIC failure text so unrelated structural checks (length ratio,
paragraph count) can't mask or fake the result.

Runs under pytest (``pytest tests/``) and standalone (``python tests/test_validate.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.config import ValidationConfig  # noqa: E402
from translation_bot.docs_extract import Chapter  # noqa: E402
from translation_bot.validate import validate_translation  # noqa: E402

CFG = ValidationConfig()

# A plain Korean source of a few paragraphs; the English below sits comfortably inside the
# length-ratio band so only the leak checks decide the outcome.
SOURCE = Chapter(
    index=1,
    title="Test",
    paragraphs=[
        "그는 창밖을 오래도록 바라보았다.",
        "빗소리가 방 안을 가득 채웠다.",
        "그녀는 아무 말도 하지 않았다.",
    ],
)

CLEAN_EN = (
    "He gazed out the window for a long while, watching the grey street below.\n\n"
    "The sound of the rain filled the small room from wall to wall.\n\n"
    "She said nothing at all, and the silence stretched between them."
)


def _korean_failure(failures):
    return any("untranslated Korean" in f for f in failures)


def _meta_failure(failures):
    return any(("reasoning" in f) or ("leaked" in f) for f in failures)


def test_clean_translation_has_no_leak_failures():
    res = validate_translation(SOURCE, CLEAN_EN, CFG)
    assert not _korean_failure(res.failures), res.failures
    assert not _meta_failure(res.failures), res.failures


def test_stray_korean_phrase_is_flagged():
    # A single leaked Korean sentence in an otherwise-English chapter — far below the
    # whole-chapter 10% fraction, so ONLY the stray-phrase signal can catch it.
    leaked = CLEAN_EN + "\n\n그녀는 천천히 고개를 돌려 그를 바라보았다."
    res = validate_translation(SOURCE, leaked, CFG)
    assert _korean_failure(res.failures), res.failures
    assert not res.ok


def test_meta_commentary_is_flagged():
    leaked = "Let me translate.\n\n" + CLEAN_EN
    res = validate_translation(SOURCE, leaked, CFG)
    assert _meta_failure(res.failures), res.failures
    assert not res.ok


def test_arrow_mapping_is_flagged():
    leaked = '무대 위의 세레나데 → "Serenade on Stage"\n\n' + CLEAN_EN
    res = validate_translation(SOURCE, leaked, CFG)
    assert (_meta_failure(res.failures) or _korean_failure(res.failures)), res.failures
    assert not res.ok


def test_emoticons_do_not_flag_a_chat_chapter():
    # Korean text-emoticons kept in an SNS/chat scene must NOT be read as untranslated
    # source, and must not by themselves push the chapter to needs-review.
    chat = (
        "The group chat lit up all at once.\n\n"
        "[Narin: are you seeing this ㅠㅠㅠㅠㅠ]\n\n"
        "[Sehui: LMAO he really did that ㅋㅋㅋㅋㅋㅋ ㅇㅇ]\n\n"
        "Nobody could quite believe what had just happened on the broadcast."
    )
    res = validate_translation(SOURCE, chat, CFG)
    assert not _korean_failure(res.failures), res.failures
    assert not _meta_failure(res.failures), res.failures


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

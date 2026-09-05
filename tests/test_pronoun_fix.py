"""Tests for the pronoun flag and the targeted pronoun repair.

Three things have to hold:

* a pronoun failure is recognised as its OWN kind of problem, with its own action —
  not lumped in with length-ratio drift under "something failed";
* AI resolve is actually told which character is wrong and which pronoun to use
  (before this, a pronoun-only failure got a generic "translate faithfully" nudge and
  the model simply re-guessed);
* the repair refuses to write anything if the model changed more than pronouns.

The failure strings below are copied verbatim from real state.json records, so the
back-compat parsing is tested against the data it actually has to read.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_pronoun_fix.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.app as A  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.docs_extract import Chapter  # noqa: E402
from translation_bot.glossary import Glossary  # noqa: E402
from translation_bot.pipeline import fix_pronouns_chapter, pronouns_only_changed  # noqa: E402
from translation_bot.state import State  # noqa: E402
from translation_bot.translator import Translator  # noqa: E402
from translation_bot.validate import PronounConflict  # noqa: E402

# Verbatim from projects/*/state.json.
REAL_FAILURES = [
    "Da-yul is tagged 'she' in the glossary but the chapter uses the opposite pronoun 47x",
    "Ihyeon is tagged 'he' in the glossary but the chapter uses the opposite pronoun 2x",
    "Ihyeon is tagged 'he' in the glossary but the chapter uses the opposite pronoun 1x",
    "Grandpa is tagged 'he' in the glossary but the chapter uses the opposite pronoun 1x",
    "Cerestia Kaiserion is tagged 'she' in the glossary but the chapter uses the opposite pronoun 3x",
    "Isabella Kegril is tagged 'she' in the glossary but the chapter uses the opposite pronoun 5x",
    "Her Majesty is tagged 'she' in the glossary but the chapter uses the opposite pronoun 2x",
]


# ---- the message the validator writes is the message the parser reads -------

def test_conflict_message_is_unchanged_from_what_is_on_disk():
    """The structured form must still render the exact sentence already in state.json,
    or every previously-flagged chapter would stop being recognised."""
    c = PronounConflict(name="Da-yul", expected="she", hits=47)
    assert c.message() == REAL_FAILURES[0]


def test_every_real_failure_string_parses():
    for f in REAL_FAILURES:
        assert A._PRONOUN_FAILURE_RE.match(f), f


def test_multi_word_names_parse_whole():
    got = A.pronoun_conflicts({"failures": [REAL_FAILURES[4]]})
    assert got == [{"name": "Cerestia Kaiserion", "expected": "she", "hits": 3}]


def test_a_non_pronoun_failure_is_not_mistaken_for_one():
    assert A.pronoun_conflicts(
        {"failures": ["length ratio 0.64 below 1.6 — likely omission/summarizing"]}) == []


# ---- structured beats parsed, but parsed is the fallback --------------------

def test_structured_conflicts_are_preferred():
    rec = {"failures": [REAL_FAILURES[1]],
           "pronoun_conflicts": [{"name": "Ihyeon", "expected": "he", "hits": 9}]}
    assert A.pronoun_conflicts(rec)[0]["hits"] == 9, "stored data should win over re-parsing"


def test_legacy_record_without_structured_data_still_works():
    """The whole point of the fallback: chapters flagged before this feature existed."""
    assert A.pronoun_conflicts({"failures": [REAL_FAILURES[1]]}) == [
        {"name": "Ihyeon", "expected": "he", "hits": 2}]


def test_missing_record_is_not_an_error():
    assert A.pronoun_conflicts(None) == []
    assert A.pronoun_conflicts({}) == []


# ---- the separate flag ------------------------------------------------------

def test_pronoun_failure_gets_its_own_kind_and_action():
    d = A.diagnose([REAL_FAILURES[0]])[0]
    assert d["kind"] == "pronoun"
    assert d["action"] == "fix_pronouns", "a pronoun problem should not route to ai_resolve"


def test_diagnosis_message_is_plain_english_not_the_raw_failure():
    d = A.diagnose([REAL_FAILURES[0]])[0]
    assert d["message"] != REAL_FAILURES[0]
    assert "Da-yul" in d["message"] and "47 times" in d["message"]


def test_singular_hit_reads_correctly():
    assert "1 time." in A.diagnose([REAL_FAILURES[2]])[0]["message"]


def test_other_failures_keep_their_existing_kinds():
    assert A.diagnose(["length ratio 0.64 below 1.6 — likely omission"])[0]["kind"] == "omission"
    assert A.diagnose(["AI reasoning/notes leaked into the text"])[0]["kind"] == "leak"


def test_flags_are_deduped_across_several_characters():
    assert A.failure_flags([REAL_FAILURES[1], REAL_FAILURES[3]]) == ["pronoun"]


def test_flags_of_a_clean_chapter_are_empty():
    assert A.failure_flags([]) == []


# ---- AI resolve is finally told what is wrong ------------------------------

def test_corrective_instruction_names_the_character_and_the_pronoun():
    """The original bug: a pronoun-only failure produced a generic instruction that
    never mentioned gender, so the re-translation re-guessed and failed identically."""
    rec = {"failures": [REAL_FAILURES[1]]}
    got = A._corrective_instruction(rec["failures"], A.pronoun_conflicts(rec))
    assert "Ihyeon" in got
    assert "he/him/his/himself" in got
    assert "failed an automated fidelity check" not in got, "generic fallback should be gone"


def test_corrective_instruction_covers_every_conflicting_character():
    rec = {"failures": [REAL_FAILURES[4], REAL_FAILURES[5]]}
    got = A._corrective_instruction(rec["failures"], A.pronoun_conflicts(rec))
    assert "Cerestia Kaiserion" in got and "Isabella Kegril" in got


def test_corrective_instruction_still_handles_the_other_failures():
    got = A._corrective_instruction(["length ratio 0.51 below 1.6 — likely omission"], [])
    assert "too SHORT" in got


def test_generic_fallback_survives_when_nothing_matches():
    assert "failed an automated fidelity check" in A._corrective_instruction(["mystery"], [])


# ---- the guard that makes an in-place rewrite safe -------------------------

def test_guard_accepts_a_pronoun_only_rewrite():
    before = "Ollia scrunched up her face. This was unfamiliar to her."
    after = "Ollia scrunched up his face. This was unfamiliar to him."
    assert pronouns_only_changed(before, after)


def test_guard_accepts_reflowed_whitespace():
    """Re-emitting a chapter often rewraps a line; that changes nothing that matters."""
    assert pronouns_only_changed("Ollia raised her hand\nand smiled.",
                                 "Ollia raised his hand and smiled.")


def test_guard_rejects_a_reworded_sentence():
    """The failure mode this exists for: the model 'improves' the prose while it is in
    there, silently replacing a chapter the user may have edited by hand."""
    assert not pronouns_only_changed("Ollia scrunched up her face.",
                                     "Ollia wrinkled his nose in distaste.")


def test_guard_rejects_a_dropped_sentence():
    assert not pronouns_only_changed("She left. The door closed behind her.",
                                     "He left.")


def test_guard_rejects_changed_punctuation():
    assert not pronouns_only_changed('She said, “Wait.”', 'He said, "Wait."')


def test_guard_accepts_an_untouched_chapter():
    assert pronouns_only_changed("Nothing to do here.", "Nothing to do here.")


# ---- the repair end to end (with a stand-in translator) ---------------------

class _FakeTranslator:
    """Stands in for Claude: returns whatever `reply` is, recording what it was asked."""

    _PRONOUN_FORMS = Translator._PRONOUN_FORMS

    def __init__(self, reply):
        self.reply = reply
        self.seen = None

    def fix_pronouns(self, prose, fixes, hooks=None):
        self.seen = {"prose": prose, "fixes": fixes}
        return self.reply, {"input_tokens": 10, "output_tokens": 10}, 0.01


def _project(tmp_path, translation, *, in_audit=False):
    """A minimal project on disk, with the translation in chapters/ or only in audit/."""
    cfg = Config()
    cfg.paths.output_dir = tmp_path / "chapters"
    cfg.paths.audit_dir = tmp_path / "audit"
    cfg.paths.state_file = tmp_path / "state.json"
    cfg.paths.glossary_json = tmp_path / "glossary.json"
    if in_audit:
        cfg.paths.audit_dir.mkdir(parents=True, exist_ok=True)
        (cfg.paths.audit_dir / "chapter-01.md").write_text(
            "# Chapter 1\n\n## Source\n\n...\n\n## Translation (English)\n\n" + translation,
            encoding="utf-8")
    else:
        cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
        (cfg.paths.output_dir / "chapter-01.md").write_text(translation, encoding="utf-8")
    return cfg


CH = Chapter(index=1, title="Test", paragraphs=["그는 조용히 고개를 끄덕였다."])
CONFLICT = [{"name": "Ollia", "expected": "he", "hits": 1}]


def test_repair_writes_the_corrected_text(tmp_path):
    cfg = _project(tmp_path, "Ollia scrunched up her face.")
    t = _FakeTranslator("Ollia scrunched up his face.")
    status = fix_pronouns_chapter(CH, 1, t, Glossary([]), cfg, State(),
                                  conflicts=CONFLICT)
    assert (cfg.paths.output_dir / "chapter-01.md").read_text(encoding="utf-8").strip() \
        == "Ollia scrunched up his face."
    assert status in ("validated", "needs-review")


def test_repair_snapshots_the_previous_version_so_revert_still_works(tmp_path):
    cfg = _project(tmp_path, "Ollia scrunched up her face.")
    fix_pronouns_chapter(CH, 1, _FakeTranslator("Ollia scrunched up his face."),
                         Glossary([]), cfg, State(), conflicts=CONFLICT)
    prev = cfg.paths.output_dir.parent / "previous" / "chapter-01.md"
    assert prev.exists(), "the reader's compare/revert has nothing to show"
    assert "her face" in prev.read_text(encoding="utf-8")


def test_repair_reads_a_chapter_that_only_exists_in_audit(tmp_path):
    """The case that matters most: a chapter that FAILED validation was never written
    to chapters/ at all, and mis-gendered chapters are exactly those chapters."""
    cfg = _project(tmp_path, "Ollia scrunched up her face.", in_audit=True)
    t = _FakeTranslator("Ollia scrunched up his face.")
    fix_pronouns_chapter(CH, 1, t, Glossary([]), cfg, State(), conflicts=CONFLICT)
    assert t.seen["prose"].strip() == "Ollia scrunched up her face."
    assert (cfg.paths.output_dir / "chapter-01.md").exists(), "the fix was never materialised"


def test_repair_tells_the_model_which_character_and_which_pronoun(tmp_path):
    cfg = _project(tmp_path, "Ollia scrunched up her face.")
    t = _FakeTranslator("Ollia scrunched up his face.")
    fix_pronouns_chapter(CH, 1, t, Glossary([]), cfg, State(), conflicts=CONFLICT)
    assert t.seen["fixes"] == CONFLICT


def test_repair_refuses_to_write_a_reworded_chapter(tmp_path):
    """The guard, end to end: a model that 'improves' the prose must change nothing."""
    cfg = _project(tmp_path, "Ollia scrunched up her face.")
    t = _FakeTranslator("Ollia wrinkled his nose in obvious distaste.")
    with pytest.raises(ValueError):
        fix_pronouns_chapter(CH, 1, t, Glossary([]), cfg, State(), conflicts=CONFLICT)
    assert (cfg.paths.output_dir / "chapter-01.md").read_text(encoding="utf-8").strip() \
        == "Ollia scrunched up her face.", "the chapter was modified despite the refusal"


def test_repair_with_nothing_to_fix_is_a_no_op(tmp_path):
    cfg = _project(tmp_path, "Ollia scrunched up her face.")
    state = State()
    state.update(1, status="needs-review")
    assert fix_pronouns_chapter(CH, 1, _FakeTranslator("x"), Glossary([]), cfg, state,
                                conflicts=[]) == "needs-review"


def test_repair_on_an_untranslated_chapter_raises_rather_than_writing(tmp_path):
    cfg = Config()
    cfg.paths.output_dir = tmp_path / "chapters"
    cfg.paths.audit_dir = tmp_path / "audit"
    cfg.paths.state_file = tmp_path / "state.json"
    with pytest.raises(ValueError):
        fix_pronouns_chapter(CH, 1, _FakeTranslator("x"), Glossary([]), cfg, State(),
                             conflicts=CONFLICT)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

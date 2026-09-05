"""Tests for re-checking a stored flag before spending a model call on it.

A flag in state.json is a snapshot of what the checks said when the chapter was
translated. The glossary moves on (pronouns get filled in, a name's spelling is fixed)
and the checks themselves improve, so by the time "Fix pronouns" is pressed the stored
flag may describe a problem that no longer exists. Queueing the AI repair anyway spends
a model call that finds nothing to change — and then re-saves the very same flag, which
is what made the button look broken.

``_recheck_saved`` re-runs validation on the English already on disk (no model call, no
source re-fetch) and persists the verdict, so the repair is asked for only where a
problem still stands.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_pronoun_recheck.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.app as A  # noqa: E402
from translation_bot import state as state_mod  # noqa: E402
from translation_bot.docs_extract import Chapter  # noqa: E402
from translation_bot.glossary import GlossaryEntry  # noqa: E402
from translation_bot.state import State  # noqa: E402

# One paragraph per chapter keeps the structural checks satisfied; the gender check is
# what these tests are about.
CLEAN = ("Grandma kept stroking Ihyeon's back, unable to take her eyes off him. "
         "Her hands were shaking.")
MIS_GENDERED = ("Ihyeon opened the door, brushing snow from her coat.\n\n"
                "Ihyeon had not slept. She stared at the ceiling until dawn.")

GLOSSARY = [GlossaryEntry(korean="이현", english="Ihyeon", type="name", pronoun="he"),
            GlossaryEntry(korean="할매", english="Grandma", type="name")]

# The stale record: what the old check wrote for a chapter it wrongly flagged.
STALE = {
    "status": state_mod.STATUS_NEEDS_REVIEW,
    "failures": ["Ihyeon is tagged 'he' in the glossary but the chapter uses "
                 "the opposite pronoun 1x"],
    "pronoun_conflicts": [{"name": "Ihyeon", "expected": "he", "hits": 1}],
}


def _chapter(index: int) -> Chapter:
    # Two paragraphs of Korean: enough source for the structural and length checks to
    # pass either translation below, so only the gender check can fail.
    return Chapter(index=index, title=f"Chapter {index}",
                   paragraphs=["원문 " * 20, "원문 " * 20])


def _install(monkeypatch, translations: dict[int, str], state: State) -> list:
    """Point _recheck_saved at in-memory chapters/state instead of a project on disk."""
    chapters = [_chapter(i) for i in sorted(translations)]
    saved: list = []
    monkeypatch.setattr(A, "get_chapters", lambda pid, cfg, refresh=False: chapters)
    monkeypatch.setattr(A, "_output_total", lambda pid, chs: len(chs))
    monkeypatch.setattr(A, "current_translation",
                        lambda cfg, index, total: translations.get(index))
    monkeypatch.setattr(A, "glossary_for", lambda cfg, ch: GLOSSARY)
    monkeypatch.setattr(A.State, "load", classmethod(lambda cls, path: state))
    monkeypatch.setattr(A.State, "save", lambda self, path: saved.append(path))
    return saved


class _Cfg:
    """Only the attributes _recheck_saved actually reads."""
    class validation:
        paragraph_tolerance = 2
        paragraph_tolerance_pct = 0.2
        length_ratio_min = 0.1
        length_ratio_max = 99.0
        dialogue_tolerance = 99

    class paths:
        state_file = "state.json"


def test_a_flag_that_no_longer_holds_is_cleared_without_a_model_call(monkeypatch):
    state = State()
    state.chapters["1"] = dict(STALE)
    saved = _install(monkeypatch, {1: CLEAN}, state)

    remaining = A._recheck_saved("p", _Cfg(), [1])

    assert remaining == {1: []}                      # nothing left to repair
    assert state.get(1)["status"] == state_mod.STATUS_VALIDATED
    assert state.get(1)["failures"] == []
    assert saved, "the cleared verdict has to be persisted, not just returned"


def test_a_chapter_that_is_still_wrong_keeps_its_conflict(monkeypatch):
    state = State()
    state.chapters["1"] = dict(STALE)
    _install(monkeypatch, {1: MIS_GENDERED}, state)

    remaining = A._recheck_saved("p", _Cfg(), [1])

    assert [c["name"] for c in remaining[1]] == ["Ihyeon"]
    assert state.get(1)["status"] == state_mod.STATUS_NEEDS_REVIEW


def test_an_unchanged_verdict_does_not_rewrite_state(monkeypatch):
    # Re-checking is cheap precisely because it usually changes nothing; it must not
    # churn state.json (and race the worker's own writes) for a verdict that held.
    state = State()
    state.chapters["1"] = {
        "status": state_mod.STATUS_NEEDS_REVIEW,
        "failures": ["Ihyeon is tagged 'he' in the glossary but the chapter uses "
                     "the opposite pronoun 2x"],
        "pronoun_conflicts": [{"name": "Ihyeon", "expected": "he", "hits": 2}],
    }
    saved = _install(monkeypatch, {1: MIS_GENDERED}, state)
    monkeypatch.setattr(A, "current_translation", lambda cfg, index, total: MIS_GENDERED)

    A._recheck_saved("p", _Cfg(), [1])
    assert saved == [], "no verdict changed, so nothing should have been written"


def test_a_chapter_with_no_saved_translation_is_left_alone(monkeypatch):
    # Nothing on disk to re-judge; the stored flag stands rather than being cleared on
    # the strength of an empty string.
    state = State()
    state.chapters["1"] = dict(STALE)
    _install(monkeypatch, {1: ""}, state)

    remaining = A._recheck_saved("p", _Cfg(), [1])

    assert [c["name"] for c in remaining[1]] == ["Ihyeon"]
    assert state.get(1)["status"] == state_mod.STATUS_NEEDS_REVIEW


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

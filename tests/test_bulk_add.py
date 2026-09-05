"""Regression tests for the bulk-add partitioning logic (server/bulk.py).

Bulk add's contract: rows that arrive with a recognizable type are added
without any Claude call; only unlabeled rows are classified; and a bulk paste
never overwrites a mapped entry — the sole mutation is upgrading an
English-only placeholder when a row supplies its Korean.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_bulk_add.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.bulk import (  # noqa: E402
    MAX_TERM_LEN,
    normalize_bulk_pronoun,
    normalize_bulk_type,
    prepare_bulk_rows,
    split_flat,
)
from translation_bot.glossary import Glossary, GlossaryEntry  # noqa: E402


# ---- normalization ----------------------------------------------------------

def test_normalize_bulk_type_synonyms():
    assert normalize_bulk_type("name") == "name"
    assert normalize_bulk_type("Characters") == "name"
    assert normalize_bulk_type("codename") == "name"
    assert normalize_bulk_type(" LOCATION ") == "place"
    assert normalize_bulk_type("realm") == "place"
    assert normalize_bulk_type("abilities") == "skill"
    assert normalize_bulk_type("Curse") == "skill"
    assert normalize_bulk_type("Items") == "term"
    assert normalize_bulk_type("herb") == "term"
    assert normalize_bulk_type("buffs") == "term"
    assert normalize_bulk_type("law") == "term"
    assert normalize_bulk_type("Cultural Term") == "term"
    assert normalize_bulk_type("slang") == "term"
    assert normalize_bulk_type("misc") == "other"


def test_normalize_bulk_type_unknown_stays_empty():
    # "" must mean "classify me" — never a silent "other".
    assert normalize_bulk_type("") == ""
    assert normalize_bulk_type(None) == ""
    assert normalize_bulk_type("unknown") == ""
    assert normalize_bulk_type("auto") == ""
    assert normalize_bulk_type("banana") == ""


def test_normalize_bulk_pronoun():
    assert normalize_bulk_pronoun("male") == "he"
    assert normalize_bulk_pronoun(" F ") == "she"
    assert normalize_bulk_pronoun("He") == "he"
    assert normalize_bulk_pronoun("they") == "they"
    assert normalize_bulk_pronoun("banana") == ""
    assert normalize_bulk_pronoun("") == ""


def test_split_flat():
    assert split_flat("Kael, Sera;\nmana core") == ["Kael", "Sera", "mana core"]
    assert split_flat(" , ;\n") == []
    assert split_flat("") == []


# ---- partitioning -----------------------------------------------------------

def test_partition_mixed_typed_and_untyped():
    plan = prepare_bulk_rows(
        [
            {"english": "Kael", "type": "name", "pronoun": "male"},
            {"korean": "마나 코어", "english": "mana core", "type": "",
             "note": "power source"},
            {"english": "Ironhold", "type": "mystery"},  # unknown label → classify
        ],
        existing=[],
    )
    assert [(e.english, e.type, e.pronoun) for e in plan.typed] == [("Kael", "name", "he")]
    assert plan.untyped == [
        {"korean": "마나 코어", "english": "mana core", "note": "power source",
         "pronoun": "", "register": ""},
        {"korean": "", "english": "Ironhold", "note": "", "pronoun": "", "register": ""},
    ]
    assert plan.updated == [] and plan.skipped == []


def test_paste_dedup_first_wins():
    plan = prepare_bulk_rows(
        [
            {"english": "Kael", "type": "name"},
            {"english": "kael", "type": "place"},          # english dupe, case-insensitive
            {"korean": "세라", "english": "Sera", "type": "name"},
            {"korean": "세라", "english": "Sera Two", "type": "name"},  # korean dupe
        ],
        existing=[],
    )
    assert [e.english for e in plan.typed] == ["Kael", "Sera"]
    assert [s["reason"] for s in plan.skipped] == ["duplicate in this paste"] * 2


def test_skip_existing_mapped_english():
    existing = [GlossaryEntry(korean="카엘", english="Kael", type="name")]
    plan = prepare_bulk_rows([{"english": "kael", "type": "name"}], existing)
    assert plan.typed == [] and plan.updated == []
    assert plan.skipped == [{"english": "kael", "korean": "", "reason": "already in glossary"}]


def test_skip_existing_korean_names_current_spelling():
    existing = [GlossaryEntry(korean="카엘", english="Kael", type="name")]
    plan = prepare_bulk_rows([{"korean": "카엘", "english": "Cael", "type": "name"}], existing)
    assert plan.typed == [] and plan.updated == []
    assert plan.skipped[0]["reason"] == "Korean term already mapped to “Kael”"


def test_placeholder_upgrade_inherits_curated_fields():
    existing = [GlossaryEntry(korean="", english="Sera", type="name",
                              pronoun="she", note="the healer")]
    plan = prepare_bulk_rows(
        [{"korean": "세라", "english": "Sera", "type": "", "note": "MC's sister"}],
        existing,
    )
    assert plan.untyped == []  # inherited type — upgrades never hit the classifier
    assert len(plan.updated) == 1
    e = plan.updated[0]
    assert (e.korean, e.type, e.pronoun) == ("세라", "name", "she")
    assert e.note == "MC's sister"  # the row's own value wins when provided


def test_placeholder_without_korean_is_skipped():
    existing = [GlossaryEntry(korean="", english="Sera", type="name")]
    plan = prepare_bulk_rows([{"english": "Sera", "type": "name"}], existing)
    assert plan.updated == []
    assert plan.skipped[0]["reason"] == "already in glossary"


def test_korean_only_row_skipped():
    plan = prepare_bulk_rows([{"korean": "카엘"}], existing=[])
    assert plan.skipped == [{"english": "", "korean": "카엘",
                             "reason": "needs an English spelling"}]


def test_length_guard_catches_prose():
    prose = "He walked into the citadel and " * 5
    plan = prepare_bulk_rows([{"english": prose}], existing=[])
    assert len(prose) > MAX_TERM_LEN
    assert plan.skipped[0]["reason"] == "too long — looks like prose, not a term"


def test_blank_rows_dropped_silently():
    plan = prepare_bulk_rows([{}, {"english": "  ", "korean": ""}], existing=[])
    assert plan.typed == [] and plan.untyped == [] and plan.skipped == []


# ---- round trip through a real Glossary -------------------------------------

def test_plan_applies_cleanly_to_glossary():
    g = Glossary([GlossaryEntry(korean="", english="Sera", type="name", pronoun="she")])
    plan = prepare_bulk_rows(
        [
            {"korean": "세라", "english": "Sera"},          # upgrade
            {"english": "mana core", "type": "term"},       # fresh typed add
        ],
        g.entries(),
    )
    for e in plan.typed + plan.updated:
        g.add(e)
    entries = {e.english: e for e in g.entries()}
    assert len(entries) == 2
    assert entries["Sera"].korean == "세라"       # placeholder superseded, not duplicated
    assert entries["Sera"].pronoun == "she"      # curation survived the upgrade
    assert entries["mana core"].type == "term"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

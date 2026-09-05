"""Regression tests for the character gender/pronoun consistency mechanism.

The glossary pronoun is the anchor that stops a character's gender being
re-guessed (and flipped) chapter to chapter. These tests pin down each link:
the system prompt renders its gender directive and pronoun contract, model
proposals carry pronoun through the pending queue (with junk normalized away),
the injected glossary block exposes the tag, and model ids map to the right
Claude Code alias.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_glossary_pronoun.py``).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.glossary import (  # noqa: E402
    Glossary,
    GlossaryEntry,
    format_injection,
    load_pending,
    normalize_pronoun,
    queue_new_terms,
)
from translation_bot.prompts import build_system_prompt  # noqa: E402
from translation_bot.translator import _agent_model  # noqa: E402


# ---- prompt rendering -------------------------------------------------------

def test_system_prompt_renders_and_contains_gender_directive():
    # A missed doubled brace in the .format() template raises KeyError here.
    prompt = build_system_prompt("(no entries)")
    assert "Character gender & pronouns" in prompt
    assert "[pronoun: ...]" in prompt
    assert '"pronoun": "he|she|they|unknown"' in prompt  # NEW_TERMS contract


def test_system_prompt_renders_with_all_optional_sections():
    prompt = build_system_prompt(
        "- 카엘 -> Kael (name)",
        web_access=True,
        honorific_note="Keep -hyung attached.",
        style_note="BL romance.",
        names_block="- Kael (name)",
    )
    assert "Character gender & pronouns" in prompt


# ---- normalize_pronoun ------------------------------------------------------

def test_normalize_pronoun():
    assert normalize_pronoun("he") == "he"
    assert normalize_pronoun(" She ") == "she"
    assert normalize_pronoun("THEY") == "they"
    assert normalize_pronoun("unknown") == ""
    assert normalize_pronoun("") == ""
    assert normalize_pronoun(None) == ""
    assert normalize_pronoun("banana") == ""


# ---- pending queue carries pronoun -----------------------------------------

def test_queue_new_terms_carries_pronoun(tmp_path):
    pending_path = tmp_path / "glossary_pending.json"
    added = queue_new_terms(
        pending_path,
        Glossary([]),
        [
            {"korean": "카엘", "english": "Kael", "type": "name",
             "note": "addressed as hyung", "pronoun": "he"},
            {"korean": "세라", "english": "Sera", "type": "name", "pronoun": "unknown"},
            {"korean": "마나", "english": "mana", "type": "term", "pronoun": ""},
        ],
        chapter_index=3,
    )
    assert added == 3
    items = {p["english"]: p for p in load_pending(pending_path)}
    assert items["Kael"]["pronoun"] == "he"
    assert items["Sera"]["pronoun"] == ""   # "unknown" normalized away
    assert items["mana"]["pronoun"] == ""


def test_queue_new_terms_survives_json_roundtrip(tmp_path):
    pending_path = tmp_path / "glossary_pending.json"
    queue_new_terms(pending_path, Glossary([]),
                    [{"korean": "카엘", "english": "Kael", "type": "name", "pronoun": "he"}], 1)
    raw = json.loads(pending_path.read_text(encoding="utf-8"))
    assert raw[0]["pronoun"] == "he"


# ---- prompt injection shows the tag ----------------------------------------

def test_format_injection_renders_pronoun_tag():
    entry = GlossaryEntry(korean="카엘", english="Kael", type="name",
                          pronoun="he", register="casual/banmal")
    block = format_injection([entry])
    assert "[pronoun: he; register: casual/banmal]" in block


def test_format_injection_no_tag_without_profile():
    block = format_injection([GlossaryEntry(korean="마나", english="mana", type="term")])
    assert "[pronoun" not in block


# ---- model alias mapping ----------------------------------------------------

def test_agent_model_aliases():
    assert _agent_model("claude-opus-5") == "opus"
    assert _agent_model("claude-opus-4-8") == "opus"
    assert _agent_model("claude-sonnet-5") == "sonnet"
    assert _agent_model("claude-haiku-4-5") == "haiku"
    # Fable/unknown ids pass through untouched (the SDK accepts full ids).
    assert _agent_model("claude-fable-5") == "claude-fable-5"
    assert _agent_model("") == "opus"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

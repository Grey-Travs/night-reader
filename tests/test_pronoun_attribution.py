"""Regression tests for WHICH pronouns the gender check blames on a character.

The check exists to catch a chapter translated before a character's glossary
``pronoun`` was pinned, which renders them as the wrong gender for pages at a time.
It used to attribute every opposite-gender pronoun in a paragraph to the one glossary
character named in it, so ordinary correct prose — "Grandma kept stroking Ihyeon's
back, unable to take her eyes off him" — was reported as Ihyeon being mis-gendered.
That produced a flag no repair could clear: the AI pronoun fix, correctly told to
leave pronouns it can't attribute alone, changed nothing, and the same check re-raised
the same flag, so "Fix pronouns" looked broken no matter how many times it ran.

These tests pin both halves: real mis-gendering is still caught, and pronouns that
demonstrably belong to somebody else are not counted.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_pronoun_attribution.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.glossary import GlossaryEntry  # noqa: E402
from translation_bot.validate import (  # noqa: E402
    _MIN_WRONG_HITS,
    _pronoun_conflicts,
    contradictory_pronouns,
)


def name(english: str, pronoun: str = "", korean: str = "") -> GlossaryEntry:
    return GlossaryEntry(korean=korean or f"[{english}]", english=english,
                         type="name", pronoun=pronoun)


def conflicts(text: str, glossary) -> dict[str, int]:
    return {c.name: c.hits for c in _pronoun_conflicts(text, glossary)}


# ---- the failure the check exists for ---------------------------------------

def test_systematic_mis_gendering_is_still_caught():
    # Ihyeon is male in the glossary but the chapter calls him "she" throughout.
    text = "\n\n".join([
        "Ihyeon opened the door and stepped inside, brushing snow from her coat.",
        "Ihyeon had not slept. She stared at the ceiling until dawn.",
        "For all her talk, Ihyeon never once looked back.",
    ])
    assert conflicts(text, [name("Ihyeon", "he")]) == {"Ihyeon": 3}


def test_a_clean_chapter_reports_nothing():
    text = "\n\n".join([
        "Ihyeon opened the door and stepped inside, brushing snow from his coat.",
        "Ihyeon had not slept. He stared at the ceiling until dawn.",
    ])
    assert conflicts(text, [name("Ihyeon", "he")]) == {}


# ---- pronouns that belong to somebody else ----------------------------------

def test_another_glossary_character_in_the_paragraph_is_not_counted():
    # "her" is Grandma's. Ihyeon is correctly "him" in the very same sentence.
    text = ("Grandma kept stroking Ihyeon's back, unable to take her eyes off him. "
            "Her hands were shaking.")
    assert conflicts(text, [name("Ihyeon", "he"), name("Grandma")]) == {}


def test_an_unlisted_character_still_suppresses_the_paragraph():
    # Tae-ha is nowhere in the glossary — coverage is never complete, so the check
    # falls back to "a mid-sentence capital is another character".
    text = ("Da-yul flicked her ears and nestled in Tae-ha's arms. "
            "Tae-ha held her close, his sleeve brushing her cheek.")
    assert conflicts(text, [name("Da-yul", "she")]) == {}


def test_a_kinship_or_title_noun_suppresses_the_paragraph():
    # "his" is the Crown Prince's, not Her Majesty's.
    text = ("For a Crown Prince to begin his first letter to Her Majesty his mother "
            "with a story about a frog was unthinkable.")
    assert conflicts(text, [name("Her Majesty", "she")]) == {}


def test_sentence_initial_capitals_do_not_suppress():
    # Otherwise every sentence would look like it names somebody, and nothing would
    # ever be checked at all.
    text = "\n\n".join([
        "Ihyeon said nothing. Snow kept falling on her shoulders.",
        "Ihyeon turned away. Nothing about her expression changed.",
    ])
    assert conflicts(text, [name("Ihyeon", "he")]) == {"Ihyeon": 2}


# ---- weighing the evidence --------------------------------------------------

def test_a_single_stray_pronoun_is_not_enough():
    assert _MIN_WRONG_HITS == 2
    text = "Ihyeon frowned. Something about her tone had changed."
    assert conflicts(text, [name("Ihyeon", "he")]) == {}


def test_the_correct_pronoun_outvoting_the_wrong_one_is_not_a_conflict():
    # Two strays against a chapter that otherwise genders him correctly throughout is
    # attribution noise, not a translation that got the character's gender wrong.
    text = "\n\n".join([
        "Ihyeon shrugged. He had heard it all before, and he did not care.",
        "Ihyeon lifted his hand. His grip was steady, his breathing slow.",
        "Ihyeon laughed. Her laugh was short.",
        "Ihyeon left. Her coat stayed on the hook.",
    ])
    assert conflicts(text, [name("Ihyeon", "he")]) == {}


# ---- a glossary that contradicts itself -------------------------------------

def test_a_name_tagged_both_ways_is_a_glossary_problem_not_a_chapter_problem():
    # Two Korean spellings of one character, tagged he and she. Whichever pronoun the
    # chapter picks contradicts the other entry, so it could never stop being flagged —
    # and the repair pass would be handed two opposite orders for the same character.
    glossary = [name("Da-yul", "she", "다율"), name("Da-yul", "he", "다율이")]
    text = "\n\n".join([
        "Da-yul smiled. She waved her small hand and laughed.",
        "Da-yul yawned. She rubbed her eyes and went quiet.",
    ])
    assert contradictory_pronouns(glossary) == ["Da-yul"]
    assert conflicts(text, glossary) == {}


def test_the_same_name_mapped_twice_with_one_pronoun_is_reported_once():
    glossary = [name("Ihyeon", "he", "이현"), name("Ihyeon", "he", "천이현")]
    text = "\n\n".join([
        "Ihyeon opened the door and stepped inside, brushing snow from her coat.",
        "Ihyeon had not slept. She stared at the ceiling until dawn.",
    ])
    assert contradictory_pronouns(glossary) == []
    assert conflicts(text, glossary) == {"Ihyeon": 2}


def test_a_disagreement_reached_through_the_korean_is_caught():
    # The real shape, from "Are You Out Of Your Mind?": one character filed under three
    # romanizations. No two share an English name, so matching on English alone saw no
    # problem — while the glossary was telling the translator both "he" and "they".
    glossary = [name("Lee Shiyun", "he", "이시윤"),
                name("Lee Siyoon", "he", "이시윤 / 시윤"),
                name("Shiyoon", "they", "시윤")]
    # Linked by the Korean 시윤: "he" vs "they".
    assert contradictory_pronouns(glossary) == ["Lee Siyoon", "Shiyoon"]


def test_a_they_pin_can_contradict_a_he_pin():
    # "they" is a pin a human can set but not one the chapter check can test, so it used
    # to be dropped before the comparison — which made a real disagreement invisible.
    glossary = [name("Siyoon", "he", "시윤"), name("Siyoon", "they", "시윤")]
    assert contradictory_pronouns(glossary) == ["Siyoon"]


def test_different_characters_sharing_no_handle_are_not_conflated():
    glossary = [name("Ihyeon", "he", "이현"), name("Da-yul", "she", "다율")]
    assert contradictory_pronouns(glossary) == []


def test_several_korean_spellings_that_agree_are_not_a_contradiction():
    glossary = [name("Lee Shiyun", "he", "이시윤"),
                name("Lee Siyoon", "he", "이시윤 / 시윤"),
                name("Shiyoon", "he", "시윤")]
    assert contradictory_pronouns(glossary) == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

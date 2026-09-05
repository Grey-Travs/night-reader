"""Regression tests for the mis-gendered-pronoun repair tool (tools/fix_pronouns.py).

The tool's contract: it rewrites she/her to he/his/him only where the referent
is established, it never touches a genuinely female character, and it gets the
possessive/object split on "her" right ("her eyes" -> his, "beside her" -> him).

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_fix_pronouns.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fix_pronouns import (  # noqa: E402
    TARGETS,
    _rewrite,
    process,
    resolve,
)

OLLIA = TARGETS["ollia"]["pattern"]


# ---- "her" is possessive or object ------------------------------------------

def test_her_before_noun_is_possessive():
    assert _rewrite("Ollia scrunched up her face.")[0] == \
        "Ollia scrunched up his face."


def test_her_after_preposition_is_object():
    assert _rewrite("this was unfamiliar to her.")[0] == \
        "this was unfamiliar to him."


def test_her_before_punctuation_is_object():
    assert _rewrite("But before that hand could reach her.")[0] == \
        "But before that hand could reach him."


def test_her_before_determiner_is_object():
    assert _rewrite("He gave her a puzzled look")[0] == \
        "He gave him a puzzled look"


def test_her_before_infinitive_is_object():
    assert _rewrite("He asked her to wait")[0] == "He asked him to wait"


def test_hyphenated_modifier_stays_possessive():
    # "now-bared" must not be split; "now" alone would look like a particle.
    assert _rewrite("against her now-bared, smooth forehead")[0] == \
        "against his now-bared, smooth forehead"


def test_contact_verb_makes_back_a_body_part():
    assert _rewrite("Cassian patted her back awkwardly.")[0] == \
        "Cassian patted his back awkwardly."


def test_particle_back_stays_object():
    assert _rewrite("Cassian pulled her back from the ledge")[0] == \
        "Cassian pulled him back from the ledge"


# ---- the other pronoun forms ------------------------------------------------

def test_she_hers_herself():
    got, _ = _rewrite("She opened her eyes to find herself alone; the fault "
                      "was hers.")
    assert got == "He opened his eyes to find himself alone; the fault was his."


def test_contraction_keeps_clitic():
    assert _rewrite("she'd poured liquor")[0] == "he'd poured liquor"


def test_capitalisation_is_preserved():
    assert _rewrite("Her gaze fell. She left.")[0] == "His gaze fell. He left."


# ---- referent resolution ----------------------------------------------------

def test_target_alone_converts():
    paras = ["Ollia scrunched up her face."]
    assert resolve(paras, 0, OLLIA, 4)[0] == "convert"


def test_female_character_blocks():
    paras = ["Heilon let go of her wounded arm and straightened her back."]
    assert resolve(paras, 0, OLLIA, 4)[0] != "convert"


def test_male_bystander_does_not_block():
    # Cassian cannot own a "her", so he must not make this ambiguous.
    paras = ["Cassian stared blankly at her, and Ollia looked away."]
    assert resolve(paras, 0, OLLIA, 4)[0] == "convert"


def test_shared_paragraph_with_woman_is_ambiguous():
    paras = ["Ollia explained how she had passed the flute to the princess."]
    assert resolve(paras, 0, OLLIA, 4)[0] == "ambiguous"


def test_pronoun_follows_nearest_antecedent():
    paras = ["Ollia stepped into the alley.", "She let out a shaking breath."]
    assert resolve(paras, 1, OLLIA, 4)[0] == "convert"


def test_unknown_gender_bystander_blocks():
    paras = ["The mage narrowed her eyes at Ollia."]
    assert resolve(paras, 0, OLLIA, 4)[0] == "ambiguous"


# ---- whole-chapter behaviour ------------------------------------------------

def test_her_highness_is_never_rewritten():
    text = "Ollia bowed to Her Highness and lowered her gaze."
    new, applied, _ = process(text, OLLIA, 4, sole_female=True)
    assert "Her Highness" in new
    assert "lowered his gaze" in new
    assert applied


def test_female_scene_survives_untouched():
    text = ("Heilon narrowed her brow.\n\n"
            "From childhood she had drawn attention with her swordsmanship.")
    new, applied, _ = process(text, OLLIA, 4, sole_female=False)
    assert new == text
    assert not applied


def test_collision_is_flagged_for_review():
    text = "Cassian reached out, and she pulled his wrist to her chest."
    _, applied, _ = process(text, OLLIA, 4, sole_female=True)
    assert applied[0]["collision"] is True


def test_is_idempotent():
    text = "Ollia scrunched up her face and she sighed."
    once, _, _ = process(text, OLLIA, 4, sole_female=True)
    twice, applied, _ = process(once, OLLIA, 4, sole_female=True)
    assert once == twice
    assert not applied


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

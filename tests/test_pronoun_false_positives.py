"""The shapes that made correctly-translated chapters look mis-gendered.

Nine chapters across four novels were flagged as rendering a character as the wrong
gender. Every one was read against its Korean source and every one was wrong: the prose
was correct and the checker was counting somebody else's pronouns. That matters more than
a missed conflict would, because a flag sends the repair pass in to rewrite prose that was
already right — flipping the pronouns of whoever the scene partner was.

Each test below is one of those failure shapes, rewritten with invented characters (the
real ones live in gitignored per-user projects). The counterpart tests in
``test_pronoun_attribution.py`` guard the other direction: that a genuine, systematic
mis-gendering is still caught.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_pronoun_false_positives.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.glossary import GlossaryEntry  # noqa: E402
from translation_bot.validate import _pronoun_conflicts  # noqa: E402


def name(english: str, pronoun: str = "", korean: str = "") -> GlossaryEntry:
    return GlossaryEntry(korean=korean or f"[{english}]", english=english,
                         type="name", pronoun=pronoun)


def conflicts(text: str, glossary) -> dict[str, int]:
    return {c.name: c.hits for c in _pronoun_conflicts(text, glossary)}


# ---- named only to identify somebody else -----------------------------------

def test_a_possessive_only_mention_is_about_the_other_person():
    # "…she, MARIN'S PARTNER no less, had gone out of HER way" names Marin (male) only
    # to say who the woman is. Three feminine pronouns were counted against him.
    text = ("It was unlikely she'd suddenly developed feelings for him, and he figured "
            "there had to be a reason she, Marin's partner no less, had gone out of her "
            "way to come over here.")
    assert conflicts(text, [name("Marin", "he")]) == {}


# ---- a chat log line is the speaker talking about other people --------------

def test_a_chat_log_speaker_label_is_not_evidence_about_the_speaker():
    # "Rue:" is a speaker label. The masculine pronouns are the man she is plotting
    # against, not Rue herself.
    text = "\n\n".join([
        "Rue: Beat him to it, obviously. Before he can get me, I'll bury him.",
        "Rue: He seems obsessed with me and it's so scary.",
    ])
    assert conflicts(text, [name("Rue", "she")]) == {}


def test_a_speaker_label_does_not_hide_a_real_conflict_in_narration():
    # The guard is scoped to the labelled line. Ordinary narration in the same chapter
    # still counts, so a chat-format chapter is not blanket-exempt.
    text = "\n\n".join([
        "Rue: Beat him to it, obviously. Before he can get me, I'll bury him.",
        "Rue closed the laptop. His shoulders were shaking.",
        "Rue said nothing more. Snow kept falling on his coat.",
    ])
    assert conflicts(text, [name("Rue", "she")]) == {"Rue": 2}


# ---- the two-hander scene ---------------------------------------------------

def test_an_unnamed_scene_partner_is_not_counted_against_the_named_character():
    # These novels are built from two-handers where one party is named and the other is
    # carried by pronoun alone. Both genders in one paragraph means at least one set
    # belongs to somebody else, and nothing in the text says which — so the paragraph is
    # not evidence either way. Counting it gave the man's four pronouns to the woman he
    # was standing next to.
    text = ("Sera grinned and linked her arm through his. He glanced at her, then he "
            "nodded, and his smile widened.")
    assert conflicts(text, [name("Sera", "she")]) == {}


def test_being_gendered_correctly_elsewhere_outweighs_the_partners_pronouns():
    # The asymmetry that matters: the correct pronoun is counted from EVERY paragraph
    # naming her, the wrong one only from paragraphs that clear the ambiguity guards.
    # Here the three paragraphs proving she is "she" all mention Raon too, so the old
    # check discarded them wholesale and saw only the scene partner's four masculine
    # pronouns — a chapter that genders her correctly throughout, reported as mis-gendered.
    text = "\n\n".join([
        "Sera glanced at him and cautiously ventured a question.",
        "He unlocked it and handed it over, and Sera took it and tapped at the screen.",
        "At his words, Sera nodded, confirming he'd gotten it right.",
        "Sera turned to Raon and smiled. She had been waiting all evening, her hands cold.",
        "Raon watched Sera cross the lobby. She walked quickly, her coat over her arm.",
        "Sera called out to Raon once more. She was not finished, and her voice carried.",
    ])
    assert conflicts(text, [name("Sera", "she"), name("Raon", "he")]) == {}


# ---- a pronoun in an earlier sentence ---------------------------------------

def test_a_pronoun_before_the_first_mention_belongs_to_the_earlier_topic():
    # First-person narration about a man, and only then does she come to mind. The two
    # masculine pronouns are the man the narrator was already thinking about.
    text = ("The words sounded almost like he was checking whether I'd been waiting for "
            "him, and the back of my neck went ticklish for no reason. Then Yeonhwa came "
            "to mind, and my mood soured.")
    assert conflicts(text, [name("Yeonhwa", "she")]) == {}


def test_a_pronoun_pointing_back_within_its_own_sentence_still_counts():
    # Cataphora inside one sentence is a real reference — the sentence is the unit, not
    # the name's position in it. Otherwise this genuine mis-gendering would be missed.
    text = "\n\n".join([
        "For all her talk, Jiwan never once looked back.",
        "Jiwan had not slept. She stared at the ceiling until dawn.",
    ])
    assert conflicts(text, [name("Jiwan", "he")]) == {"Jiwan": 2}


# ---- plural person nouns ----------------------------------------------------

def test_a_genre_role_noun_marks_another_person_too():
    # From a raid scene: the Governor (female) shouts about somebody else, and that
    # somebody is identified only as "a returning player" — never named. Without
    # "player" in the person-noun list his two pronouns were counted against her.
    text = ("Governor shouted, dumbfounded. Dancing leisurely in the middle of combat. "
            "He'd muttered earlier about being a returning player, so maybe he had no "
            "skills either.")
    assert conflicts(text, [name("Governor", "she")]) == {}


def test_plural_person_nouns_put_other_people_in_the_paragraph_too():
    # "MEN rather than WOMEN" establishes other people just as "a man" would; the
    # singular-only list read this as evidence about the woman it happened to name.
    text = ("But unfortunately, his tastes ran toward men rather than women. Knowing "
            "full well that if it weren't for that, he might well have chosen Nari as "
            "his final pick, he felt a pang of guilt.")
    assert conflicts(text, [name("Nari", "she")]) == {}


# ---- the failure that DOES need catching ------------------------------------

def test_a_wrong_glossary_pronoun_still_surfaces_as_a_conflict():
    # The Dobby case: the glossary pinned a female character as "he", so the model
    # rendered her male off subject-dropped Korean. Once the glossary is corrected the
    # affected chapters must light up — that is how the damage gets found.
    text = "\n\n".join([
        "Danbi asked back, his voice more than a little surprised.",
        "Gladly accepting the opening, Danbi gave a small bow of his head.",
        "Opening up very cautiously, Danbi swallowed hard. His hands were restless.",
    ])
    assert conflicts(text, [name("Danbi", "she")]) == {"Danbi": 3}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

"""Automatic validation of a translated chapter — never silently accept suspect output.

Structural, length-ratio, dialogue, and surface checks flag likely omission,
embellishment, or formatting drift. A failed check triggers one corrective retry
upstream; a still-failing chapter is marked needs-review rather than written as good.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .config import ValidationConfig
from .docs_extract import Chapter, _QUOTE_RE
from .sanitize import find_leaks, has_korean_leak, korean_fraction
from .textsplit import SEP_RE


@dataclass
class PronounConflict:
    """One character the chapter mis-genders, in a form a repair pass can act on.

    The failure list carries :meth:`message` (prose, for the user); this dataclass
    carries the same finding as data, so the fixer knows WHICH character to correct
    and to WHAT — rather than re-parsing an English sentence.
    """

    name: str
    expected: str  # the glossary's pronoun: "he" / "she"
    hits: int      # how many opposite-gender pronouns were counted

    def message(self) -> str:
        """The human-readable failure line.

        Kept byte-identical to what this check has always emitted so records
        already saved in state.json still parse and read the same.
        """
        return (f"{self.name} is tagged '{self.expected}' in the glossary "
                f"but the chapter uses the opposite pronoun {self.hits}x")


@dataclass
class ValidationResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    # The pronoun failures above, as data. Persisted per chapter so "Fix pronouns"
    # and the corrective re-translate can name the character and the right pronoun.
    pronoun_conflicts: list[dict] = field(default_factory=list)


def _paragraphs(text: str) -> list[str]:
    return [p for p in SEP_RE.split(text.strip()) if p.strip()]


def _nonspace_len(text: str) -> int:
    return len(re.sub(r"\s", "", text))


_PRONOUN_FORMS = {
    "he": re.compile(r"\b(?:he|his|him|himself)\b", re.I),
    "she": re.compile(r"\b(?:she|her|hers|herself)\b", re.I),
}

# Nouns that put a SECOND person in the paragraph. A pronoun sitting next to one of
# these may well be theirs, so the paragraph says nothing reliable about the character
# being checked. ("Grandma kept stroking Ihyeon's back, unable to take HER eyes off
# him" is correct prose that the old check read as Ihyeon being mis-gendered.)
#
# Plurals count too: "his tastes ran toward MEN rather than WOMEN" put other people in
# the paragraph just as surely as the singular would, and the singular-only list read
# that sentence as evidence about the woman it happened to name.
_PERSON_NOUN_RE = re.compile(
    r"\b(?:grandmas?|grandmothers?|grandpas?|grandfathers?|granny|grannies|gramps|"
    r"moms?|mums?|mothers?|mommy|mummy|dads?|fathers?|papa|daddy|"
    r"aunts?|aunties|auntie|uncles?|nieces?|nephews?|cousins?|"
    r"brothers?|sisters?|siblings?|twins?|"
    r"hyungs?|noonas?|nunas?|oppas?|unnie|unnies|sunbaes?|hoobaes?|ahjussis?|ahjummas?|"
    r"sons?|daughters?|child|children|kids?|boys?|girls?|"
    r"man|men|woman|women|lady|ladies|gentleman|gentlemen|guys?|"
    r"husbands?|wife|wives|fiances?|fiancees?|lovers?|boyfriends?|girlfriends?|"
    # Relational and role nouns: a "partner", "friend" or "contestant" is another person
    # in the paragraph exactly the way a "brother" is.
    r"partners?|friends?|colleagues?|classmates?|roommates?|teammates?|"
    r"seniors?|juniors?|neighbou?rs?|contestants?|members?|"
    r"sirs?|madams?|mistress(?:es)?|masters?|lords?|"
    r"kings?|queens?|princess(?:es)?|princes?|emperors?|empress(?:es)?|"
    r"majesty|highness|"
    r"dukes?|duchess(?:es)?|countess(?:es)?|counts?|baroness(?:es)?|barons?|"
    r"marquis|knights?|"
    r"maids?|butlers?|servants?|guards?|soldiers?|captains?|commanders?|leaders?|"
    r"bosses|boss|managers?|hosts?|producers?|secretar(?:y|ies)|assistants?|"
    # Genre role nouns. These novels are set in games, raids and guilds, where "a
    # returning player" or "the healer" names a person as plainly as "the captain" does —
    # and the second party in a scene is often referred to only that way. ("District
    # Governor shouted… HE'd muttered about being a returning player" is 0, not her.)
    r"players?|users?|gamers?|streamers?|viewers?|tanks?|healers?|espers?|guides?|"
    r"hunters?|summoners?|awakened|contestants?|"
    r"teachers?|professors?|students?|doctors?|nurses?|priestess(?:es)?|priests?|"
    r"nuns?|monks?)\b",
    re.I)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")

# Capitalized words that say nothing about a second person being present.
_HARMLESS_CAPS = {
    "i", "i'm", "i'll", "i'd", "i've", "ok", "okay", "oh", "ah", "ha", "huh", "hm",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


def _has_other_proper_noun(text: str) -> bool:
    """True when a capitalized word appears that is not sentence-initial.

    Glossary coverage is never complete — ``relevant_to`` only returns entries whose
    Korean occurs in the source, and plenty of characters are never added at all — so
    a known-name list alone cannot tell us whether a paragraph mentions someone else.
    In web-novel prose a mid-sentence capital is nearly always another character.
    """
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text):
        for word in _WORD_RE.findall(sentence)[1:]:  # skip the sentence-initial capital
            if word[0].isupper() and word.lower() not in _HARMLESS_CAPS:
                return True
    return False


def _from_first_mention(para: str, name: re.Pattern) -> str:
    """The paragraph from the start of the SENTENCE that first names the character.

    A pronoun can point backwards to a name in its own sentence — "For all HER talk,
    Ihyeon never once looked back" is Ihyeon — so the sentence is the unit, not the name's
    position within it. But a pronoun in an EARLIER sentence belongs to whoever that
    sentence was about: "…like HE was checking whether I'd been waiting for HIM… Then Seo
    Jihye came to mind" introduces her afterwards, as a new topic, and those two masculine
    pronouns are the man the narrator had been thinking about all along.
    """
    m = name.search(para)
    if m is None:
        return ""
    start = 0
    for boundary in re.finditer(r"(?<=[.!?…])[\s\"'”’]+|\n+", para[:m.start()]):
        start = boundary.end()
    return para[start:]


def _mentioned_only_as_possessive(para: str, english: str) -> bool:
    """True when every mention of the name in this paragraph is a possessive.

    "…there had to be a reason she, QUOKKA'S PARTNER no less, had gone out of HER way"
    names Quokka only to identify somebody else. The paragraph is grammatically about
    that other person, so its pronouns are hers, not his — but a bare name-match reads
    it as three counts of Quokka being called "she".
    """
    bare = re.compile(r"\b" + re.escape(english) + r"\b(?![’']s?\b)")
    return not bare.search(para)


def _is_speech_by(para: str, english: str) -> bool:
    """True when this is a chat/script line SPOKEN BY the character ("Clover: …").

    Chat-log chapters label each line with its speaker, and what a character says is
    almost always about somebody else: "Clover: Beat HIM to it… I'll bury HIM" counts
    three masculine pronouns against Clover, when they belong to the man she is
    plotting against. A speaker's own lines are no evidence of the speaker's gender.
    """
    return bool(re.search(r"^[ \t>*_-]*" + re.escape(english) + r"\s*[:：]", para, re.M))


# Every pronoun a human can pin. Wider than _PRONOUN_FORMS, which only lists the two the
# chapter check can actually test — but a "they" pin still contradicts a "he" pin, and
# comparing only he/she made a three-way disagreement look like agreement.
_PINNED_PRONOUNS = ("he", "she", "they")

# One character often has several Korean spellings, sometimes packed into one field as
# "이시윤 / 시윤".
_KOREAN_SPLIT = re.compile(r"[/,·|]|\s{2,}")


def _identifiers(entry) -> set[str]:
    """Every handle that refers to this character: the English name and each Korean term."""
    ids = {entry.english}
    for part in _KOREAN_SPLIT.split(getattr(entry, "korean", "") or ""):
        part = part.strip()
        if part:
            ids.add(part)
    return ids


def contradictory_pronouns(glossary) -> list[str]:
    """Names the glossary tags with two different pronouns at once.

    A character mapped under several Korean spellings gets several entries, and nothing
    stops one of them being marked *he* while another says *she*. Such a name can never be
    satisfied — whichever pronoun the chapter uses contradicts the other entry — so it
    would be flagged forever, and the repair pass would be handed two opposite
    instructions for the same character. The glossary is what needs fixing, so these are
    reported instead of being blamed on the chapter.

    Matching on the English name alone missed the common case, because the same person is
    often filed under different romanizations: "Lee Shiyun" (이시윤) and "Lee Siyoon"
    (이시윤 / 시윤) were both *he* while "Shiyoon" (시윤) was *they*, and nothing noticed,
    because no two of those share an English name. Entries are therefore grouped by every
    handle they carry — the English name AND each Korean term — so a disagreement reached
    through the Korean is caught too.
    """
    by_id: dict[str, set[str]] = {}
    names_by_id: dict[str, set[str]] = {}
    for e in glossary or []:
        if (getattr(e, "type", "") == "name" and getattr(e, "english", "")
                and getattr(e, "pronoun", "") in _PINNED_PRONOUNS):
            for key in _identifiers(e):
                by_id.setdefault(key, set()).add(e.pronoun)
                names_by_id.setdefault(key, set()).add(e.english)
    bad: set[str] = set()
    for key, prons in by_id.items():
        if len(prons) > 1:
            bad |= names_by_id[key]   # every spelling involved, so none is trusted
    return sorted(bad)


# How much wrong-gender evidence is needed before a chapter is called mis-gendered.
# The failure worth catching is systematic — the character reads as the wrong gender
# for most of the chapter — and clears both bars easily. One or two strays that the
# correct pronoun outnumbers are attribution noise, not a translation error.
_MIN_WRONG_HITS = 2


def _pronoun_conflicts(translation: str, glossary) -> list[PronounConflict]:
    """Names the chapter systematically renders as the wrong gender.

    Korean omits pronouns, so a chapter translated before a character's ``pronoun``
    was pinned can render them as the wrong gender for pages at a time. That is the
    failure this catches, and it is loud: the wrong pronoun repeats and the right one
    barely appears.

    Attributing a pronoun to a character is only safe in a paragraph that mentions
    nobody else, so paragraphs naming another person — known to the glossary or not —
    are not counted at all. What survives is weighed both ways: the correct pronoun is
    tallied alongside the wrong one, and a name is reported only when the wrong one
    repeats AND outnumbers it.
    """
    ambiguous = set(contradictory_pronouns(glossary))
    tagged: list = []
    by_name: set[str] = set()
    for e in glossary or []:
        if (getattr(e, "type", "") == "name"
                and getattr(e, "pronoun", "") in _PRONOUN_FORMS
                and getattr(e, "english", "")
                and e.english not in by_name        # one name, several Korean spellings
                and e.english not in ambiguous):
            by_name.add(e.english)
            tagged.append(e)
    if not tagged:
        return []

    # Every character the glossary knows, pronoun or not — the widest "somebody else is
    # in this paragraph" signal available before falling back to capitalization.
    known = {e.english for e in (glossary or [])
             if getattr(e, "type", "") == "name" and getattr(e, "english", "")}

    conflicts: list[PronounConflict] = []
    for entry in tagged:
        name = re.compile(r"\b" + re.escape(entry.english) + r"\b")
        others = [re.compile(r"\b" + re.escape(n) + r"\b")
                  for n in known if n != entry.english]
        wrong = _PRONOUN_FORMS["she" if entry.pronoun == "he" else "he"]
        right = _PRONOUN_FORMS[entry.pronoun]
        wrong_hits = right_hits = 0
        for para in _paragraphs(translation):
            if not name.search(para):
                continue
            # Named only to identify someone else ("Quokka's partner") — the paragraph
            # is about that other person.
            if _mentioned_only_as_possessive(para, entry.english):
                continue
            # The character's own chat-log line: they are talking ABOUT other people.
            if _is_speech_by(para, entry.english):
                continue
            rest = name.sub("", para)
            # Evidence that the character IS gendered correctly counts from every
            # paragraph that names them. Evidence that they are gendered WRONGLY has to
            # clear the ambiguity guards below first.
            #
            # The burden is deliberately asymmetric, because the two errors do not cost
            # the same: a missed conflict leaves a chapter as it is, while a false one
            # sends the repair pass in to "fix" prose that was already right — flipping
            # the pronouns of whoever the scene partner was. Counting the correct
            # pronoun only in the strictly-filtered paragraphs threw away most of the
            # proof that a chapter is fine.
            right_hits += len(right.findall(rest))
            # Anyone else in the paragraph makes every pronoun in it ambiguous.
            if any(o.search(rest) for o in others):
                continue
            if _PERSON_NOUN_RE.search(rest) or _has_other_proper_noun(rest):
                continue
            # Pronouns in sentences BEFORE the character is first named are somebody
            # else's (see _from_first_mention).
            w = len(wrong.findall(name.sub("", _from_first_mention(para, name))))
            # Both genders inside one paragraph: at least one set belongs to somebody
            # else, and nothing here says which. The two-hander scenes these novels are
            # built from ("Yuna grinned and linked HER arm through HIS") are the common
            # case, and counting the man's pronouns against the woman he is standing
            # next to is how a correctly translated chapter gets flagged.
            if w and right.search(rest):
                continue
            wrong_hits += w
        if wrong_hits >= _MIN_WRONG_HITS and wrong_hits > right_hits:
            conflicts.append(PronounConflict(name=entry.english,
                                             expected=entry.pronoun, hits=wrong_hits))
    return conflicts


def validate_translation(
    chapter: Chapter, translation: str, cfg: ValidationConfig, glossary=None
) -> ValidationResult:
    failures: list[str] = []
    warnings: list[str] = []

    src = chapter.metrics
    out_paras = _paragraphs(translation)
    out_para_count = len(out_paras)
    out_dialogue = sum(1 for p in out_paras if _QUOTE_RE.search(p))
    out_chars = _nonspace_len(translation)
    ratio = (out_chars / src.char_count) if src.char_count else 0.0

    metrics = {
        "source_paragraphs": src.paragraph_count,
        "output_paragraphs": out_para_count,
        "source_dialogue": src.dialogue_count,
        "output_dialogue": out_dialogue,
        "source_chars": src.char_count,
        "output_chars": out_chars,
        "length_ratio": round(ratio, 3),
    }

    # 0. Reasoning-leak check (hard fail). The model must never leave "thinking out
    #    loud" — meta-commentary, glossary chatter, or a redo draft — in the prose.
    leaks = find_leaks(translation)
    if leaks:
        failures.append(f"AI reasoning/notes leaked into the text (e.g. “{leaks[0][:80]}”)")
    #    …and it must not leave untranslated Korean source behind. Two signals, both
    #    counting composed Hangul syllables only (never emoticon jamo like ㅠㅠ/ㅋㅋ, which
    #    these novels legitimately keep in chat scenes): (a) a large fraction of the WHOLE
    #    chapter is Korean — wholesale untranslated; (b) a stray Korean phrase/run remains
    #    even in an otherwise-English chapter — a leaked source sentence the whole-chapter
    #    fraction is too coarse to notice. Either way we FLAG for review, never delete.
    if korean_fraction(translation) > 0.10:
        failures.append("substantial untranslated Korean remains in the output")
    elif has_korean_leak(translation):
        failures.append("stray untranslated Korean remains in the output — review before publishing")

    # 1. Paragraph-count check (structural). Tolerance scales with chapter length so
    #    minor formatting merges don't flag, but a missing scene still does.
    para_tol = max(cfg.paragraph_tolerance, round(src.paragraph_count * cfg.paragraph_tolerance_pct))
    if abs(out_para_count - src.paragraph_count) > para_tol:
        failures.append(
            f"paragraph count {out_para_count} vs source {src.paragraph_count} "
            f"(tolerance {para_tol})"
        )

    # 2. Length-ratio check (omission / embellishment signal).
    if not src.char_count:
        warnings.append("source has zero counted characters; skipping length-ratio check")
    elif ratio < cfg.length_ratio_min:
        failures.append(
            f"length ratio {ratio:.2f} below {cfg.length_ratio_min} — likely omission/summarizing"
        )
    elif ratio > cfg.length_ratio_max:
        failures.append(
            f"length ratio {ratio:.2f} above {cfg.length_ratio_max} — likely embellishment"
        )

    # 3. Dialogue-line check (secondary signal — warn, don't fail).
    if abs(out_dialogue - src.dialogue_count) > cfg.dialogue_tolerance:
        warnings.append(
            f"dialogue lines {out_dialogue} vs source {src.dialogue_count} "
            f"(tolerance {cfg.dialogue_tolerance})"
        )

    # 4. Surface sanity (lightweight regex).
    if re.search(r"(?<!\.)\.\.(?!\.)", translation) or re.search(r"\.{4,}", translation):
        warnings.append("found ellipses that are not exactly three dots")
    if "…" in translation:
        warnings.append("found unicode ellipsis '…' (should be three ASCII dots)")
    if out_dialogue and ('"' in translation) and not re.search(r"[“”]", translation):
        warnings.append("dialogue present but no curly quotes found (straight quotes?)")

    # 5. Character-gender check. The glossary pronoun is authoritative (see
    #    prompts.py); contradicting it means the chapter mis-genders someone,
    #    which is invisible to every check above. Recorded twice: as prose in
    #    `failures` (what the user reads) and as data in `pronoun_conflicts` (what
    #    the pronoun repair and the corrective re-translate act on).
    conflicts = _pronoun_conflicts(translation, glossary)
    for conflict in conflicts:
        failures.append(conflict.message())
    #    A name the glossary tags both ways can't be checked at all, and no repair could
    #    ever satisfy it. Warn so the glossary gets fixed, rather than failing chapters
    #    for a contradiction they didn't cause.
    for name in contradictory_pronouns(glossary):
        warnings.append(
            f"“{name}” is listed in the glossary with two different pronouns — "
            f"set them all to the same one so this character can be checked")

    return ValidationResult(
        ok=not failures, failures=failures, warnings=warnings, metrics=metrics,
        pronoun_conflicts=[asdict(c) for c in conflicts],
    )

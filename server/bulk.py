"""Pure partitioning logic behind the glossary bulk-add endpoint.

Bulk add accepts structured rows (full entries, per-type groups, spreadsheet
pastes) as well as the legacy flat list, so the expensive part — deciding what
each row *does* — lives here, free of FastAPI and the Anthropic client, where
tests can pin the semantics down without a server or a config.toml.

The contract that matters for the user's plan allowance: a row that arrives
with a recognizable type is added directly; only rows with no usable type are
sent to a single classify_terms call, and if there are none the endpoint never
constructs a Translator at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from translation_bot.glossary import GlossaryEntry, normalize_pronoun

MAX_ROWS = 2000     # sanity cap on one request
MAX_UNTYPED = 200   # one classify call — hundreds of terms risks truncated JSON
MAX_TERM_LEN = 100  # longer "terms" are almost always un-delimited prose

# Labels people actually paste ("characters", "locations", "curses", …) mapped
# onto the five glossary types, following TERM_CLASSIFY_PROMPT's definitions:
# spells/curses are skills; effects, laws, items, and named groups are terms;
# codenames/aliases are names. Deliberately absent: "unknown"/"auto"/"" — those
# mean "no type given", which must classify rather than silently become "other".
# Mirror of TYPE_SYNONYMS in web/src/glossary-parse.js — update both together.
_TYPE_SYNONYMS: dict[str, str] = {
    "abilities": "skill",
    "ability": "skill",
    "alias": "name",
    "aliases": "name",
    "armor": "term",
    "art": "skill",
    "arts": "skill",
    "beast": "term",
    "beasts": "term",
    "buff": "term",
    "buffs": "term",
    "character": "name",
    "characters": "name",
    "cities": "place",
    "city": "place",
    "class": "skill",
    "classes": "skill",
    "codename": "name",
    "codenames": "name",
    "concept": "term",
    "concepts": "term",
    "countries": "place",
    "country": "place",
    "cultural term": "term",
    "curse": "skill",
    "curses": "skill",
    "debuff": "term",
    "debuffs": "term",
    "dungeon": "place",
    "dungeons": "place",
    "faction": "term",
    "factions": "term",
    "group": "term",
    "groups": "term",
    "guild": "term",
    "guilds": "term",
    "herb": "term",
    "herbs": "term",
    "item": "term",
    "items": "term",
    "kingdom": "place",
    "kingdoms": "place",
    "law": "term",
    "laws": "term",
    "location": "place",
    "locations": "place",
    "magic": "skill",
    "misc": "other",
    "miscellaneous": "other",
    "monster": "term",
    "monsters": "term",
    "name": "name",
    "names": "name",
    "nickname": "name",
    "nicknames": "name",
    "npc": "name",
    "npcs": "name",
    "object": "term",
    "objects": "term",
    "organisation": "term",
    "organisations": "term",
    "organization": "term",
    "organizations": "term",
    "other": "other",
    "others": "other",
    "people": "name",
    "person": "name",
    "place": "place",
    "places": "place",
    "plan": "term",
    "plans": "term",
    "plant": "term",
    "plants": "term",
    "potion": "term",
    "potions": "term",
    "procedure": "term",
    "procedures": "term",
    "protagonist": "name",
    "protagonists": "name",
    "race": "term",
    "races": "term",
    "rank": "term",
    "ranks": "term",
    "realm": "place",
    "realms": "place",
    "region": "place",
    "regions": "place",
    "regulation": "term",
    "regulations": "term",
    "skill": "skill",
    "skills": "skill",
    "slang": "term",
    "spell": "skill",
    "spells": "skill",
    "status": "term",
    "statuses": "term",
    "system": "term",
    "systems": "term",
    "technique": "skill",
    "techniques": "skill",
    "term": "term",
    "terms": "term",
    "title": "term",
    "titles": "term",
    "town": "place",
    "towns": "place",
    "weapon": "term",
    "weapons": "term",
    "world": "place",
    "worlds": "place",
}

_PRONOUN_SYNONYMS = {"male": "he", "m": "he", "female": "she", "f": "she"}


def normalize_bulk_type(raw: object) -> str:
    """A valid glossary type, or "" meaning "no usable type — auto-classify".

    Unlike the single-term editor's fallback to "other", an unrecognized label
    here must stay empty: the whole point of bulk add is that unlabeled rows
    get classified instead of landing as a silent "other"."""
    return _TYPE_SYNONYMS.get(str(raw or "").strip().lower(), "")


def normalize_bulk_pronoun(raw: object) -> str:
    """Bulk pastes come from spreadsheets and LLM output, so accept the
    male/female shorthands; everything else goes through normalize_pronoun
    (junk → "")."""
    p = str(raw or "").strip().lower()
    return normalize_pronoun(_PRONOUN_SYNONYMS.get(p, p))


def split_flat(text: str) -> list[str]:
    """Split a legacy flat paste on commas/newlines/semicolons — the pre-array
    wire format. Dedup is prepare_bulk_rows' job, not the splitter's."""
    return [t for t in (raw.strip() for raw in re.split(r"[,\n;]", text or "")) if t]


@dataclass
class BulkPlan:
    """What a bulk paste will do — decided before any model call or save."""

    typed: list[GlossaryEntry] = field(default_factory=list)    # add directly, no LLM
    untyped: list[dict] = field(default_factory=list)           # await one classify call
    updated: list[GlossaryEntry] = field(default_factory=list)  # placeholder upgrades
    skipped: list[dict] = field(default_factory=list)           # {english, korean, reason}


def prepare_bulk_rows(rows: list[dict], existing: list[GlossaryEntry]) -> BulkPlan:
    """Partition pasted rows into direct adds, rows needing classification,
    placeholder upgrades, and skips.

    Bulk add never overwrites a mapped entry: respelling goes through the
    single-term editor, which warns about already-translated chapters going
    stale — a silent bulk respell would bypass that safety net. The one
    mutation allowed is upgrading an English-only placeholder when a row
    supplies its Korean; the placeholder's curated fields fill in whatever the
    row left blank (notably its type, so upgrades never need a classify call).
    """
    plan = BulkPlan()
    by_english = {e.english.lower(): e for e in existing if e.english}
    by_korean = {e.korean: e for e in existing if e.korean}
    seen_english: set[str] = set()
    seen_korean: set[str] = set()

    for raw in rows:
        korean = str(raw.get("korean", "") or "").strip()
        english = str(raw.get("english", "") or "").strip()
        typ = normalize_bulk_type(raw.get("type", ""))
        note = str(raw.get("note", "") or "").strip()
        pronoun = normalize_bulk_pronoun(raw.get("pronoun", ""))
        register = str(raw.get("register", "") or "").strip()

        def skip(reason: str) -> None:
            plan.skipped.append({"english": english, "korean": korean, "reason": reason})

        if not english and not korean:
            continue  # blank row
        if not english:
            skip("needs an English spelling")
            continue
        if len(english) > MAX_TERM_LEN:
            skip("too long — looks like prose, not a term")
            continue
        if english.lower() in seen_english or (korean and korean in seen_korean):
            skip("duplicate in this paste")
            continue
        if korean and korean in by_korean:
            skip(f"Korean term already mapped to “{by_korean[korean].english}”")
            continue

        cur = by_english.get(english.lower())
        if cur is not None:
            if cur.korean or not korean:
                skip("already in glossary")
                continue
            plan.updated.append(GlossaryEntry(
                korean=korean,
                english=english,
                type=typ or cur.type,
                note=note or cur.note,
                pronoun=pronoun or cur.pronoun,
                register=register or cur.register,
            ))
            seen_english.add(english.lower())
            seen_korean.add(korean)
            continue

        seen_english.add(english.lower())
        if korean:
            seen_korean.add(korean)
        if typ:
            plan.typed.append(GlossaryEntry(korean=korean, english=english, type=typ,
                                            note=note, pronoun=pronoun, register=register))
        else:
            plan.untyped.append({"korean": korean, "english": english, "note": note,
                                 "pronoun": pronoun, "register": register})
    return plan

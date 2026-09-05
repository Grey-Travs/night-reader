#!/usr/bin/env python
"""Repair mis-gendered pronouns for male characters in translated chapters.

Chapters translated before a character's ``pronoun`` was pinned in
``glossary.json`` had their gender guessed per-chapter by the model. For this
novel that means Ollia (a male priest) and Yorheim (likewise) are referred to
as she/her across a number of chapters.

This script rewrites *only* pronoun tokens whose referent it can establish, and
leaves every genuinely female character (Heilon, Roan, Lora, Merin, Roselia,
Naria, Loan, Eria, ...) untouched.

It is a dry run by default: nothing is written without ``--apply``.

    python tools/fix_pronouns.py                       # dry run, all targets
    python tools/fix_pronouns.py --chapter 61          # one chapter
    python tools/fix_pronouns.py --show-ambiguous      # list what it skipped
    python tools/fix_pronouns.py --apply               # write (makes a backup)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import pathlib
import re
import shutil
import sys

PROJECTS = pathlib.Path(__file__).resolve().parent.parent / "projects"

# --------------------------------------------------------------------------
# Characters
# --------------------------------------------------------------------------

# Male characters that the translator mis-gendered, and the chapters where that
# was confirmed by manual review. Chapters absent from these lists are correct
# (their she/her belongs to real women) and are never touched unless --scan-all.
TARGETS = {
    "ollia": {
        "pattern": re.compile(r"\bOllia\b|\bSaperdo\b"),
        "chapters": {
            "099de743f10a": {"01", "02", "04", "09", "10", "25", "28", "31",
                             "35", "38", "40", "42", "59", "61", "63", "65",
                             "68", "70", "77", "83", "92", "93"},
            "411cbcfcf239": {"041", "049", "051", "061", "066", "081", "087",
                             "099", "100"},
        },
    },
    "yorheim": {
        "pattern": re.compile(r"\bYorheim\b"),
        "chapters": {
            "099de743f10a": {"65", "67", "68", "70"},
            "411cbcfcf239": set(),
        },
    },
}

# Anything here can legitimately own a "she/her". If one of these shares a
# paragraph with the target, the paragraph is ambiguous and is left alone.
FEMALE_ENTITY = re.compile(
    r"\b(?:Roan|Heilon|Lora|Naria|Merin|Loan|Roselia|Rozelia|Eria|Constance|"
    r"Ellie|Julie|Sera|Milli|"
    r"[Pp]rincess|Highness|Empress|Queen|Madam|Mistress|"
    r"wom[ae]n|girls?|lady|ladies|mother|mom|wi[fv]e(?:s)?|widow|maids?|"
    r"priestess|sisters?|daughters?|aunt|grandmother|niece|nun|female|"
    r"waitress|hostess)\b")

# People of unknown/unstated gender. These *can* own a "she/her", so their
# presence makes a paragraph ambiguous. Confirmed male characters are
# deliberately absent: Cassian standing next to Ollia cannot own a "her", so
# treating him as a competitor would block almost every real fix.
AMBIG_PERSON = re.compile(
    r"\b(?:childr?e?n?|kid|mage|magician|knight|guard|attendant|servant|"
    r"patient|victim|villagers?|magistrate|acolyte|physician|healer|"
    r"apprentice|merchant|innkeeper|soldier|someone|somebody|person|people|"
    r"stranger|figure|corpse|body|companion|partner|friend|noble|commoner)\b",
    re.I)

# --------------------------------------------------------------------------
# Pronoun rewriting
# --------------------------------------------------------------------------

# "her" is either a possessive determiner ("her eyes" -> his) or an object
# pronoun ("beside her" -> him). Anything in this set following "her" means the
# object reading; a noun or adjective means the possessive reading.
OBJECT_FOLLOWERS = {
    # prepositions / conjunctions / complementisers
    "to", "and", "as", "in", "from", "with", "for", "on", "at", "by", "but",
    "or", "so", "if", "that", "than", "then", "when", "while", "until", "till",
    "before", "after", "about", "against", "into", "onto", "over", "under",
    "through", "toward", "towards", "like", "of", "off", "out", "up", "down",
    "away", "back", "along", "around", "across", "behind", "beside", "between",
    "because", "since", "though", "although", "unless", "whether",
    # verbs / auxiliaries that cannot follow a determiner
    "was", "were", "is", "are", "had", "has", "have", "would", "will", "could",
    "can", "should", "shall", "did", "does", "do", "go", "went", "come",
    "came", "again", "too", "not", "no", "yet", "still", "once", "just",
    "now", "here", "there", "ever", "never", "how", "why", "what",
    # determiners (an object pronoun followed by a new NP)
    "a", "an", "the", "this", "these", "those",
}

# Words that are both particles and body-part nouns: "pulled her back" (object)
# vs "patted her back" (possessive). Resolved by looking at the preceding verb.
AMBIGUOUS_FOLLOWERS = {"back", "side", "front", "top", "middle"}

# Verbs of physical contact take a possessive body part: "patted her back".
CONTACT_VERB = re.compile(
    r"\b(?:pat(?:ted|ting)?|strok(?:e|ed|ing)|rub(?:bed|bing)?|touch(?:ed|ing)?|"
    r"slap(?:ped|ping)?|scratch(?:ed|ing)?|caress(?:ed|ing)?|massag(?:e|ed|ing)|"
    r"brush(?:ed|ing)?|grip(?:ped|ping)?|grasp(?:ed|ing)?|clutch(?:ed|ing)?|"
    r"kiss(?:ed|ing)?|press(?:ed|ing)?)\s*$", re.I)

_APOSTROPHES = "'’"


def _match_case(new: str, old: str) -> str:
    """Carry ``old``'s capitalisation onto ``new``."""
    if old[:1].isupper():
        return new[:1].upper() + new[1:]
    return new


def _her_is_possessive(text: str, end: int, start: int = 0) -> bool:
    """Decide whether the "her" ending at ``end`` is possessive.

    ``her eyes`` -> possessive (his);  ``beside her.`` -> object (him).
    """
    rest = text[end:]
    # Capture the whole following token, hyphens included, so a compound
    # modifier ("now-bared", "half-open") is not mistaken for its first part.
    m = re.match(r"\s*([A-Za-z][\w" + _APOSTROPHES + r"\-]*)", rest)
    if not m:
        return False                       # end of line / punctuation -> object
    word = m.group(1).lower().strip(_APOSTROPHES + "-")
    if "-" in word:
        return True                        # compound adjective -> possessive
    if word in AMBIGUOUS_FOLLOWERS:
        # "patted her back" (body part) vs "pulled her back" (particle).
        return bool(CONTACT_VERB.search(text[:start]))
    if word in OBJECT_FOLLOWERS:
        return False
    return True                            # noun or adjective -> possessive


def _rewrite(text: str) -> tuple[str, list[str]]:
    """Rewrite every female pronoun in ``text`` to its male form."""
    notes: list[str] = []
    out: list[str] = []
    pos = 0
    pattern = re.compile(
        r"\b([Ss]he|[Hh]er|[Hh]ers|[Hh]erself)\b([" + _APOSTROPHES + r"]\w+)?")
    for m in pattern.finditer(text):
        out.append(text[pos:m.start()])
        word, clitic = m.group(1), m.group(2) or ""
        low = word.lower()
        if low == "she":
            new = _match_case("he", word)
        elif low == "herself":
            new = _match_case("himself", word)
        elif low == "hers":
            new = _match_case("his", word)
        else:                              # "her"
            if _her_is_possessive(text, m.end(1), m.start(1)):
                new = _match_case("his", word)
            else:
                new = _match_case("him", word)
                notes.append(f"her->him before {text[m.end(1):m.end(1)+14]!r}")
        out.append(new + clitic)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), notes


# --------------------------------------------------------------------------
# Referent resolution
# --------------------------------------------------------------------------

FEM_PRON = re.compile(r"\b(?:[Ss]he|[Hh]er|[Hh]ers|[Hh]erself)\b")
MALE_PRON = re.compile(r"\b(?:[Hh]e|[Hh]is|[Hh]im|[Hh]imself)\b")
HER_HIGHNESS = re.compile(r"\b[Hh]er\s+Highness\b")


def _competitors(para: str, target: re.Pattern) -> bool:
    """True if someone other than the target could own a *female* pronoun here.

    Only females and people of unstated gender count; a male bystander cannot
    be the referent of "she/her" and so is not a competitor.
    """
    stripped = target.sub("", para)
    return bool(FEMALE_ENTITY.search(stripped) or AMBIG_PERSON.search(stripped))


def resolve(paras: list[str], idx: int, target: re.Pattern,
            lookback: int) -> tuple[str, str]:
    """Classify the female pronouns in ``paras[idx]``.

    Returns ``(verdict, reason)`` where verdict is one of
    ``convert`` / ``skip`` / ``ambiguous``.
    """
    para = paras[idx]
    named = bool(target.search(para))
    rival = _competitors(para, target)

    if named and not rival:
        return "convert", "target named, sole referent"
    if named and rival:
        return "ambiguous", "target shares paragraph with another person"
    if rival:
        return "skip", "another person present, target not named"

    # Nobody named here — walk back for the nearest antecedent.
    for back in range(1, lookback + 1):
        j = idx - back
        if j < 0:
            break
        prev = paras[j]
        t_here = bool(target.search(prev))
        r_here = _competitors(prev, target)
        if t_here and not r_here:
            return "convert", f"nearest antecedent is target (-{back}¶)"
        if t_here and r_here:
            return "ambiguous", f"mixed antecedents (-{back}¶)"
        if r_here:
            return "skip", f"nearest antecedent is another person (-{back}¶)"
    return "ambiguous", "no antecedent within lookback"


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def process(text: str, target: re.Pattern, lookback: int,
            sole_female: bool) -> tuple[str, list[dict], list[dict]]:
    """Return ``(new_text, applied, ambiguous)`` for one chapter."""
    lines = text.split("\n")
    # Index of non-blank lines, so lookback counts paragraphs not blank lines.
    para_idx = [i for i, ln in enumerate(lines) if ln.strip()]
    paras = [lines[i] for i in para_idx]

    applied: list[dict] = []
    ambiguous: list[dict] = []

    for n, i in enumerate(para_idx):
        para = paras[n]
        if not FEM_PRON.search(para):
            continue
        if sole_female:
            verdict, reason = "convert", "no other female in chapter"
        else:
            verdict, reason = resolve(paras, n, target, lookback)

        if verdict != "convert":
            ambiguous.append({"line": i + 1, "verdict": verdict,
                              "reason": reason, "text": para})
            continue

        # "Her Highness" is always the princess — protect it even inside a
        # paragraph we are otherwise converting.
        holds: list[str] = []

        def _hold(m: re.Match) -> str:
            holds.append(m.group(0))
            return f"\x00{len(holds)-1}\x00"

        guarded = HER_HIGHNESS.sub(_hold, para)
        new, notes = _rewrite(guarded)
        new = re.sub(r"\x00(\d+)\x00", lambda m: holds[int(m.group(1))], new)

        if new != para:
            lines[i] = new
            # If the paragraph already contained male pronouns for someone
            # else (usually Cassian), converting the target's pronouns makes
            # both people "he". That is correct but can read ambiguously, so
            # mark it for a human pass.
            applied.append({"line": i + 1, "reason": reason, "notes": notes,
                            "collision": bool(MALE_PRON.search(para)),
                            "before": para, "after": new})

    return "\n".join(lines), applied, ambiguous


def chapter_key(path: pathlib.Path) -> str:
    m = re.search(r"chapter-(\d+)", path.name)
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", action="append",
                    help="project id (default: both known projects)")
    ap.add_argument("--chapter", action="append",
                    help="restrict to chapter number(s), e.g. --chapter 61")
    ap.add_argument("--target", action="append", choices=sorted(TARGETS),
                    help="character(s) to fix (default: all)")
    ap.add_argument("--scan-all", action="store_true",
                    help="consider every chapter, not just the verified list")
    ap.add_argument("--lookback", type=int, default=8,
                    help="paragraphs of context for referent resolution (8)")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry run)")
    ap.add_argument("--diff", action="store_true", help="print a unified diff")
    ap.add_argument("--show-ambiguous", action="store_true",
                    help="print paragraphs that were skipped for review")
    ap.add_argument("--show-collisions", action="store_true",
                    help="print converted paragraphs where two men now share "
                         "'he', which may read ambiguously")
    ap.add_argument("--report", metavar="PATH",
                    help="write the full change set to a JSON file")
    args = ap.parse_args()

    targets = args.target or sorted(TARGETS)
    projects = args.project or ["099de743f10a", "411cbcfcf239"]
    backup_root = None
    if args.apply:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = PROJECTS / f"_pronoun_backup_{stamp}"

    grand = {"changed": 0, "paras": 0, "tokens": 0, "ambiguous": 0,
             "collisions": 0}
    summary: list[str] = []
    record: dict[str, dict] = {}

    for proj in projects:
        cdir = PROJECTS / proj / "chapters"
        if not cdir.is_dir():
            print(f"!! no such project: {proj}", file=sys.stderr)
            continue
        for path in sorted(cdir.glob("chapter-*.md")):
            key = chapter_key(path)
            if args.chapter and key.lstrip("0") not in {
                    c.lstrip("0") for c in args.chapter}:
                continue

            text = path.read_text(encoding="utf-8")
            original = text
            all_applied: list[dict] = []
            all_ambig: list[dict] = []

            for tname in targets:
                spec = TARGETS[tname]
                if not args.scan_all and key not in spec["chapters"].get(proj, set()):
                    continue
                # A chapter with no other female at all: every she/her is the
                # target's, so resolution can be skipped entirely.
                sole = not FEMALE_ENTITY.search(text)
                text, applied, ambig = process(text, spec["pattern"],
                                               args.lookback, sole)
                for a in applied:
                    a["target"] = tname
                for a in ambig:
                    a["target"] = tname
                all_applied += applied
                all_ambig += ambig

            if not all_applied:
                continue

            tok = sum(len(FEM_PRON.findall(a["before"])) for a in all_applied)
            coll = sum(1 for a in all_applied if a["collision"])
            grand["changed"] += 1
            grand["paras"] += len(all_applied)
            grand["tokens"] += tok
            grand["ambiguous"] += len(all_ambig)
            grand["collisions"] += coll
            summary.append(f"  {proj[:3]}/{path.name:<16} "
                           f"paragraphs={len(all_applied):<4} tokens={tok:<4} "
                           f"skipped={len(all_ambig):<4} collisions={coll}")
            record[f"{proj}/{path.name}"] = {"applied": all_applied,
                                             "ambiguous": all_ambig}

            if args.diff:
                print(f"\n{'='*72}\n{proj}/{path.name}\n{'='*72}")
                for d in difflib.unified_diff(
                        original.split("\n"), text.split("\n"),
                        fromfile="before", tofile="after", lineterm="", n=0):
                    print(d)

            if args.show_ambiguous and all_ambig:
                print(f"\n--- skipped in {proj}/{path.name} ---")
                for a in all_ambig:
                    print(f"  L{a['line']} [{a['verdict']}: {a['reason']}]")
                    print(f"     {a['text'][:150]}")

            if args.show_collisions:
                hits = [a for a in all_applied if a["collision"]]
                if hits:
                    print(f"\n--- two men share 'he' in {proj}/{path.name} ---")
                    for a in hits:
                        print(f"  L{a['line']}\n     {a['after'][:200]}")

            if args.apply:
                dest = backup_root / proj
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest / path.name)
                path.write_text(text, encoding="utf-8")

    print("\n" + "=" * 72)
    print("DRY RUN — nothing written" if not args.apply
          else f"APPLIED — backup in {backup_root}")
    print("=" * 72)
    print("\n".join(summary))
    print(f"\n  chapters={grand['changed']}  paragraphs={grand['paras']}  "
          f"pronoun tokens={grand['tokens']}"
          f"\n  skipped-for-review={grand['ambiguous']}  "
          f"paragraphs where two men now share 'he'={grand['collisions']}")

    if args.report:
        pathlib.Path(args.report).write_text(
            json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\n  full change set written to {args.report}")
    if not args.apply:
        print("\n  re-run with --diff to inspect, --show-ambiguous to see skips,"
              "\n  then --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

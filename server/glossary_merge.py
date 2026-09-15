"""Merging the glossaries of a series' documents into one shared glossary.

This is the step that fixes a real bug rather than adding a convenience. A novel split
across several Google Docs keeps one glossary PER DOCUMENT, so the locked spelling of a
character name resets at chapter 101 and can drift from there — the exact thing the
glossary exists to prevent. Measured across ten of the user's series: 60 conflicts, worst
case 19 in a single series, including one character locked as both "Baek Jeongwoo" and
"Baek Jeong-u", and one term as both "marking" and "imprint bond".

Three things make this delicate:

* ``Glossary.add`` REPLACES an entry with the same Korean key and drops the matching
  English-only placeholder. Merging member by member would therefore mean the last
  document silently wins every disagreement. So conflicts are collected first, and only
  the chosen winner is ever added.
* English-only canonical entries dedupe by ``english.lower()``, so "Sylph" and "sylph"
  collapse into one and the evidence of a casing disagreement is destroyed. They are
  grouped case-insensitively here and the casing choice is surfaced explicitly.
* ``_queue_new_terms_locked`` builds its dedupe set by subscripting ``p["korean"]`` and
  ``p["english"]`` directly, so a merged pending item missing either key raises KeyError
  on the translation worker's thread, mid-chapter.

A member's own ``glossary.json`` is left exactly where it is and never rewritten. Once the
merge has run, ``project_config`` points the member at the series glossary instead, so its
own file is stale but untouched — which makes unlinking the series a zero-step rollback,
because the file that was live before the merge still holds the pre-merge state. The cost
of that choice: terms approved while a novel is part of a series live in the series
glossary, so detaching it returns the novel to its pre-merge names.
"""

from __future__ import annotations

from pathlib import Path

from translation_bot.glossary import (
    Glossary,
    GlossaryEntry,
    glossary_lock,
    load_pending,
    save_pending,
)

from . import projects as pj
from . import series as series_mod

# Conflict kinds, in the order a human most needs to see them.
KIND_ENGLISH = "english"    # same Korean, two different English spellings -- the drift bug
KIND_PRONOUN = "pronoun"    # agreed spelling, disagreeing pronoun
KIND_CASING = "casing"      # English-only name written two ways


def _members(series: dict) -> list[dict]:
    """Each member's own glossary and pending queue, in reading order.

    Always read from the member's own ``glossary.json``: it is never rewritten, so a
    second merge sees the same inputs as the first and the operation is idempotent. Two of
    the user's documents already hold byte-identical 346-entry glossaries (someone used
    copy_glossary), and those must merge to agreement, not to 346 conflicts.
    """
    out = []
    for member in series.get("members") or []:
        pid = member.get("project_id")
        project = pj.get_project(pid)
        if not project:
            continue
        pdir = pj.PROJECTS_DIR / pid
        out.append({
            "project_id": pid,
            "name": project.get("name") or pid,
            "dir": pdir,
            "glossary": Glossary.load(pdir / "glossary.json"),
            "pending": load_pending(pdir / "glossary_pending.json"),
        })
    return out


def _spelling_counts(members: list[dict], spellings: set[str]) -> dict[str, int]:
    """How many translated chapters each spelling actually appears in.

    Chapter files are found with a NON-RECURSIVE glob of each project's own ``chapters/``
    directory. 28 of the user's projects carry a stray ``export_artifacts_backup_*/``
    tree holding a full copy of every chapter, and five carry ``repin_backup_*/``, so an
    rglob here would count some chapters three times over and let whichever spelling
    happened to be backed up more decide every conflict.
    """
    counts = {s: 0 for s in spellings if s}
    if not counts:
        return counts
    for member in members:
        try:
            files = sorted((member["dir"] / "chapters").glob("chapter-*.md"))
        except OSError:
            continue
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for spelling in counts:
                if spelling in text:
                    counts[spelling] += 1
    return counts


def _candidates(pairs: list[tuple[dict, GlossaryEntry]]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for member, entry in pairs:
        key = (entry.english, entry.pronoun, entry.type)
        row = seen.get(key)
        if row is None:
            seen[key] = {
                "english": entry.english,
                "pronoun": entry.pronoun,
                "register": entry.register,
                "type": entry.type,
                "note": entry.note,
                "from": [member["name"]],
                "project_ids": [member["project_id"]],
            }
        else:
            row["from"].append(member["name"])
            row["project_ids"].append(member["project_id"])
            if entry.note and entry.note not in row["note"]:
                row["note"] = f"{row['note']} {entry.note}".strip()
    return list(seen.values())


def find_conflicts(members: list[dict]) -> list[dict]:
    """Every disagreement that needs a person to choose, with its evidence."""
    by_korean: dict[str, list[tuple[dict, GlossaryEntry]]] = {}
    by_english: dict[str, list[tuple[dict, GlossaryEntry]]] = {}
    for member in members:
        for entry in member["glossary"].entries():
            if entry.korean:
                by_korean.setdefault(entry.korean, []).append((member, entry))
            elif entry.english:
                by_english.setdefault(entry.english.casefold(), []).append((member, entry))

    conflicts: list[dict] = []
    for korean, pairs in by_korean.items():
        spellings = {e.english for _, e in pairs if e.english}
        pronouns = {e.pronoun for _, e in pairs if e.pronoun}
        if len(spellings) > 1:
            kind = KIND_ENGLISH
        elif len(pronouns) > 1:
            kind = KIND_PRONOUN
        else:
            continue  # full agreement -- nothing to ask
        conflicts.append({"kind": kind, "korean": korean, "candidates": _candidates(pairs)})
    for pairs in by_english.values():
        if len({e.english for _, e in pairs}) > 1:
            conflicts.append({"kind": KIND_CASING, "korean": "", "candidates": _candidates(pairs)})

    # Rank every conflict in one pass over the series' chapters rather than per conflict.
    spellings = {c["english"] for conf in conflicts for c in conf["candidates"]}
    counts = _spelling_counts(members, spellings)
    for conflict in conflicts:
        for candidate in conflict["candidates"]:
            candidate["in_chapters"] = counts.get(candidate["english"], 0)
        # The spelling readers have actually seen most wins; ties go to the earlier
        # document, because changing an established name costs more the earlier it was set.
        best = max(conflict["candidates"], key=lambda c: c["in_chapters"])
        conflict["suggested"] = best["english"]
        conflict["reason"] = (
            f"appears in {best['in_chapters']} translated chapters"
            if best["in_chapters"] else "from the earliest document"
        )
        conflict["chosen"] = None
    conflicts.sort(key=lambda c: (c["kind"] != KIND_ENGLISH, c["korean"] or ""))
    return conflicts


def _merged_glossary(members: list[dict], conflicts: list[dict],
                     picks: dict[str, str]) -> Glossary:
    """Build the one glossary. Conflicted keys are added ONCE, as the chosen winner."""
    decided: dict[str, str] = {}
    for conflict in conflicts:
        key = conflict["korean"] or conflict["candidates"][0]["english"].casefold()
        decided[key] = picks.get(key) or conflict["suggested"]

    merged = Glossary([])
    # Agreed entries first, so a conflicted key is never written by a plain add and then
    # overwritten by whichever document came last.
    conflicted_korean = {c["korean"] for c in conflicts if c["korean"]}
    conflicted_english = {
        c["candidates"][0]["english"].casefold() for c in conflicts if not c["korean"]
    }
    for member in members:
        for entry in member["glossary"].entries():
            if entry.korean and entry.korean in conflicted_korean:
                continue
            if not entry.korean and entry.english.casefold() in conflicted_english:
                continue
            merged.add(entry)

    for conflict in conflicts:
        key = conflict["korean"] or conflict["candidates"][0]["english"].casefold()
        winner = decided[key]
        chosen = next(
            (c for c in conflict["candidates"] if c["english"] == winner),
            conflict["candidates"][0],
        )
        merged.add(GlossaryEntry(
            korean=conflict["korean"],
            english=chosen["english"],
            type=chosen["type"],
            note=chosen["note"],
            pronoun=chosen["pronoun"],
            register=chosen["register"],
        ))
    return merged


def _merged_pending(members: list[dict], merged: Glossary, mapping: dict | None) -> list[dict]:
    """One shared approvals queue.

    Two invariants, both load-bearing:

    * every item keeps BOTH ``korean`` and ``english`` keys, because
      ``_queue_new_terms_locked`` subscripts them directly and a missing key raises
      KeyError on a worker thread in the middle of a translation;
    * ``chapter`` is rewritten from the member's LOCAL index to the series' global number,
      or every suggestion in the queue points at the wrong chapter.

    Anything the merge just locked to the same spelling is dropped: one of the user's
    series carries 471 pending items across three documents, and handing that back
    undeduped would be a wall nobody works through.
    """
    members_map = (mapping or {}).get("members") or {}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for member in members:
        rows = (members_map.get(member["project_id"]) or {}).get("rows") or []
        to_global = {r.get("index"): r.get("global") for r in rows}
        for item in member["pending"]:
            korean = str(item.get("korean") or "").strip()
            english = str(item.get("english") or "").strip()
            if not korean and not english:
                continue
            key = (korean, english.casefold())
            if key in seen:
                continue
            locked = merged.get(korean) if korean else None
            if locked is not None and locked.english == english:
                continue  # the merge already settled this one
            seen.add(key)
            row = dict(item)
            row["korean"] = korean
            row["english"] = english
            # A shared queue has to say which document a suggestion came from.
            row["project_id"] = member["project_id"]
            row["project_name"] = member["name"]
            local = item.get("chapter")
            row["local_chapter"] = local
            row["chapter"] = to_global.get(local, local)
            out.append(row)
    return out


def merge_glossaries(sid: str, picks: dict[str, str] | None = None,
                     *, dry_run: bool = True) -> dict:
    """Merge a series' glossaries. With ``dry_run`` nothing is written.

    ``picks`` maps a conflict's key — its Korean term, or for an English-only name its
    case-folded English — to the chosen spelling. Unanswered conflicts take the suggested
    winner rather than being left out: leaving nineteen character names unlocked until
    someone clicks would let the next translation drift worse than before the merge.
    """
    series = series_mod.get_series(sid)
    if series is None:
        raise ValueError("That series doesn't exist.")
    members = _members(series)
    if len(members) < 2:
        raise ValueError("A series needs at least two readable novels to merge.")

    conflicts = find_conflicts(members)
    merged = _merged_glossary(members, conflicts, picks or {})
    pending = _merged_pending(members, merged, series_mod.load_mapping(sid))

    for conflict in conflicts:
        key = conflict["korean"] or conflict["candidates"][0]["english"].casefold()
        conflict["key"] = key
        conflict["chosen"] = (picks or {}).get(key)

    result = {
        "series_id": sid,
        "dry_run": dry_run,
        "members": [
            {"project_id": m["project_id"], "name": m["name"],
             "entries": len(m["glossary"].entries()), "pending": len(m["pending"])}
            for m in members
        ],
        "locked": len(merged.entries()),
        "pending": len(pending),
        "conflicts": conflicts,
        # "0 conflicts" is not "0 duplicates": entries filed under DIFFERENT Korean keys
        # for the same concept are invisible to this merge and are carried across as-is.
        # One real glossary holds the same character under three Korean keys, romanized
        # two ways. Carrying them is the conservative choice, but callers must not sell
        # this as a glossary cleanup.
        "near_duplicates_kept": True,
    }
    if dry_run:
        return result

    sdir = series_mod.series_root() / sid
    sdir.mkdir(parents=True, exist_ok=True)
    # Locking on the series directory is what makes every member share one lock from here
    # on: glossary_lock keys on the file's parent, so the cross-document race that
    # test_glossary_race guards is covered with no new locking code.
    with glossary_lock(sdir / "glossary.json"):
        merged.save(sdir / "glossary.json", sdir / "glossary.md")
        save_pending(sdir / "glossary_pending.json", pending)

    series["glossary_merged_at"] = series_mod.now()
    series_mod.write_series(series)
    return result


def series_glossary_dir(sid: str) -> Path:
    return series_mod.series_root() / sid

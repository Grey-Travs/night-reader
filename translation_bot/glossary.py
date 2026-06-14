"""Master glossary, relevant-entry injection, and the human review queue.

The glossary is the single source of truth for names/terms. Only entries whose
Korean term appears in a chapter's source are injected into that chapter's prompt
(keeps the prompt lean as the glossary grows). Newly encountered terms never get
auto-committed — they land in a pending queue for an approve/edit/reject gate, so
a misclassified name or wrong romanization can't silently propagate everywhere.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

VALID_TYPES = {"name", "place", "skill", "term", "other"}


@dataclass
class GlossaryEntry:
    korean: str
    english: str
    type: str = "other"
    note: str = ""
    # Character profile hints (most useful on `name` entries). Korean omits pronouns
    # and encodes social register in verb endings; pinning these keeps the English
    # pronoun and tone consistent across chapters instead of being re-guessed.
    pronoun: str = ""   # e.g. "he", "she", "they"
    register: str = ""  # e.g. "formal", "casual/banmal", "polite/jondaemal"

    @classmethod
    def from_dict(cls, d: dict) -> "GlossaryEntry":
        return cls(
            korean=str(d.get("korean", "")).strip(),
            english=str(d.get("english", "")).strip(),
            type=(str(d.get("type", "other")).strip().lower() or "other"),
            note=str(d.get("note", "")).strip(),
            pronoun=str(d.get("pronoun", "")).strip(),
            register=str(d.get("register", "")).strip(),
        )


class Glossary:
    """In-memory view of ``glossary.json``.

    Holds two kinds of entry, both kept in one ordered list:
    - *mapped* — a Korean term locked to an English spelling (``korean`` set).
    - *canonical English name* — an English spelling established from already-English
      chapters whose Korean isn't known yet (``korean`` empty). These are injected as
      a "match these spellings" reference, so a freshly translated chapter stays
      consistent with the existing English ones. When the Korean is later learned, the
      mapped entry supersedes the English-only placeholder automatically.
    """

    def __init__(self, entries: list[GlossaryEntry] | None = None):
        self._entries: list[GlossaryEntry] = []
        self._by_korean: dict[str, GlossaryEntry] = {}
        for e in entries or []:
            self.add(e)

    # ---- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        path = Path(path)
        if not path.exists():
            return cls([])
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else []
        except (json.JSONDecodeError, OSError, ValueError):
            data = []  # a corrupt glossary must not break translation/library loading
        if not isinstance(data, list):
            data = []
        return cls([GlossaryEntry.from_dict(d) for d in data if isinstance(d, dict)])

    def save(self, json_path: str | Path, md_path: str | Path | None = None) -> None:
        entries = self.entries()
        Path(json_path).write_text(
            json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if md_path is not None:
            Path(md_path).write_text(self.to_markdown(), encoding="utf-8")

    # ---- access ------------------------------------------------------------
    def entries(self) -> list[GlossaryEntry]:
        return sorted(self._entries, key=lambda e: (e.type, e.english or e.korean))

    def __contains__(self, korean: str) -> bool:
        return korean in self._by_korean

    def get(self, korean: str) -> GlossaryEntry | None:
        return self._by_korean.get(korean)

    def relevant_to(self, source_text: str) -> list[GlossaryEntry]:
        """Mapped entries whose Korean term occurs in the chapter source."""
        hits = [e for e in self._entries if e.korean and e.korean in source_text]
        return sorted(hits, key=lambda e: (e.type, e.korean))

    def canonical(self) -> list[GlossaryEntry]:
        """English-only canonical names, for the injected "match these spellings" block.

        Mapped entries are deliberately excluded: they're already injected (with their
        Korean) by ``relevant_to`` whenever they appear, so this keeps the prompt lean
        and avoids duplicating them. The value here is names whose Korean isn't known
        yet (e.g. learned from already-English chapters)."""
        return sorted((e for e in self._entries if not e.korean and e.english),
                      key=lambda e: (e.type, e.english))

    # ---- mutation ----------------------------------------------------------
    def _drop_english_only(self, english: str) -> None:
        low = english.lower()
        self._entries = [e for e in self._entries if not (not e.korean and e.english.lower() == low)]

    def add(self, entry: GlossaryEntry) -> None:
        if not entry.english and not entry.korean:
            return
        if entry.korean:
            old = self._by_korean.get(entry.korean)
            if old is not None and old in self._entries:
                self._entries.remove(old)
            if entry.english:
                self._drop_english_only(entry.english)  # mapped entry supersedes the placeholder
            self._by_korean[entry.korean] = entry
            self._entries.append(entry)
        else:
            # A mapped entry with the same English already covers this spelling — don't
            # add a redundant English-only canonical name.
            low = entry.english.lower()
            if any(e.korean and e.english.lower() == low for e in self._entries):
                return
            self._drop_english_only(entry.english)  # dedup canonical names by English
            self._entries.append(entry)

    def remove(self, korean: str) -> bool:
        """Drop a mapped term by its Korean key. Returns True if something was removed."""
        e = self._by_korean.pop(korean, None)
        if e is None:
            return False
        if e in self._entries:
            self._entries.remove(e)
        return True

    def remove_english(self, english: str) -> bool:
        """Drop an English-only canonical name. Returns True if something was removed."""
        low = english.lower()
        for e in list(self._entries):
            if not e.korean and e.english.lower() == low:
                self._entries.remove(e)
                return True
        return False

    # ---- rendering ---------------------------------------------------------
    def to_markdown(self) -> str:
        lines = ["# Glossary", "", "| Korean | English | Type | Note |", "| --- | --- | --- | --- |"]
        for e in self.entries():
            note = e.note.replace("|", "\\|")
            lines.append(f"| {e.korean} | {e.english} | {e.type} | {note} |")
        return "\n".join(lines) + "\n"


def _profile_hint(e: GlossaryEntry) -> str:
    """The bracketed pronoun/register hint for a character entry, if any."""
    bits = []
    if e.pronoun:
        bits.append(f"pronoun: {e.pronoun}")
    if e.register:
        bits.append(f"register: {e.register}")
    return f" [{'; '.join(bits)}]" if bits else ""


def format_injection(entries: list[GlossaryEntry]) -> str:
    """Render the glossary block for the system prompt."""
    if not entries:
        return "(No established glossary entries appear in this chapter yet.)"
    return "\n".join(
        f"- {e.korean} -> {e.english} ({e.type})" + _profile_hint(e) + (f" — {e.note}" if e.note else "")
        for e in entries
    )


def format_names(entries: list[GlossaryEntry]) -> str:
    """Render the canonical-English-names block (spellings to match for consistency)."""
    lines = []
    for e in entries:
        if not e.english:
            continue
        lines.append(f"- {e.english} ({e.type})" + _profile_hint(e) + (f" — {e.note}" if e.note else ""))
    return "\n".join(lines)


# ---- pending queue ---------------------------------------------------------
def load_pending(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def save_pending(path: str | Path, items: list[dict]) -> None:
    Path(path).write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def queue_new_terms(
    pending_path: str | Path,
    glossary: Glossary,
    new_terms: list[dict],
    chapter_index: int,
) -> int:
    """Diff model-proposed terms against the master glossary and the existing
    queue, appending genuinely new/conflicting ones. Returns how many were added.

    Conflicts (same Korean term, different English) are kept and flagged rather
    than silently overwriting an established spelling.
    """
    pending = load_pending(pending_path)
    pending_keys = {(p["korean"], p["english"]) for p in pending}
    added = 0

    for raw in new_terms:
        entry = GlossaryEntry.from_dict(raw)
        if not entry.korean or not entry.english:
            continue

        existing = glossary.get(entry.korean)
        if existing and existing.english == entry.english:
            continue  # already known, identical — nothing to review

        key = (entry.korean, entry.english)
        if key in pending_keys:
            continue  # already queued

        item = asdict(entry)
        item["chapter"] = chapter_index
        if entry.type not in VALID_TYPES:
            item["type"] = "other"
        if existing and existing.english != entry.english:
            item["conflict_with"] = existing.english
        pending.append(item)
        pending_keys.add(key)
        added += 1

    save_pending(pending_path, pending)
    return added

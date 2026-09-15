"""Series: several Google Docs that are really one novel.

Google Docs caps a document at ~100 tabs and this app reads one tab per chapter
(``docs_extract.extract_chapters``), so a 400-chapter novel arrives as four separate
documents -- "Collateral Kitty", "Collateral Kitty2", ... -- which the library sees as
four unrelated novels. Two things break as a result:

  * reading stops dead at every 100-chapter boundary, and
  * each document keeps its OWN glossary, so the locked spelling of a character name
    resets at chapter 101 and can silently drift -- exactly what the glossary exists
    to prevent.

A series fixes both by owning an ORDERED list of existing projects plus one shared
glossary. Projects are never moved or merged: ``projects/<id>/`` stays where it is and
keeps its own state.json, chapters/ and audit/, so linking is reversible and nothing
already translated is touched.

Series ids are server-generated hex, like project ids, so a pasted name can never
become a filesystem path.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from translation_bot.atomic import atomic_write_text, quarantine_unreadable

import server.projects as pj


def series_root() -> Path:
    """Where series manifests live: ``series/``, beside ``projects/``.

    A FUNCTION rather than a module constant on purpose. The test suite isolates itself
    by monkeypatching ``pj.PROJECTS_DIR`` at a tmp_path (a dozen tests do this), and a
    constant computed at import time would ignore the patch and keep reading and writing
    the user's real library.
    """
    return pj.PROJECTS_DIR.parent / "series"


_SERIES_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# Fields a user may edit on a series. Everything else (id, created_at, and the resolved
# chapter mapping) is server-managed.
EDITABLE_FIELDS = {"name", "archived"}

# ---------------------------------------------------------------------------
# Title matching
#
# The convention is described as "[NOVEL]" then "[NOVEL]2". The 52 documents actually on
# disk use six, and "strip the trailing digit" gets several of them wrong:
#
#   glued digit      Collateral Kitty2
#   space + digit    I Unleashed a Mad Dog on the Raid Party That Abandoned Me 2
#   TWO spaces       Do Not Pick Up the Crown Prince Who Became a Frog!  2
#   "pt." form       The Abandoned Guide Enjoys an Easy Life pt.2
#   digit after "?"  Are You Out Of Your Mind?2
#   chapter range    Unintentional Transmigration Operations 101-124
#
# on top of three traps that defeat exact comparison: case drift ("Deflower Me If You
# Can" vs "Deflower me if you can 2"), the Korean title present on one member and absent
# on its sequel, and one document whose Korean parenthetical is missing its closing
# bracket.
# ---------------------------------------------------------------------------

_HANGUL = "가-힣"

# A trailing parenthetical containing Hangul, i.e. the Korean title. It has to come off
# before titles can be compared because it appears on some members and not others
# ("Paste; Feeding My Guide (...)" vs "Paste; Feeding My Guide2"). The closing bracket is
# OPTIONAL: one real document is missing it entirely -- "I am the Convenience Store Owner
# Next to the Esper Management Bureau (...". Hangul is required so that an English
# parenthetical such as "(Side Story)" is left alone.
_KO_PARENS_RE = re.compile(rf"\s*\([^()]*[{_HANGUL}][^()]*\)?\s*$")

# A trailing volume marker, with or without "pt." and with any amount of space.
_VOL_RE = re.compile(r"\s*(?:pt\.?\s*)?(\d{1,4})\s*$", re.IGNORECASE)

# "Unintentional Transmigration Operations 101-124" states its global chapter range in
# the name. Good as a START hint only: that document now holds 34 tabs, not the 24 the
# name claims, so the end is already stale.
_RANGE_RE = re.compile(r"\s*(\d{2,4})\s*[-–—]\s*(\d{2,4})\s*$")

# A trailing number at or below this is read as a volume ("...2"); anything larger is
# read as a chapter number ("... 101"), which tells us where the document starts.
_MAX_VOLUME = 20

# Trailing characters that carry no meaning for comparison. "!" and "?" are in here
# because on real documents they sit between the base title and the volume digit
# ("Are You Out Of Your Mind?2"), and they come off both members alike.
_TRIM = " \t　-–—:;,.!?"

_PASSES = 6  # bounded; each pass removes at most one marker


def split_title(name: str) -> tuple[str, int | None, int | None]:
    """Split a project name into ``(base, volume, start_chapter_hint)``.

    Markers are stripped in a loop rather than once, because they nest in both orders:
    the digit can sit outside the Korean parenthetical ("... (...) 2") or be glued to
    its closing bracket ("...)2").
    """
    s = unicodedata.normalize("NFKC", name or "").strip()
    volume: int | None = None
    start: int | None = None
    for _ in range(_PASSES):
        m = _RANGE_RE.search(s)
        if m:
            if start is None:
                start = int(m.group(1))
            s = s[: m.start()].rstrip()
            continue
        m = _VOL_RE.search(s)
        if m:
            n = int(m.group(1))
            if n <= _MAX_VOLUME:
                if volume is None:
                    volume = n
            elif start is None:
                start = n
            s = s[: m.start()].rstrip()
            continue
        m = _KO_PARENS_RE.search(s)
        if m:
            s = s[: m.start()].rstrip()
            continue
        break
    return s, volume, start


def normalize_title(name: str) -> str:
    """The comparison key that groups sibling documents."""
    base, _, _ = split_title(name)
    base = re.sub(r"\s+", " ", base)  # "Frog!  2" leaves a double space behind
    return base.strip(_TRIM).casefold()


def _sort_key(member: dict) -> tuple:
    # Volume when the name states one, else the start hint, else creation order. Every
    # fallback matters: part 1 usually carries no marker at all.
    return (
        member.get("volume") or 0,
        member.get("start_hint") or 0,
        member.get("created_at") or "",
    )


def seed_start_chapters(members: list[dict]) -> list[dict]:
    """Fill in each member's ``start_chapter``, in order.

    Priority: a range stated in the name, else one past the running total of the members
    before it. Both are only seeds -- the user confirms them in the mapping review table,
    and the confirmed numbers are what gets persisted.
    """
    running = 0
    for m in members:
        hint = m.get("start_hint")
        m["start_chapter"] = hint if hint else running + 1
        running = m["start_chapter"] - 1 + int(m.get("chapter_count") or 0)
    return members


def suggest_series(projects: list[dict] | None = None) -> list[dict]:
    """Group existing projects into proposed series.

    A suggestion only -- never applied on its own. The naming is inconsistent enough
    that a human confirms the grouping and the chapter offsets before anything is
    written.
    """
    rows = pj.list_projects() if projects is None else projects
    groups: dict[str, list[dict]] = {}
    for p in rows:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        # A one-chapter project is a scratch/test document, not a volume of anything.
        if int(p.get("chapter_count") or 0) <= 1:
            continue
        base, volume, start = split_title(name)
        key = normalize_title(name)
        if not key:
            continue
        groups.setdefault(key, []).append(
            {
                "project_id": p.get("id"),
                "name": name,
                "chapter_count": int(p.get("chapter_count") or 0),
                "volume": volume,
                "start_hint": start,
                "created_at": p.get("created_at") or "",
                "_base": base,
            }
        )

    out = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=_sort_key)
        seed_start_chapters(members)
        out.append(
            {
                "key": key,
                # Display name comes from the first member in reading order, which is the
                # one that carries the full title including the Korean parenthetical.
                "name": members[0]["_base"] or members[0]["name"],
                "members": [
                    {k: v for k, v in m.items() if k != "_base"} for m in members
                ],
                "total_chapters": sum(m["chapter_count"] for m in members),
            }
        )
    out.sort(key=lambda g: g["name"].casefold())
    return out


# ---------------------------------------------------------------------------
# Manifest storage
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Same contract as get_project: a truncated manifest reads as missing rather than
        # turning every request into a 500. Keep the bytes aside first -- the caller may
        # well write a fresh manifest straight over them.
        quarantine_unreadable(path)
        return None


def list_series() -> list[dict]:
    root = series_root()
    if not root.exists():
        return []
    out = []
    for sj in root.glob("*/series.json"):
        data = _read_json(sj)
        if data:
            out.append(data)
    out.sort(key=lambda s: (s.get("name") or "").casefold())
    return out


def get_series(sid: str) -> dict | None:
    if not _SERIES_ID_RE.match(sid or ""):
        return None
    sj = series_root() / sid / "series.json"
    if not sj.exists():
        return None
    return _read_json(sj)


def member_ids(series: dict) -> list[str]:
    return [
        m.get("project_id") for m in series.get("members") or [] if m.get("project_id")
    ]


def create_series(name: str, project_ids: list[str], *, sid: str | None = None) -> dict:
    sid = sid or uuid.uuid4().hex[:12]
    if not _SERIES_ID_RE.match(sid):
        raise ValueError("invalid series id")
    if not project_ids:
        raise ValueError("a series needs at least one project")
    if len(set(project_ids)) != len(project_ids):
        raise ValueError("the same project cannot appear twice in a series")
    known = {p.get("id") for p in pj.list_projects()}
    missing = [p for p in project_ids if p not in known]
    if missing:
        raise ValueError(f"unknown project(s): {', '.join(missing)}")
    # A project in two series would give one set of chapters two different global
    # numbers, and the glossary redirection could not resolve which series owns it.
    taken = {m for s in list_series() for m in member_ids(s)}
    clash = [p for p in project_ids if p in taken]
    if clash:
        raise ValueError(f"already in a series: {', '.join(clash)}")

    series = {
        "id": sid,
        "name": (name or "").strip() or "Untitled series",
        "created_at": _now(),
        # start_chapter is seeded from suggest_series and confirmed by the user; sealed
        # stays False until the refresh path sets it.
        "members": [
            {"project_id": pid, "start_chapter": None, "sealed": False}
            for pid in project_ids
        ],
        "publish_targets": [],
    }
    _atomic_write_json(series_root() / sid / "series.json", series)
    _invalidate_index()
    return series


def write_series(series: dict) -> dict:
    _atomic_write_json(series_root() / series["id"] / "series.json", series)
    _invalidate_index()
    return series


def delete_series(sid: str) -> bool:
    """Unlink a series. The member novels are never touched.

    Because each member's own glossary.json was left in place and never rewritten by the
    merge, removing the series is the whole rollback: project_config stops redirecting and
    every novel is back on the glossary it had before. The tradeoff is that terms approved
    while the novels were linked stay in the series glossary.
    """
    import shutil

    if not _SERIES_ID_RE.match(sid or ""):
        return False
    sdir = series_root() / sid
    if not sdir.exists():
        return False
    shutil.rmtree(sdir, ignore_errors=True)
    _invalidate_index()
    return True


def now() -> str:
    """A timestamp in the format every manifest here uses."""
    return _now()


# pid -> sid, built once and dropped on any write. project_config() calls into this for
# EVERY novel on every library load, and walking series/*/series.json each time costs ~50
# extra stat calls per render on Windows -- measurable, and _safe_summary would swallow
# any error rather than show it. The app is single-process, so an in-memory index is safe.
# Cached against the library root it was built from, so that pointing the app at a
# different library -- which the test suite does constantly via monkeypatched
# PROJECTS_DIR -- rebuilds it instead of answering from the previous one.
_pid_index: tuple[Path, dict[str, str]] | None = None


def _invalidate_index() -> None:
    global _pid_index
    _pid_index = None


def _project_index() -> dict[str, str]:
    global _pid_index
    root = series_root()
    if _pid_index is None or _pid_index[0] != root:
        _pid_index = (root, {m: s["id"] for s in list_series() for m in member_ids(s)})
    return _pid_index[1]


def series_for_project(pid: str) -> dict | None:
    """The series a project belongs to, if any.

    Every project not in a series must keep working exactly as before, so callers treat
    ``None`` as "behave the old way" rather than as an error.
    """
    sid = _project_index().get(pid)
    return get_series(sid) if sid else None


def glossary_dir_for_project(pid: str) -> Path | None:
    """The series directory whose glossary this project should read and write, or None.

    Deliberately returns None until the merge has actually run. Pointing a member at an
    empty series glossary the moment it was linked would look exactly like every locked
    name and term had been wiped, and the next translation would drift immediately.
    """
    sid = _project_index().get(pid)
    if not sid:
        return None
    sdir = series_root() / sid
    return sdir if (sdir / "glossary.json").exists() else None


# ---------------------------------------------------------------------------
# Resolved chapter numbering
#
# Stored per series in mapping.json. The rows carry more than the bare global number --
# kind, confidence and duplicate_of -- because the review table has to show WHY a row is
# what it is, and because a number that was merely inferred must be visibly different
# from one the document stated outright.
# ---------------------------------------------------------------------------

MAPPING_VERSION = 1


def mapping_path(sid: str) -> Path:
    return series_root() / sid / "mapping.json"


def load_mapping(sid: str) -> dict | None:
    path = mapping_path(sid)
    if not path.exists():
        return None
    return _read_json(path)


def save_mapping(sid: str, doc: dict) -> dict:
    doc["version"] = MAPPING_VERSION
    doc["resolved_at"] = _now()
    _atomic_write_json(mapping_path(sid), doc)
    return doc


def resolve_mapping(series: dict, chapters_by_pid: dict[str, list]) -> dict:
    """Resolve every member's chapters to global numbers.

    ``chapters_by_pid`` maps a member's project id to its ``Chapter`` list. It is passed
    in rather than fetched here so this module stays free of network and cache concerns --
    the caller already has ``get_chapters`` and knows about the offline/degraded path.

    A member missing from ``chapters_by_pid`` (offline, or never fetched) keeps whatever
    rows the stored mapping already had, so one unreachable document cannot blank out the
    numbering for the whole series.
    """
    from translation_bot.chapter_numbers import detect_gaps_duplicates, infer_mapping

    previous = (load_mapping(series["id"]) or {}).get("members") or {}
    members: dict[str, dict] = {}
    running = 0

    for member in series.get("members") or []:
        pid = member.get("project_id")
        if not pid:
            continue
        chapters = chapters_by_pid.get(pid)
        # An explicitly confirmed start wins; otherwise carry on from the member before.
        start = member.get("start_chapter") or (running + 1)

        # An EMPTY list counts as unreadable too, not as a document that genuinely lost
        # every chapter. A read failure is far likelier than a real edit of that size, and
        # blanking a member's numbering is destructive -- the posting feature and the
        # reader both depend on it.
        if not chapters:
            kept = previous.get(pid) or {"rows": []}
            kept["offline"] = True
            members[pid] = kept
            numbers = [
                r.get("global") for r in kept.get("rows") or []
                if isinstance(r.get("global"), int)
            ]
            running = max(numbers) if numbers else running
            continue

        rows = infer_mapping(chapters, start_hint=start)
        members[pid] = {
            "offline": False,
            "start_chapter": start,
            "rows": [r.as_dict() for r in rows],
        }
        info = detect_gaps_duplicates(rows)
        running = info["last"] or running

    doc = {"members": members}
    doc.update(_series_totals(doc))
    return doc


def _series_totals(doc: dict) -> dict:
    """Gaps and duplicates across the whole series, not just within one document.

    A boundary is exactly where numbering mistakes hide, so this has to span members --
    a per-document check would never notice that part 2 starts at 102.
    """
    numbers: list[int] = []
    for member in (doc.get("members") or {}).values():
        for row in member.get("rows") or []:
            n = row.get("global")
            if isinstance(n, int):
                numbers.append(n)
    if not numbers:
        return {"first": None, "last": None, "gaps": [], "duplicates": [], "total": 0}
    seen: dict[int, int] = {}
    for n in numbers:
        seen[n] = seen.get(n, 0) + 1
    return {
        "first": min(numbers),
        "last": max(numbers),
        "gaps": sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers)),
        "duplicates": sorted(n for n, c in seen.items() if c > 1),
        "total": len(numbers),
    }


def apply_overrides(doc: dict, overrides: list[dict]) -> dict:
    """Apply the user's confirmed edits from the review table.

    Each override is ``{project_id, index, global?, kind?, label?}``. Only these fields
    can be changed: ``index`` is never touched, because it is the key that ties a row to
    ``state.json``, ``chapter-NNN.md``, ``previous/``, ``audit/`` and ``variants/``.
    """
    members = doc.get("members") or {}
    for item in overrides or []:
        pid = item.get("project_id")
        member = members.get(pid)
        if not member:
            continue
        for row in member.get("rows") or []:
            if row.get("index") != item.get("index"):
                continue
            if "global" in item:
                row["global"] = item["global"]
            if "kind" in item:
                row["kind"] = item["kind"]
            if "label" in item:
                row["label"] = item["label"] or ""
            # A human decision is final: it must not be marked as guessed, and a later
            # re-resolve has to leave it alone.
            row["source"] = "manual"
            row["confidence"] = "high"
            break
    doc.update(_series_totals(doc))
    return doc


def global_for(sid: str, pid: str, index: int) -> int | None:
    doc = load_mapping(sid) or {}
    member = (doc.get("members") or {}).get(pid) or {}
    for row in member.get("rows") or []:
        if row.get("index") == index:
            return row.get("global")
    return None


# Kinds you would never turn a page onto. A DUPLICATE is literally the same text again,
# and an EXTRA is an author note or cover tab. A side story is left in: it is real content
# the reader wants, it just does not take a chapter number.
_UNREADABLE_KINDS = ("duplicate", "extra")


def reading_order(series: dict, mapping: dict | None) -> list[dict]:
    """The series' chapters in the order you would actually read them."""
    return [
        row for row in series_chapters(series, mapping)
        if row.get("kind") not in _UNREADABLE_KINDS
    ]


def neighbours(series: dict, mapping: dict | None, pid: str,
               index: int) -> tuple[dict | None, dict | None]:
    """The chapters either side of this one, crossing document boundaries.

    This is what makes chapter 100 of one Google Doc flow into chapter 1 of the next
    without the reader having to know they are different novels.
    """
    order = reading_order(series, mapping)
    pos = next(
        (i for i, r in enumerate(order)
         if r.get("project_id") == pid and r.get("index") == index),
        None,
    )
    if pos is None:
        return None, None
    return (
        order[pos - 1] if pos > 0 else None,
        order[pos + 1] if pos < len(order) - 1 else None,
    )


def globals_by_index(mapping: dict | None, pid: str) -> dict[int, dict]:
    """``{local index: row}`` for one member, for decorating a chapter list."""
    rows = ((mapping or {}).get("members") or {}).get(pid, {}).get("rows") or []
    return {r["index"]: r for r in rows if r.get("index") is not None}


def series_chapters(series: dict, mapping: dict | None) -> list[dict]:
    """The series' chapters as one flat list, in reading order.

    Reading order is ``(member position, index)`` rather than sorted by global number, so
    side stories and extras sit where they actually live in the document instead of
    needing fractional numbers. ``global`` is a label, not the sort key.
    """
    out: list[dict] = []
    members = (mapping or {}).get("members") or {}
    for position, member in enumerate(series.get("members") or []):
        pid = member.get("project_id")
        rows = (members.get(pid) or {}).get("rows") or []
        for row in sorted(rows, key=lambda r: r.get("index") or 0):
            out.append({
                "project_id": pid,
                "member_position": position,
                "index": row.get("index"),
                "global": row.get("global"),
                "kind": row.get("kind"),
                "label": row.get("label") or "",
                "confidence": row.get("confidence"),
                "duplicate_of": row.get("duplicate_of"),
            })
    return out

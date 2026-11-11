"""Library-wide numbers for the Stats page: tokens, chapters, novels, posts, app updates.

Read-only from end to end. Every number here is already on disk somewhere, recorded by
the code that did the work; this only adds them up. Nothing is written, and the per-novel
files are read without quarantining: a file that cannot be read is skipped and counted in
``unreadable``, because a statistics page has no business moving the user's data aside.
(The novel and series lists come from the shared listers, which keep their own rules.)

Where the tokens come from, and what is NOT counted:
  - translation, pronoun fixes and AI resolves: ``state.json``, per chapter (State.add_usage)
  - reading page photos and double-checking them: ``pages.json`` totals
  - paragraph rewrites: ``variants/*.json``, per variant
  The quick look the phone asks for and the seam joins between pages record their cost
  but not their tokens, so they are absent from the token total (and present in the
  plan-equivalent cost through ``pages.json``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from translation_bot.state import DONE_STATUSES

from . import posting
from . import projects as pj
from . import series as series_mod

TOKEN_KEYS = ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")

# Statuses a photographed page reaches only after Claude has read it.
_READ_PAGE_STATUSES = ("ok", "edited", "needs-check")


def _read_json(path: Path):
    """The parsed file, or None. Never quarantines: see the module docstring."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _zero() -> dict:
    return {k: 0 for k in TOKEN_KEYS}


def _add(acc: dict, usage) -> None:
    if not isinstance(usage, dict):
        return
    for key in TOKEN_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            acc[key] += int(value)


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=pj.PROJECT_ROOT, capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def app_updates() -> dict:
    """How many changes Night Reader itself has had: its commits, and since when.

    None for both when git is missing or this is not a checkout, so the page can say
    "unknown" instead of claiming zero.
    """
    count = _git("rev-list", "--count", "HEAD")
    first = _git("log", "--max-parents=0", "--format=%aI", "HEAD")
    return {
        "count": int(count) if count and count.isdigit() else None,
        "since": first.splitlines()[0] if first else None,
    }


def compute() -> dict:
    sources = {"translation": _zero(), "photos": _zero(), "rewrites": _zero()}
    cost = 0.0
    chapters = {"translated": 0, "needs_review": 0, "english": 0}
    pages_read = 0
    rewrites = 0
    unreadable = 0
    earliest = None

    projects = pj.list_projects()
    for project in projects:
        pid = str(project.get("id") or "")
        if not pid:
            continue
        created = project.get("created_at")
        if created and (earliest is None or created < earliest):
            earliest = created
        root = pj.PROJECTS_DIR / pid

        state_path = root / "state.json"
        if state_path.exists():
            doc = _read_json(state_path)
            if not isinstance(doc, dict):
                unreadable += 1
            else:
                for rec in (doc.get("chapters") or {}).values():
                    if not isinstance(rec, dict):
                        continue
                    _add(sources["translation"], rec.get("usage"))
                    cost += float(rec.get("cost_usd") or 0.0)
                    status = rec.get("status")
                    if status in DONE_STATUSES:
                        chapters["translated"] += 1
                    elif status == "needs-review":
                        chapters["needs_review"] += 1
                    elif status == "english-source":
                        chapters["english"] += 1

        pages_path = root / "pages.json"
        if pages_path.exists():
            doc = _read_json(pages_path)
            if not isinstance(doc, dict):
                unreadable += 1
            else:
                totals = doc.get("totals") or {}
                _add(sources["photos"], totals)
                cost += float(totals.get("cost_usd") or 0.0)
                pages_read += sum(1 for p in doc.get("pages") or []
                                  if isinstance(p, dict) and p.get("status") in _READ_PAGE_STATUSES)

        for vpath in sorted((root / "variants").glob("chapter-*.json")):
            doc = _read_json(vpath)
            if not isinstance(doc, dict):
                unreadable += 1
                continue
            for group in doc.get("groups") or []:
                for variant in (group or {}).get("variants") or []:
                    if not isinstance(variant, dict) or variant.get("kind") == "original":
                        continue
                    rewrites += 1
                    _add(sources["rewrites"], variant.get("usage"))
                    cost += float(variant.get("cost_usd") or 0.0)

    tokens = _zero()
    for part in sources.values():
        for key in TOKEN_KEYS:
            tokens[key] += part[key]

    # A novel is a series, or a project that belongs to none. Several projects in one
    # series are one novel split across documents, so counting projects would overstate it.
    series_list = series_mod.list_series()
    members = {str(m.get("project_id")) for s in series_list for m in s.get("members") or []}
    standalone = [p for p in projects if str(p.get("id")) not in members]
    novels_total = len(series_list) + len(standalone)
    novels_archived = (sum(1 for s in series_list if s.get("archived"))
                       + sum(1 for p in standalone if p.get("archived")))

    posted = 0
    novels_posting = 0
    last_post = None
    for series in series_list:
        found = 0
        for target in series.get("publish_targets") or []:
            ledger = _read_json(posting.ledger_path(series["id"], str(target.get("id") or "")))
            if not isinstance(ledger, dict):
                continue
            for entry in ledger.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") != "posted":
                    continue
                found += 1
                at = entry.get("at")
                if at and (last_post is None or at > last_post):
                    last_post = at
        posted += found
        novels_posting += 1 if found else 0

    glossary_names = 0
    for series in series_list:
        doc = _read_json(series_mod.series_root() / series["id"] / "glossary.json")
        glossary_names += len(doc) if isinstance(doc, list) else 0
    for project in standalone:
        doc = _read_json(pj.PROJECTS_DIR / str(project.get("id")) / "glossary.json")
        glossary_names += len(doc) if isinstance(doc, list) else 0

    return {
        "tokens": {**tokens, "total": sum(tokens.values()), "by_source": sources},
        "cost_usd": round(cost, 2),
        "chapters": chapters,
        "novels": {"total": novels_total, "archived": novels_archived,
                   "active": novels_total - novels_archived},
        "posted": {"chapters": posted, "novels": novels_posting, "last_at": last_post},
        "app_updates": app_updates(),
        "pages_read": pages_read,
        "rewrites": rewrites,
        "glossary_names": glossary_names,
        "since": earliest,
        "unreadable": unreadable,
    }

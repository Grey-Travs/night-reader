"""Multi-novel project management.

Each novel is a self-contained project under ``projects/<id>/`` with its own
glossary, state, and outputs. Global ``config.toml`` holds shared defaults (model,
effort, validation) and the shared Google/Claude logins. A project's *effective*
config is the global config with the per-project paths and doc id overlaid, so the
whole existing engine works per-project unchanged.

Project ids are server-generated hex (never user-controlled), so a pasted novel
name can never become a filesystem path (no traversal).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from translation_bot.config import Config
from translation_bot.docs_extract import Chapter
from translation_bot.text_source import chapters_to_records, records_to_chapters

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = PROJECT_ROOT / "projects"

# Project fields a user may edit at runtime (everything else is server-managed).
EDITABLE_FIELDS = {"name", "style_note", "instructions", "honorific_note"}

_DOC_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]{20,})")
_BARE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{20,}$")
_PROJECT_ID_RE = re.compile(r"^[a-f0-9]{12}$")


def extract_doc_id(url_or_id: str) -> str | None:
    """Pull a Google Doc id out of a pasted URL, or accept a bare id."""
    s = (url_or_id or "").strip()
    m = _DOC_ID_RE.search(s)
    if m:
        return m.group(1)
    if _BARE_ID_RE.match(s):
        return s
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_projects() -> list[dict]:
    if not PROJECTS_DIR.exists():
        return []
    out = []
    for pj in PROJECTS_DIR.glob("*/project.json"):
        try:
            out.append(json.loads(pj.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda p: p.get("created_at", ""))
    return out


def get_project(pid: str) -> dict | None:
    if not _PROJECT_ID_RE.match(pid or ""):
        return None
    pj = PROJECTS_DIR / pid / "project.json"
    if not pj.exists():
        return None
    return json.loads(pj.read_text(encoding="utf-8"))


def find_project_by_doc(doc_id: str) -> dict | None:
    for p in list_projects():
        if p.get("source_doc_id") == doc_id:
            return p
    return None


def create_project(name: str, doc_id: str, *, pid: str | None = None) -> dict:
    pid = pid or uuid.uuid4().hex[:12]
    if not _PROJECT_ID_RE.match(pid):
        raise ValueError("invalid project id")
    pdir = PROJECTS_DIR / pid
    (pdir / "chapters").mkdir(parents=True, exist_ok=True)
    (pdir / "audit").mkdir(parents=True, exist_ok=True)
    project = {
        "id": pid,
        "name": name.strip() or "Untitled novel",
        "source_doc_id": doc_id,
        "created_at": _now(),
    }
    (pdir / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return project


def _write_project(project: dict) -> dict:
    (PROJECTS_DIR / project["id"] / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return project


def rename_project(pid: str, name: str) -> dict:
    return update_project(pid, name=name)


def update_project(pid: str, **fields) -> dict:
    """Patch editable project fields (name, style_note, instructions, honorific_note)."""
    project = get_project(pid)
    if project is None:
        raise KeyError(pid)
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            continue
        text = (value or "").strip() if isinstance(value, str) else value
        if key == "name":
            project["name"] = text or project.get("name") or "Untitled novel"
        else:
            project[key] = text
    return _write_project(project)


# ---- text-source projects (paste / .txt upload, no Google account) ----------
def create_text_project(name: str, chapters: list[Chapter], *, pid: str | None = None) -> dict:
    pid = pid or uuid.uuid4().hex[:12]
    if not _PROJECT_ID_RE.match(pid):
        raise ValueError("invalid project id")
    pdir = PROJECTS_DIR / pid
    (pdir / "chapters").mkdir(parents=True, exist_ok=True)
    (pdir / "audit").mkdir(parents=True, exist_ok=True)
    (pdir / "source.json").write_text(
        json.dumps(chapters_to_records(chapters), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    project = {
        "id": pid,
        "name": name.strip() or "Untitled novel",
        "source_type": "text",
        "source_doc_id": "",
        "chapter_count": len(chapters),
        "created_at": _now(),
    }
    return _write_project(project)


def load_text_chapters(pid: str) -> list[Chapter]:
    path = PROJECTS_DIR / pid / "source.json"
    if not path.exists():
        return []
    return records_to_chapters(json.loads(path.read_text(encoding="utf-8")))


def delete_project(pid: str) -> bool:
    import shutil

    if not _PROJECT_ID_RE.match(pid or ""):
        return False
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        return False
    shutil.rmtree(pdir)
    return True


def project_config(global_cfg: Config, project: dict) -> Config:
    """Global config with per-project paths, doc id, and style overrides overlaid."""
    cfg = global_cfg.model_copy(deep=True)
    pdir = PROJECTS_DIR / project["id"]
    cfg.google.source_doc_id = project.get("source_doc_id", "")
    cfg.paths.output_dir = pdir / "chapters"
    cfg.paths.glossary_json = pdir / "glossary.json"
    cfg.paths.glossary_md = pdir / "glossary.md"
    cfg.paths.glossary_pending = pdir / "glossary_pending.json"
    cfg.paths.state_file = pdir / "state.json"
    cfg.paths.audit_dir = pdir / "audit"
    # Per-novel style overrides (genre/tone framing, free-form instructions, honorifics).
    if project.get("style_note"):
        cfg.translation.style_note = project["style_note"]
    if project.get("instructions"):
        cfg.translation.extra_instruction = project["instructions"]
    if project.get("honorific_note"):
        cfg.translation.honorific_note = project["honorific_note"]
    return cfg

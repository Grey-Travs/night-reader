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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = PROJECT_ROOT / "projects"

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


def rename_project(pid: str, name: str) -> dict:
    project = get_project(pid)
    if project is None:
        raise KeyError(pid)
    project["name"] = name.strip() or project["name"]
    (PROJECTS_DIR / pid / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return project


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
    """Global config with per-project paths and doc id overlaid."""
    cfg = global_cfg.model_copy(deep=True)
    pdir = PROJECTS_DIR / project["id"]
    cfg.google.source_doc_id = project["source_doc_id"]
    cfg.paths.output_dir = pdir / "chapters"
    cfg.paths.glossary_json = pdir / "glossary.json"
    cfg.paths.glossary_md = pdir / "glossary.md"
    cfg.paths.glossary_pending = pdir / "glossary_pending.json"
    cfg.paths.state_file = pdir / "state.json"
    cfg.paths.audit_dir = pdir / "audit"
    return cfg

"""FastAPI backend for the Translation Bot — multi-novel.

Each novel is a project (``projects/<id>/``) with its own glossary, state, and
outputs. The shared Google/Claude logins and default model/validation settings
live in the global ``config.toml``. Long-running translation streams progress over
Server-Sent Events.

Run from the project root:  uvicorn server.app:app --port 8000
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import uuid
import zipfile
from collections import deque
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from translation_bot import state as state_mod
from translation_bot.config import Config
from translation_bot.docs_extract import (
    Chapter, ChapterMetrics, extract_chapters, fetch_document, hangul_fraction,
)
from translation_bot.epub import build_epub
from translation_bot.glossary import VALID_TYPES, Glossary, GlossaryEntry, load_pending, save_pending
from translation_bot.google_auth import build_docs_service, get_credentials, load_saved_credentials
from translation_bot.pipeline import chapter_filename, process_chapter, write_chapter_file
from translation_bot.sanitize import find_leaks, korean_fraction, remove_snippets, strip_reasoning
from translation_bot.state import State
from translation_bot.text_source import split_text_into_chapters
from translation_bot.translator import RateLimitedError, Translator, TranslatorError
from translation_bot.validate import validate_translation

from . import projects as pj

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
DIST_DIR = PROJECT_ROOT / "web" / "dist"

app = FastAPI(title="Korean Web-Novel Translation Bot")
# This API is unauthenticated and acts on local data, so lock it to the loopback
# interface. TrustedHost rejects foreign Host headers (DNS-rebinding defense); CORS
# is limited to the local dev origins (in production, frontend + API are same-origin).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:8000", "http://127.0.0.1:8000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Input validation for the settings endpoint (prevents corrupting config.toml).
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_DEEP_MODES = {"off", "flagged", "always"}

_chapter_cache: dict[str, list[Chapter]] = {}  # keyed by project id
# Projects whose source Google Doc can't be fetched on this device (no access /
# offline). They fall back to a read-only "saved copy" rebuilt from local files.
_offline_projects: set[str] = set()
_jobs: dict[str, "Job"] = {}
_active_job_by_project: dict[str, str] = {}     # pid -> job_id of the in-flight job
_running_tasks: set[asyncio.Task] = set()        # strong refs so tasks aren't GC'd


# ----------------------------------------------------------------------------- helpers
def load_global_config() -> Config:
    if not CONFIG_PATH.exists():
        raise HTTPException(400, "config.toml not found. Complete setup first.")
    return Config.load(CONFIG_PATH)


def require_project(pid: str) -> dict:
    project = pj.get_project(pid)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def project_cfg(pid: str) -> tuple[dict, Config]:
    project = require_project(pid)
    return project, pj.project_config(load_global_config(), project)


def classify(ch: Chapter, cfg: Config) -> str:
    # An offline-reconstructed chapter has no source text to measure, so it carries
    # the language it was saved with.
    saved_lang = getattr(ch, "language", None)
    if saved_lang:
        return saved_lang
    if not ch.paragraphs:
        return "empty"
    if hangul_fraction(ch.text) < cfg.translation.min_hangul_fraction:
        return "english"
    return "korean"


# A saved chapter's status implies its language when the source text is gone.
_STATUS_LANG = {
    state_mod.STATUS_ENGLISH: "english",
    state_mod.STATUS_EMPTY: "empty",
}


class LocalChapter(Chapter):
    """A chapter rebuilt from saved ``state.json`` when the source Google Doc can't
    be fetched (e.g. the novel was copied to a device without access to the doc).

    Titles, statuses, metrics, and the translated files are all local, so the novel
    stays fully readable. The Korean *source* text isn't recoverable offline, so it
    reports empty source and carries the saved language/metrics directly.
    """

    def __init__(self, index: int, title: str, metrics: ChapterMetrics, language: str):
        super().__init__(index=index, title=title, paragraphs=[])
        self._metrics = metrics
        self.language = language

    @property
    def text(self) -> str:
        return ""

    @property
    def metrics(self) -> ChapterMetrics:
        return self._metrics


def _local_chapters(pid: str) -> list[Chapter]:
    """Rebuild a readable chapter list from a project's saved state (no network)."""
    state = State.load(pj.PROJECTS_DIR / pid / "state.json")
    chapters: list[Chapter] = []
    for key, rec in state.chapters.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        val = rec.get("validation") or {}
        metrics = ChapterMetrics(
            paragraph_count=val.get("source_paragraphs", 0) or 0,
            dialogue_count=val.get("source_dialogue", 0) or 0,
            char_count=rec.get("source_chars") or val.get("source_chars", 0) or 0,
            content_hash=rec.get("source_hash", ""),
        )
        language = _STATUS_LANG.get(rec.get("status"), "korean")
        title = rec.get("title") or f"Chapter {idx}"
        chapters.append(LocalChapter(idx, title, metrics, language))
    chapters.sort(key=lambda c: c.index)
    return chapters


def get_chapters(pid: str, cfg: Config, refresh: bool = False) -> list[Chapter]:
    if refresh or pid not in _chapter_cache:
        project = pj.get_project(pid) or {}
        if project.get("source_type") == "text":
            # Pasted / uploaded text — read from the stored source, no network.
            _chapter_cache[pid] = pj.load_text_chapters(pid)
            _offline_projects.discard(pid)
        else:
            try:
                # Non-interactive: never pops a browser sign-in inside the server.
                creds = load_saved_credentials(cfg.google.token_file)
                doc = fetch_document(build_docs_service(creds), cfg.google.source_doc_id)
                chapters = extract_chapters(
                    doc, flatten_child_tabs=cfg.google.flatten_child_tabs
                )
                # Snapshot the source so this novel is self-contained from now on
                # (readable with its Korean source on any device, copyable, backup-able).
                try:
                    pj.cache_source(pid, chapters)
                except OSError:
                    pass  # caching is best-effort; never block reading on it
                _chapter_cache[pid] = chapters
                _offline_projects.discard(pid)
            except Exception:
                # The source doc can't be fetched here (no access under this device's
                # Google login, revoked token, or no internet). Rather than 500, fall
                # back to the best local copy: a cached source snapshot (full Korean
                # source) if we have one, else a read-only list rebuilt from state.
                # A never-translated novel with no local data still surfaces the error.
                cached = pj.load_cached_source(pid)
                if cached:
                    _chapter_cache[pid] = cached
                    _offline_projects.add(pid)
                else:
                    local = _local_chapters(pid)
                    if not local:
                        raise
                    _chapter_cache[pid] = local
                    _offline_projects.add(pid)
    return _chapter_cache[pid]


def _output_total(pid: str, chapters: list[Chapter]) -> int:
    """The chapter count whose zero-padding reproduces the chapter-NN.md files on
    disk. Online/cached novels carry the full list, so this is just len(chapters).
    A state-only offline rebuild can have FEWER records than the real doc, which
    would shrink the pad width and miss the files — so recover the true width from
    the saved chapter_count and the widest existing filename."""
    total = len(chapters)
    if pid in _offline_projects:
        project = pj.get_project(pid) or {}
        try:
            total = max(total, int(project.get("chapter_count") or 0))
        except (TypeError, ValueError):
            pass
        chdir = pj.PROJECTS_DIR / pid / "chapters"
        if chdir.is_dir():
            widest = 0
            for f in chdir.glob("chapter-*.md"):
                tail = f.stem.split("-", 1)[-1]
                if tail.isdigit():
                    widest = max(widest, len(tail))
            if widest:
                total = max(total, 10 ** (widest - 1))  # smallest int of that digit width
    return total


def fetch_doc_title(doc_id: str) -> str:
    cfg = load_global_config()
    creds = get_credentials(cfg.google.credentials_file, cfg.google.token_file)
    docs = build_docs_service(creds)
    meta = docs.documents().get(documentId=doc_id, fields="title").execute()
    return meta.get("title", "")


def chapter_row(ch: Chapter, cfg: Config, state: State, total: int) -> dict:
    lang = classify(ch, cfg)
    rec = state.get(ch.index) or {}
    m = ch.metrics
    status_val = rec.get("status") or (
        "empty" if lang == "empty" else ("english-source" if lang == "english" else "pending")
    )
    return {
        "index": ch.index,
        "title": ch.title,
        "language": lang,
        "paragraphs": m.paragraph_count,
        "dialogue": m.dialogue_count,
        "chars": m.char_count,
        "status": status_val,
        "cost_usd": rec.get("cost_usd", 0.0),
        "failures": rec.get("failures", []),
        "has_output": (cfg.paths.output_dir / chapter_filename(ch.index, total)).exists(),
    }


# ----------------------------------------------------------------------------- status / settings
@app.get("/api/status")
def status() -> dict:
    cfg_exists = CONFIG_PATH.exists()
    cfg = Config.load(CONFIG_PATH) if cfg_exists else None
    creds_file = cfg.google.credentials_file if cfg else Path("client_secret.json")
    token_file = cfg.google.token_file if cfg else Path("token.json")
    return {
        "config_present": cfg_exists,
        "google_client_secret_present": Path(creds_file).exists(),
        "google_logged_in": Path(token_file).exists(),
        "claude_logged_in": CLAUDE_CREDENTIALS.exists(),
        "model": cfg.anthropic.model if cfg else None,
    }


class Settings(BaseModel):
    model: str | None = None
    effort: str | None = None
    deep_check: str | None = None


@app.get("/api/settings")
def get_settings() -> dict:
    cfg = load_global_config()
    return {
        "model": cfg.anthropic.model,
        "effort": cfg.anthropic.effort,
        "deep_check": cfg.translation.deep_check,
        "chunk_threshold": cfg.translation.chunk_threshold,
        "length_ratio_min": cfg.validation.length_ratio_min,
        "length_ratio_max": cfg.validation.length_ratio_max,
    }


@app.post("/api/settings")
def update_settings(s: Settings) -> dict:
    if not CONFIG_PATH.exists():
        raise HTTPException(400, "config.toml not found.")
    # Validate strictly so a bad value can never corrupt the global TOML.
    if s.model is not None and not _MODEL_RE.match(s.model):
        raise HTTPException(400, "Invalid model id.")
    if s.effort is not None and s.effort not in _EFFORTS:
        raise HTTPException(400, "Invalid effort level.")
    if s.deep_check is not None and s.deep_check not in _DEEP_MODES:
        raise HTTPException(400, "Invalid deep-check mode.")
    text = CONFIG_PATH.read_text(encoding="utf-8")

    def setkey(t: str, key: str, value: str, section: str | None = None) -> str:
        # Values are already restricted to safe characters above; inserted literally.
        pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)"[^"]*"'
        if re.search(pattern, t):
            return re.sub(pattern, lambda m: m.group(1) + '"' + value + '"', t)
        if section:  # key missing (older config) — insert it under its section header
            sec = rf'(?m)^(\[{re.escape(section)}\]\s*\n)'
            if re.search(sec, t):
                return re.sub(sec, lambda m: m.group(1) + f'{key} = "{value}"\n', t, count=1)
            return t.rstrip() + f'\n\n[{section}]\n{key} = "{value}"\n'
        return t

    if s.model is not None:
        text = setkey(text, "model", s.model)
    if s.effort is not None:
        text = setkey(text, "effort", s.effort)
    if s.deep_check is not None:
        text = setkey(text, "deep_check", s.deep_check, section="translation")
    CONFIG_PATH.write_text(text, encoding="utf-8")
    return {"ok": True}


@app.post("/api/init")
def init_config() -> dict:
    """Create config.toml from the example if it doesn't exist (first-run setup)."""
    example = PROJECT_ROOT / "config.example.toml"
    if not CONFIG_PATH.exists() and example.exists():
        import shutil

        shutil.copyfile(example, CONFIG_PATH)
    return status()


@app.post("/api/google/login")
async def google_login() -> dict:
    cfg = load_global_config()
    await run_in_threadpool(get_credentials, cfg.google.credentials_file, cfg.google.token_file)
    return {"ok": True}


# ----------------------------------------------------------------------------- projects
class CreateProject(BaseModel):
    url: str  # a pasted Google Docs URL or a bare doc id
    name: str | None = None


def project_summary(project: dict) -> dict:
    """Light per-project progress from saved state (no network)."""
    cfg = pj.project_config(load_global_config(), project)
    state = State.load(cfg.paths.state_file)
    counts: dict[str, int] = {}
    for rec in state.chapters.values():
        s = rec.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    return {
        **project,
        "counts": counts,
        "translated": counts.get("validated", 0),
        "needs_review": counts.get("needs-review", 0),
        "cost_usd": state.totals().get("cost_usd", 0.0),
        "chapter_count": project.get("chapter_count"),
        "source_type": project.get("source_type", "gdoc"),
        # Effective per-novel style (project override, else global default) so the
        # novel-settings UI shows what's actually in force.
        "style_note": cfg.translation.style_note,
        "instructions": cfg.translation.extra_instruction,
        "honorific_note": cfg.translation.honorific_note,
    }


def _safe_summary(project: dict) -> dict:
    """project_summary, but a single broken project never breaks the whole library."""
    try:
        return project_summary(project)
    except Exception as exc:  # noqa: BLE001 — defensive: keep the library loading
        return {**project, "counts": {}, "translated": 0, "needs_review": 0,
                "cost_usd": 0.0, "chapter_count": project.get("chapter_count"),
                "error": f"could not load: {exc}"}


@app.get("/api/projects")
def list_projects() -> dict:
    return {"projects": [_safe_summary(p) for p in pj.list_projects()]}


# ----------------------------------------------------------------------------- backup / move
@app.get("/api/backup")
def export_all_bundle() -> Response:
    """Download every novel as one portable .zip (a full library backup)."""
    pids = [p["id"] for p in pj.list_projects()]
    if not pids:
        raise HTTPException(400, "No novels to back up yet.")
    data = pj.export_bundle(pids)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="night-reader-backup.zip"'})


@app.post("/api/import")
async def import_projects(request: Request) -> dict:
    """Restore novels from a bundle made by Export/Backup. The .zip is sent as the
    raw request body (no multipart dependency needed)."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "No file was uploaded.")
    try:
        imported = pj.import_bundle(data)
    except zipfile.BadZipFile:
        raise HTTPException(400, "That doesn't look like a novel backup (.zip).")
    except Exception as exc:  # noqa: BLE001 — surface a friendly message, not a 500
        raise HTTPException(400, f"Couldn't read that backup: {exc}")
    if not imported:
        raise HTTPException(400, "No novels were found in that file.")
    for p in imported:
        _chapter_cache.pop(p["id"], None)
        _offline_projects.discard(p["id"])
    return {"imported": [_safe_summary(p) for p in imported]}


@app.get("/api/search")
def search_all(q: str = "") -> dict:
    """Search translated text across ALL novels. Reads the saved chapter files on
    disk, so it needs no network and works fully offline."""
    needle = (q or "").strip().lower()
    if not needle:
        return {"results": []}
    results: list[dict] = []
    for project in pj.list_projects():
        pid = project["id"]
        chdir = pj.PROJECTS_DIR / pid / "chapters"
        if not chdir.is_dir():
            continue
        state = State.load(pj.PROJECTS_DIR / pid / "state.json")
        for f in sorted(chdir.glob("chapter-*.md")):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            pos = text.lower().find(needle)
            if pos == -1:
                continue
            m = re.search(r"chapter-(\d+)", f.stem)
            idx = int(m.group(1)) if m else 0
            rec = state.get(idx) or {}
            start, end = max(0, pos - 40), min(len(text), pos + len(needle) + 60)
            snippet = (("…" if start else "") + text[start:end].replace("\n", " ").strip()
                       + ("…" if end < len(text) else ""))
            results.append({"project_id": pid, "project_name": project.get("name", "?"),
                            "index": idx, "title": rec.get("title") or f"Chapter {idx}",
                            "snippet": snippet})
            if len(results) >= 300:
                return {"results": results, "truncated": True}
    return {"results": results}


@app.post("/api/projects")
async def create_project(body: CreateProject) -> dict:
    doc_id = pj.extract_doc_id(body.url)
    if not doc_id:
        raise HTTPException(400, "Could not find a Google Doc id in that link.")
    existing = pj.find_project_by_doc(doc_id)
    if existing:
        raise HTTPException(409, {"message": "A novel with this document already exists.",
                                  "project_id": existing["id"]})
    name = (body.name or "").strip()
    if not name:
        try:
            name = await run_in_threadpool(fetch_doc_title, doc_id)
        except Exception as exc:
            raise HTTPException(400, f"Couldn't open that document: {exc}")
    project = pj.create_project(name or "Untitled novel", doc_id)
    return project


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    return project_summary(require_project(pid))


@app.delete("/api/projects/{pid}")
def delete_project(pid: str) -> dict:
    if not pj.delete_project(pid):
        raise HTTPException(404, "project not found")
    _chapter_cache.pop(pid, None)
    return {"ok": True}


class CreateTextProject(BaseModel):
    name: str = ""
    text: str
    split_mode: str = "separator"  # separator | heading | single
    separator: str = "---"


@app.post("/api/projects/text")
def create_text_project(body: CreateTextProject) -> dict:
    """Create a novel from pasted/uploaded text (no Google account needed)."""
    chapters = split_text_into_chapters(body.text, body.split_mode, body.separator)
    if not chapters:
        raise HTTPException(400, "Couldn't find any chapters in that text.")
    project = pj.create_text_project(body.name or "Untitled novel", chapters)
    _chapter_cache[project["id"]] = chapters
    return project_summary(project)


class ProjectUpdate(BaseModel):
    name: str | None = None
    style_note: str | None = None
    instructions: str | None = None
    honorific_note: str | None = None


@app.post("/api/projects/{pid}")
def update_project(pid: str, body: ProjectUpdate) -> dict:
    """Rename a novel and/or edit its per-novel translation style settings."""
    require_project(pid)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    project = pj.update_project(pid, **fields)
    return project_summary(project)


@app.get("/api/projects/{pid}/search")
def search_chapters(pid: str, q: str = "") -> dict:
    """Case-insensitive substring search over the translated chapter text."""
    _, cfg = project_cfg(pid)
    needle = (q or "").strip().lower()
    if not needle:
        return {"results": []}
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    results = []
    for ch in chapters:
        path = cfg.paths.output_dir / chapter_filename(ch.index, total)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        pos = text.lower().find(needle)
        if pos == -1:
            continue
        start, end = max(0, pos - 40), min(len(text), pos + len(needle) + 60)
        snippet = (("…" if start else "") + text[start:end].replace("\n", " ").strip()
                   + ("…" if end < len(text) else ""))
        results.append({"index": ch.index, "title": ch.title, "snippet": snippet})
    return {"results": results}


def _safe_name(name: str) -> str:
    base = re.sub(r"[^\w\- ]+", "", name or "novel").strip().replace(" ", "_")
    return base[:60] or "novel"


def _translated_chapters(pid: str, cfg: Config) -> list[tuple[int, str, str]]:
    """(index, title, markdown) for every chapter that has a written translation."""
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    out = []
    for ch in chapters:
        path = cfg.paths.output_dir / chapter_filename(ch.index, total)
        if path.exists():
            out.append((ch.index, ch.title, path.read_text(encoding="utf-8")))
    return out


@app.get("/api/projects/{pid}/export")
def export_novel(pid: str, format: str = "md") -> Response:
    """Download the finished translation as Markdown, plain text, or EPUB."""
    project, cfg = project_cfg(pid)
    items = _translated_chapters(pid, cfg)
    if not items:
        raise HTTPException(400, "No translated chapters to export yet.")
    name = project.get("name", "novel")
    fmt = (format or "md").lower()

    if fmt == "epub":
        out_path = pj.PROJECTS_DIR / pid / "export.epub"
        build_epub(name, "Night Reader", [(t, body) for _i, t, body in items], out_path)
        return FileResponse(out_path, media_type="application/epub+zip",
                            filename=f"{_safe_name(name)}.epub")
    if fmt == "txt":
        parts = [f"{t}\n\n{re.sub(r'[*_#>`]', '', body).strip()}\n" for _i, t, body in items]
        content, media, ext = "\n\n\n".join(parts) + "\n", "text/plain; charset=utf-8", "txt"
    else:
        parts = [f"# {t}\n\n{body.strip()}\n" for _i, t, body in items]
        content, media, ext = "\n\n".join(parts) + "\n", "text/markdown; charset=utf-8", "md"
    return Response(content=content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{_safe_name(name)}.{ext}"'})


@app.get("/api/projects/{pid}/bundle")
def export_project_bundle(pid: str) -> Response:
    """Download this one novel as a portable .zip (chapters, glossary, progress,
    and the cached source) — move it to another device or keep it as a backup."""
    project = require_project(pid)
    data = pj.export_bundle([pid])
    name = _safe_name(project.get("name", "novel"))
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.novel.zip"'})


@app.get("/api/projects/{pid}/chapters")
def list_chapters(pid: str, refresh: bool = False) -> dict:
    project, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg, refresh=refresh)
    offline = pid in _offline_projects
    total = len(chapters)
    file_total = _output_total(pid, chapters)  # pad chapter-NN.md to match files on disk
    # Don't overwrite the saved chapter_count from an offline copy — it may be partial.
    if not offline and project.get("chapter_count") != total:  # cache total for the library
        project["chapter_count"] = total
        (pj.PROJECTS_DIR / pid / "project.json").write_text(
            json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    state = State.load(cfg.paths.state_file)
    rows = [chapter_row(ch, cfg, state, file_total) for ch in chapters]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"project": project, "total": total, "chapters": rows,
            "counts": counts, "totals": state.totals(), "offline": offline}


@app.get("/api/projects/{pid}/chapters/{index}")
def chapter_detail(pid: str, index: int) -> dict:
    project, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    ch = next((c for c in chapters if c.index == index), None)
    if ch is None:
        raise HTTPException(404, f"chapter {index} not found")
    out_path = cfg.paths.output_dir / chapter_filename(index, total)
    translation = out_path.read_text(encoding="utf-8") if out_path.exists() else None
    rec = State.load(cfg.paths.state_file).get(index) or {}
    return {
        "index": index,
        "title": ch.title,
        "language": classify(ch, cfg),
        "source": ch.text,
        "translation": translation,
        "status": rec.get("status", "pending"),
        "validation": rec.get("validation"),
        "failures": rec.get("failures", []),
        "manual_edit": rec.get("manual_edit", False),
        "offline": pid in _offline_projects,
    }


class ChapterEdit(BaseModel):
    translation: str


@app.put("/api/projects/{pid}/chapters/{index}")
def save_chapter(pid: str, index: int, body: ChapterEdit) -> dict:
    """Save a hand-edited translation. Marks the chapter validated (user-approved)."""
    project, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    ch = next((c for c in chapters if c.index == index), None)
    if ch is None:
        raise HTTPException(404, f"chapter {index} not found")
    text = body.translation.strip()
    if not text:
        raise HTTPException(400, "The translation is empty.")
    write_chapter_file(cfg.paths.output_dir, index, total, text)
    state = State.load(cfg.paths.state_file)
    state.update(
        index,
        status=state_mod.STATUS_VALIDATED,
        title=ch.title,
        source_hash=ch.metrics.content_hash,
        failures=[],
        manual_edit=True,
    )
    state.save(cfg.paths.state_file)
    return {"ok": True, "status": state_mod.STATUS_VALIDATED}


def _chapter_problems(pid: str, cfg: Config, index: int) -> dict:
    """Scan a chapter's saved translation for problems: leaked AI reasoning,
    untranslated Korean, and the length/structure validation checks."""
    chapters = get_chapters(pid, cfg)
    total = len(chapters)
    ch = next((c for c in chapters if c.index == index), None)
    if ch is None:
        raise HTTPException(404, f"chapter {index} not found")
    path = cfg.paths.output_dir / chapter_filename(index, total)
    if not path.exists():
        return {"index": index, "translated": False, "ok": True, "auto_fixable": False, "problems": []}
    text = path.read_text(encoding="utf-8")

    problems: list[dict] = []
    leaks = find_leaks(text)
    if leaks:
        problems.append({"type": "reasoning_leak", "severity": "high", "auto_fixable": True,
                         "message": f"AI reasoning/notes left in the text — e.g. “{leaks[0][:90]}”"})
    kf = korean_fraction(text)
    if kf > 0.02:
        cleaned, _ = strip_reasoning(text)
        problems.append({"type": "untranslated_korean",
                         "severity": "high" if kf > 0.10 else "medium",
                         "auto_fixable": korean_fraction(cleaned) <= 0.02,
                         "message": f"Untranslated Korean remains (~{round(kf * 100)}% of the text)"})
    val = validate_translation(ch, text, cfg.validation)
    for f in val.failures:
        if "leaked" in f or "untranslated Korean" in f:
            continue  # already reported above with a fix path
        problems.append({"type": "structure", "severity": "medium", "auto_fixable": False, "message": f})
    for w in val.warnings:
        problems.append({"type": "warning", "severity": "low", "auto_fixable": False, "message": w})

    return {"index": index, "translated": True, "problems": problems,
            "auto_fixable": any(p["auto_fixable"] for p in problems), "ok": not problems}


@app.get("/api/projects/{pid}/chapters/{index}/scan")
def scan_chapter(pid: str, index: int) -> dict:
    _, cfg = project_cfg(pid)
    return _chapter_problems(pid, cfg, index)


@app.post("/api/projects/{pid}/chapters/{index}/scan/deep")
async def scan_chapter_deep(pid: str, index: int) -> dict:
    """Deep check: have Claude read the whole chapter and flag any non-story text the
    regex can't anticipate (anywhere, not just the first line). Uses your plan."""
    _, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = len(chapters)
    ch = next((c for c in chapters if c.index == index), None)
    if ch is None:
        raise HTTPException(404, f"chapter {index} not found")
    path = cfg.paths.output_dir / chapter_filename(index, total)
    if not path.exists():
        raise HTTPException(400, "This chapter isn't translated yet.")
    text = path.read_text(encoding="utf-8")
    translator = Translator(cfg.anthropic, cfg.translation)
    try:
        snippets = await run_in_threadpool(translator.find_meta_leaks, text)
    except RateLimitedError as exc:
        raise HTTPException(429, f"{exc} (this used your plan's allowance — try again later)")
    except TranslatorError as exc:
        raise HTTPException(502, f"Deep check failed: {exc}")

    base = _chapter_problems(pid, cfg, index)  # include the fast regex findings too
    seen = {p["message"] for p in base["problems"]}
    for s in snippets:
        if s and s in text and s[:120] not in seen:
            base["problems"].append({"type": "ai_detected", "severity": "high",
                                     "auto_fixable": True, "snippet": s, "message": s[:120]})
    base["auto_fixable"] = any(p["auto_fixable"] for p in base["problems"])
    base["ok"] = not base["problems"]
    base["deep"] = True
    return base


class FixRequest(BaseModel):
    remove: list[str] = []  # exact snippets to delete (from the AI deep-check)


@app.post("/api/projects/{pid}/chapters/{index}/fix")
def fix_chapter(pid: str, index: int, body: FixRequest = FixRequest()) -> dict:
    """Auto-resolve what's safe to fix in place: strip leaked AI reasoning / source
    echoes, plus any exact snippets the deep-check flagged. Issues that need a
    re-translate are reported back unchanged."""
    _, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = len(chapters)
    ch = next((c for c in chapters if c.index == index), None)
    if ch is None:
        raise HTTPException(404, f"chapter {index} not found")
    path = cfg.paths.output_dir / chapter_filename(index, total)
    if not path.exists():
        raise HTTPException(400, "This chapter isn't translated yet.")

    text = path.read_text(encoding="utf-8")
    text, snip_removed = remove_snippets(text, body.remove)
    cleaned, removed = strip_reasoning(text)
    fixed = (snip_removed > 0 or bool(removed)) and bool(cleaned.strip())
    if fixed:
        # Preserve the original once, then write the cleaned version.
        bdir = pj.PROJECTS_DIR / pid / "chapters_preclean_backup"
        bdir.mkdir(exist_ok=True)
        bpath = bdir / chapter_filename(index, total)
        if not bpath.exists():
            bpath.write_text(text, encoding="utf-8")
        write_chapter_file(cfg.paths.output_dir, index, total, cleaned)
        val = validate_translation(ch, cleaned, cfg.validation)
        state = State.load(cfg.paths.state_file)
        state.update(index, title=ch.title,
                     status=state_mod.STATUS_VALIDATED if val.ok else state_mod.STATUS_NEEDS_REVIEW,
                     failures=val.failures, manual_edit=True)
        state.save(cfg.paths.state_file)
    return {"fixed": fixed, "removed": len(removed) + snip_removed, **_chapter_problems(pid, cfg, index)}


# ----------------------------------------------------------------------------- glossary
@app.get("/api/projects/{pid}/glossary")
def glossary(pid: str) -> dict:
    _, cfg = project_cfg(pid)
    g = Glossary.load(cfg.paths.glossary_json)
    return {"locked": [asdict(e) for e in g.entries()],
            "pending": load_pending(cfg.paths.glossary_pending)}


class TermDecision(BaseModel):
    korean: str
    english: str
    type: str = "other"
    note: str = ""


class GlossaryReview(BaseModel):
    approve: list[TermDecision] = []
    reject: list[str] = []  # korean keys to drop from pending


@app.post("/api/projects/{pid}/glossary/review")
def review_glossary(pid: str, review: GlossaryReview) -> dict:
    _, cfg = project_cfg(pid)
    g = Glossary.load(cfg.paths.glossary_json)
    for t in review.approve:
        g.add(GlossaryEntry(korean=t.korean, english=t.english, type=t.type, note=t.note))
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    decided = {t.korean for t in review.approve} | set(review.reject)
    remaining = [p for p in load_pending(cfg.paths.glossary_pending) if p["korean"] not in decided]
    save_pending(cfg.paths.glossary_pending, remaining)
    return {"approved": len(review.approve), "rejected": len(review.reject), "pending": len(remaining)}


class TermUpsert(BaseModel):
    # Accept the wire/CSV key "register" but avoid shadowing a BaseModel attribute.
    model_config = ConfigDict(populate_by_name=True)
    korean: str = ""   # optional: an English-only canonical name has no Korean yet
    english: str
    type: str = "other"
    note: str = ""
    pronoun: str = ""   # character profile: he / she / they
    speech_register: str = Field("", alias="register")  # formal / casual / polite …
    original_korean: str | None = None    # the entry being edited, identified by Korean…
    original_english: str | None = None   # …or by English when it's a canonical name


class TermDelete(BaseModel):
    korean: str = ""
    english: str = ""


class GlossaryImport(BaseModel):
    entries: list[TermUpsert] = []
    mode: str = "merge"  # merge | replace


def _entry_from(body: TermUpsert) -> GlossaryEntry:
    typ = body.type.strip().lower()
    return GlossaryEntry(
        korean=body.korean.strip(),
        english=body.english.strip(),
        type=typ if typ in VALID_TYPES else "other",
        note=body.note.strip(),
        pronoun=body.pronoun.strip(),
        register=body.speech_register.strip(),
    )


def _affected_chapters(pid: str, cfg: Config, koreans: set[str]) -> list[dict]:
    """Already-translated chapters whose source contains any of these Korean terms —
    i.e. the chapters that would go stale after a glossary spelling change."""
    koreans = {k for k in koreans if k}
    if not koreans:
        return []
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    out = []
    for ch in chapters:
        path = cfg.paths.output_dir / chapter_filename(ch.index, total)
        if path.exists() and any(k in ch.text for k in koreans):
            out.append({"index": ch.index, "title": ch.title})
    return out


def _locked_payload(g: Glossary) -> dict:
    return {"locked": [asdict(e) for e in g.entries()]}


@app.post("/api/projects/{pid}/glossary/term")
def upsert_glossary_term(pid: str, body: TermUpsert) -> dict:
    """Add a new locked glossary term, or edit/rename an existing one directly.

    Unlike the review queue, this commits straight to the master glossary — the
    user is the authority here. Renaming (changing the Korean key) drops the old
    entry so it can't linger as a stale duplicate. The response lists already-
    translated chapters that reference the term so the UI can offer to refresh them.
    """
    _, cfg = project_cfg(pid)
    if not body.english.strip():
        raise HTTPException(400, "An English spelling is required.")
    g = Glossary.load(cfg.paths.glossary_json)
    entry = _entry_from(body)
    # Drop the entry being edited (renamed Korean, or an English-only name being remapped).
    original_k = (body.original_korean or "").strip()
    original_e = (body.original_english or "").strip()
    if original_k and original_k != entry.korean:
        g.remove(original_k)
    elif original_e and not original_k and (entry.korean or original_e.lower() != entry.english.lower()):
        g.remove_english(original_e)
    g.add(entry)
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return {**_locked_payload(g),
            "affected": _affected_chapters(pid, cfg, {entry.korean, original_k})}


@app.post("/api/projects/{pid}/glossary/term/delete")
def delete_glossary_term(pid: str, body: TermDelete) -> dict:
    _, cfg = project_cfg(pid)
    g = Glossary.load(cfg.paths.glossary_json)
    korean = body.korean.strip()
    removed = g.remove(korean) if korean else g.remove_english(body.english.strip())
    if not removed:
        raise HTTPException(404, "term not found")
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return _locked_payload(g)


@app.post("/api/projects/{pid}/glossary/import")
def import_glossary(pid: str, body: GlossaryImport) -> dict:
    """Bulk add terms from a CSV/JSON the client parsed. mode=replace clears first."""
    _, cfg = project_cfg(pid)
    g = Glossary([]) if body.mode == "replace" else Glossary.load(cfg.paths.glossary_json)
    imported = 0
    for t in body.entries:
        entry = _entry_from(t)
        if not entry.english:  # Korean optional (canonical names), English required
            continue
        g.add(entry)
        imported += 1
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return {**_locked_payload(g), "imported": imported}


class GlossaryCopy(BaseModel):
    source_pid: str
    mode: str = "merge"  # merge | replace


@app.post("/api/projects/{pid}/glossary/copy")
def copy_glossary(pid: str, body: GlossaryCopy) -> dict:
    """Copy the locked glossary from another novel into this one — keeps character
    names/terms consistent across parts of the same series."""
    require_project(pid)
    if body.source_pid == pid:
        raise HTTPException(400, "Choose a different novel to copy from.")
    _, cfg = project_cfg(pid)
    _, src_cfg = project_cfg(body.source_pid)  # 404s if the source novel doesn't exist
    src_entries = Glossary.load(src_cfg.paths.glossary_json).entries()
    g = Glossary([]) if body.mode == "replace" else Glossary.load(cfg.paths.glossary_json)
    before = len(g.entries())
    for e in src_entries:
        g.add(e)
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return {**_locked_payload(g), "copied": len(src_entries),
            "added": len(g.entries()) - before}


def _sample_english(chapters: list[Chapter], budget: int = 80000) -> str:
    """Sample the head of English chapters spread across the whole novel (not just the
    first few) up to a char budget, so the name extractor sees the full cast/places."""
    if not chapters:
        return ""
    # Pick up to ~40 chapters evenly spaced so late-introduced characters are covered.
    cap = min(len(chapters), 40)
    step = max(1, len(chapters) // cap)
    picked = chapters[::step][:cap]
    per = max(800, budget // len(picked))
    parts = []
    for ch in picked:
        parts.append(f"--- {ch.title} ---\n{ch.text[:per]}")
    return "\n\n".join(parts)


@app.post("/api/projects/{pid}/glossary/learn")
async def learn_glossary(pid: str) -> dict:
    """Read the already-English chapters and seed the glossary with their established
    names/terms, so newly translated chapters keep the same spellings."""
    _, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    english_chs = [ch for ch in chapters if classify(ch, cfg) == "english"]
    if not english_chs:
        raise HTTPException(400, "No already-English chapters were found to learn from.")
    sample = _sample_english(english_chs)
    translator = Translator(cfg.anthropic, cfg.translation)
    try:
        terms = await run_in_threadpool(translator.extract_glossary, sample)
    except RateLimitedError as exc:
        raise HTTPException(429, f"{exc} (this used your plan's allowance — try again later)")
    except TranslatorError as exc:
        raise HTTPException(502, f"Couldn't read the chapters: {exc}")

    g = Glossary.load(cfg.paths.glossary_json)
    existing_english = {e.english.lower() for e in g.entries() if e.english}
    added = 0
    for t in terms:
        english = t.get("english", "").strip()
        if not english or english.lower() in existing_english:
            continue
        typ = t.get("type", "name")
        g.add(GlossaryEntry(korean="", english=english,
                            type=typ if typ in VALID_TYPES else "name",
                            note=t.get("note", "")))
        existing_english.add(english.lower())
        added += 1
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return {"learned": added, "from_chapters": len(english_chs), **_locked_payload(g)}


@app.get("/api/projects/{pid}/glossary/export")
def export_glossary(pid: str, format: str = "csv") -> Response:
    project, cfg = project_cfg(pid)
    entries = Glossary.load(cfg.paths.glossary_json).entries()
    name = _safe_name(project.get("name", "glossary"))
    if (format or "csv").lower() == "json":
        content = json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=2)
        return Response(content, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}-glossary.json"'})
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["korean", "english", "type", "pronoun", "register", "note"])
    for e in entries:
        writer.writerow([e.korean, e.english, e.type, e.pronoun, e.register, e.note])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}-glossary.csv"'})


# ----------------------------------------------------------------------------- translation jobs
class TranslateRequest(BaseModel):
    indices: list[int] | None = None  # None = all Korean chapters not yet done
    force: bool = False


class Job:
    """A per-project translation worker fed by an APPENDABLE FIFO queue. Chapters can
    be enqueued while it runs, so the user never has to wait for one to finish before
    queuing the next. One worker per project keeps writes to state.json serialized."""

    def __init__(self, job_id: str, pid: str):
        self.id = job_id
        self.pid = pid
        self.pending: deque[tuple[int, bool]] = deque()  # (chapter index, force)
        self.queued: set[int] = set()        # indices pending or in-flight (for dedup)
        self.current: int | None = None       # chapter being translated right now
        self.history: list[dict] = []         # every event so far, replayed on (re)connect
        self.subscribers: list[asyncio.Queue] = []  # one queue per live SSE consumer
        self.done = False
        self.cancelled = False
        self.terminal: dict | None = None      # final event, replayable for late consumers

    def enqueue(self, items: list[tuple[int, bool]]) -> list[int]:
        """Append (index, force) pairs, skipping ones already queued/in-flight."""
        added = []
        for idx, force in items:
            if idx in self.queued:
                continue
            self.queued.add(idx)
            self.pending.append((idx, force))
            added.append(idx)
        return added

    def queue_state(self) -> dict:
        return {"current": self.current, "pending": [i for i, _ in self.pending]}

    def publish(self, ev: dict) -> None:
        """Fan an event out to every connected stream and remember it for replay.

        Non-terminal events are stamped with the live queue state; terminal
        (paused/done) events carry their own. Multiple consumers (two tabs, a
        reconnect, dev StrictMode) each get their own copy — no event splitting."""
        if ev.get("type") not in ("paused", "done"):
            ev = {**ev, **self.queue_state()}
        else:
            self.terminal = ev
        self.history.append(ev)
        if len(self.history) > 1000:
            self.history = self.history[-1000:]
        for q in list(self.subscribers):
            q.put_nowait(ev)


async def _run_worker(job: Job, cfg: Config) -> None:
    chapters = get_chapters(job.pid, cfg)
    total = len(chapters)
    by_index = {c.index: c for c in chapters}
    glossary = Glossary.load(cfg.paths.glossary_json)
    state = State.load(cfg.paths.state_file)
    translator = Translator(cfg.anthropic, cfg.translation, canonical_names=glossary.canonical())

    # Drain the queue. The only await is run_in_threadpool, so an enqueue arriving
    # mid-flight is always observed on the next loop iteration (no lost work).
    while job.pending and not job.cancelled:
        idx, force = job.pending.popleft()
        job.current = idx
        ch = by_index.get(idx)
        if ch is None:
            job.queued.discard(idx)
            job.current = None
            continue
        if not force and state.is_done(idx, ch.metrics.content_hash):
            job.queued.discard(idx)
            job.current = None
            job.publish({"type": "chapter", "index": idx, "status": "validated",
                         "title": ch.title, "skipped": True})
            continue
        job.publish({"type": "start", "index": idx, "title": ch.title,
                     "chars": ch.metrics.char_count})
        try:
            status_val = await run_in_threadpool(
                process_chapter, ch, total, translator, glossary, cfg, state
            )
        except RateLimitedError as exc:
            state.update(idx, status=state_mod.STATUS_PENDING, title=ch.title)
            state.save(cfg.paths.state_file)
            # Everything not yet finished (this chapter + the rest of the queue), so
            # the client can resume exactly what's left.
            remaining = [idx] + [i for i, _ in job.pending]
            job.current = None
            job.done = True
            job.publish({"type": "paused", "index": idx, "message": str(exc),
                         "resets_at": getattr(getattr(exc, "info", None), "resets_at", None),
                         "current": None, "pending": remaining})
            return
        except Exception as exc:  # isolation: one bad chapter never kills the queue
            state.update(idx, status=state_mod.STATUS_FAILED, title=ch.title,
                         error=f"{type(exc).__name__}: {exc}")
            state.save(cfg.paths.state_file)
            job.queued.discard(idx)
            job.current = None
            job.publish({"type": "chapter", "index": idx, "status": "failed",
                         "title": ch.title, "error": str(exc)})
            continue
        state.save(cfg.paths.state_file)
        rec = state.get(idx) or {}
        job.queued.discard(idx)
        job.current = None
        job.publish({"type": "chapter", "index": idx, "status": status_val,
                     "title": ch.title, "cost_usd": state.totals()["cost_usd"],
                     "failures": rec.get("failures", [])})

    job.done = True
    job.publish({"type": "done", "totals": State.load(cfg.paths.state_file).totals(),
                 "current": None, "pending": []})


def _spawn_worker(pid: str, cfg: Config, job: Job) -> None:
    # Bound memory: a long-lived server accrues a Job per run. Drop old finished jobs,
    # keeping the active ones plus the few most recent for a late stream's replay.
    if len(_jobs) > 40:
        finished = [jid for jid, j in _jobs.items() if j.done]
        for jid in finished[:-10]:
            _jobs.pop(jid, None)
    _jobs[job.id] = job
    _active_job_by_project[pid] = job.id
    task = asyncio.create_task(_run_worker(job, cfg))
    _running_tasks.add(task)  # strong ref so the task isn't garbage-collected

    def _cleanup(t: asyncio.Task) -> None:
        _running_tasks.discard(t)
        if _active_job_by_project.get(pid) == job.id:
            _active_job_by_project.pop(pid, None)

    task.add_done_callback(_cleanup)


def _resolve_items(pid: str, cfg: Config, req: TranslateRequest) -> list[tuple[int, bool]]:
    if req.indices:
        return [(i, req.force) for i in req.indices]
    chapters = get_chapters(pid, cfg)
    state = State.load(cfg.paths.state_file)
    return [
        (c.index, req.force) for c in chapters
        if classify(c, cfg) == "korean" and not state.is_done(c.index, c.metrics.content_hash)
    ]


@app.post("/api/projects/{pid}/translate")
async def start_translation(pid: str, req: TranslateRequest) -> dict:
    _, cfg = project_cfg(pid)
    get_chapters(pid, cfg)  # establish offline state up front (the indices path skips it)
    if pid in _offline_projects:
        raise HTTPException(409, "This novel is in read-only saved mode — its source "
                            "document isn't available on this device, so it can't be "
                            "translated here.")
    items = _resolve_items(pid, cfg, req)

    # If a worker is already running, just append — it picks the new chapters up.
    existing_id = _active_job_by_project.get(pid)
    if existing_id and existing_id in _jobs and not _jobs[existing_id].done:
        job = _jobs[existing_id]
        added = job.enqueue(items)
        if added:
            job.publish({"type": "queued", "added": added})
        return {"job_id": job.id, "queued": added, "already_running": True, **job.queue_state()}

    job = Job(uuid.uuid4().hex, pid)
    added = job.enqueue(items)
    _spawn_worker(pid, cfg, job)
    return {"job_id": job.id, "queued": added, **job.queue_state()}


@app.post("/api/projects/{pid}/translate/cancel")
def cancel_queue(pid: str) -> dict:
    """Drop the not-yet-started chapters from the queue. The in-flight one finishes."""
    require_project(pid)
    jid = _active_job_by_project.get(pid)
    if jid and jid in _jobs and not _jobs[jid].done:
        job = _jobs[jid]
        job.pending.clear()
        job.queued = {job.current} if job.current is not None else set()
        return {"ok": True, "current": job.current, "pending": []}
    return {"ok": True, "current": None, "pending": []}


@app.get("/api/queue")
def queue_overview() -> dict:
    """Live view of every project's translation queue — powers the library dashboard."""
    jobs = []
    for pid, jid in list(_active_job_by_project.items()):
        job = _jobs.get(jid)
        if job is None or job.done:
            continue
        project = pj.get_project(pid) or {}
        jobs.append({"pid": pid, "name": project.get("name", "Novel"), **job.queue_state()})
    return {"jobs": jobs}


@app.get("/api/projects/{pid}/active-job")
def active_job(pid: str) -> dict:
    """The in-flight translation job for this project, if any — lets the UI reattach
    its live progress stream (and current queue) after a reload or navigating away."""
    require_project(pid)
    jid = _active_job_by_project.get(pid)
    if jid and jid in _jobs and not _jobs[jid].done:
        return {"job_id": jid, **_jobs[jid].queue_state()}
    return {"job_id": None}


@app.get("/api/projects/{pid}/translate/{job_id}/stream")
async def stream_job(pid: str, job_id: str) -> StreamingResponse:
    job = _jobs.get(job_id)
    if job is None or job.pid != pid:
        raise HTTPException(404, "job not found")

    async def gen():
        # Register our own queue first so no event slips through between replay and
        # live (subscribe-then-snapshot); every consumer gets its own copy of events.
        q: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(q)
        try:
            # Replay history so a reconnecting consumer (reload / 2nd tab / post→connect
            # gap) catches up; if the job already finished, history ends with terminal.
            for ev in list(job.history):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("type") in ("done", "paused"):
                    return
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "paused"):
                    break
        finally:
            if q in job.subscribers:
                job.subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ----------------------------------------------------------------------------- static (production)
if DIST_DIR.exists():
    # Serve the built React app when present (the launcher builds it). API routes
    # above are registered first, so they take precedence over this catch-all.
    app.mount("/", StaticFiles(directory=str(DIST_DIR), html=True), name="web")

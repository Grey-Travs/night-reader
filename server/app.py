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
from collections import deque
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from translation_bot import state as state_mod
from translation_bot.config import Config
from translation_bot.docs_extract import Chapter, extract_chapters, fetch_document, hangul_fraction
from translation_bot.epub import build_epub
from translation_bot.glossary import VALID_TYPES, Glossary, GlossaryEntry, load_pending, save_pending
from translation_bot.google_auth import build_docs_service, get_credentials
from translation_bot.pipeline import chapter_filename, process_chapter, write_chapter_file
from translation_bot.state import State
from translation_bot.text_source import split_text_into_chapters
from translation_bot.translator import RateLimitedError, Translator, TranslatorError

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

_chapter_cache: dict[str, list[Chapter]] = {}  # keyed by project id
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
    if not ch.paragraphs:
        return "empty"
    if hangul_fraction(ch.text) < cfg.translation.min_hangul_fraction:
        return "english"
    return "korean"


def get_chapters(pid: str, cfg: Config, refresh: bool = False) -> list[Chapter]:
    if refresh or pid not in _chapter_cache:
        project = pj.get_project(pid) or {}
        if project.get("source_type") == "text":
            # Pasted / uploaded text — read from the stored source, no network.
            _chapter_cache[pid] = pj.load_text_chapters(pid)
        else:
            creds = get_credentials(cfg.google.credentials_file, cfg.google.token_file)
            doc = fetch_document(build_docs_service(creds), cfg.google.source_doc_id)
            _chapter_cache[pid] = extract_chapters(
                doc, flatten_child_tabs=cfg.google.flatten_child_tabs
            )
    return _chapter_cache[pid]


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


@app.get("/api/settings")
def get_settings() -> dict:
    cfg = load_global_config()
    return {
        "model": cfg.anthropic.model,
        "effort": cfg.anthropic.effort,
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
    text = CONFIG_PATH.read_text(encoding="utf-8")

    def setkey(t: str, key: str, value: str) -> str:
        pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)"[^"]*"'
        # Function replacement: value is inserted literally (no regex backreference
        # interpretation), and it is already restricted to safe characters above.
        return re.sub(pattern, lambda m: m.group(1) + '"' + value + '"', t) if re.search(pattern, t) else t

    if s.model is not None:
        text = setkey(text, "model", s.model)
    if s.effort is not None:
        text = setkey(text, "effort", s.effort)
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
    total = len(chapters)
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
    total = len(chapters)
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


@app.get("/api/projects/{pid}/chapters")
def list_chapters(pid: str, refresh: bool = False) -> dict:
    project, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg, refresh=refresh)
    total = len(chapters)
    if project.get("chapter_count") != total:  # cache total on project for the library
        project["chapter_count"] = total
        (pj.PROJECTS_DIR / pid / "project.json").write_text(
            json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    state = State.load(cfg.paths.state_file)
    rows = [chapter_row(ch, cfg, state, total) for ch in chapters]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"project": project, "total": total, "chapters": rows,
            "counts": counts, "totals": state.totals()}


@app.get("/api/projects/{pid}/chapters/{index}")
def chapter_detail(pid: str, index: int) -> dict:
    project, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = len(chapters)
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
    }


class ChapterEdit(BaseModel):
    translation: str


@app.put("/api/projects/{pid}/chapters/{index}")
def save_chapter(pid: str, index: int, body: ChapterEdit) -> dict:
    """Save a hand-edited translation. Marks the chapter validated (user-approved)."""
    project, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = len(chapters)
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
    total = len(chapters)
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

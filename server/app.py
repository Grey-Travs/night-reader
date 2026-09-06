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
import threading
import time
import uuid
import zipfile
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from translation_bot import ocr
from translation_bot import state as state_mod
from translation_bot.atomic import atomic_write_text
from translation_bot.config import Config
from translation_bot.docs_extract import (
    Chapter, ChapterMetrics, extract_chapters, fetch_document, hangul_fraction,
)
from translation_bot.epub import build_epub
from translation_bot.ocr_join import propose_join
from translation_bot.paragraphs import (
    align_korean,
    check_paragraph_result,
    korean_window,
    locate_block,
    splice_block,
    split_blocks,
)
from translation_bot.glossary import (
    VALID_TYPES,
    Glossary,
    GlossaryEntry,
    glossary_lock,
    load_pending,
    normalize_pronoun,
    save_pending,
)
from translation_bot.google_auth import build_docs_service, get_credentials, load_saved_credentials
from translation_bot.pipeline import (
    chapter_filename,
    current_translation,
    fix_pronouns_chapter,
    previous_chapter_path,
    process_chapter,
    read_audit_translation,
    repair_chapter,
    stripped_chapter,
    write_chapter_file,
)
from translation_bot.sanitize import (
    find_leaks, korean_fraction, remove_snippets, strip_export_footer, strip_reasoning,
    strip_source_header,
)
from translation_bot.state import State
from translation_bot.text_source import split_text_into_chapters
from translation_bot.translator import (
    RateLimitedError,
    StreamHooks,
    TranslationAborted,
    Translator,
    TranslatorError,
)
from translation_bot.validate import validate_translation

from . import console
from . import errors
from .locks import file_lock
from . import ocr_build
from . import pages as pages_mod
from . import projects as pj
from . import variants as variants_mod
from .bulk import MAX_ROWS, MAX_UNTYPED, prepare_bulk_rows, split_flat

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


# Every error leaves through one of these two handlers, so the frontend always receives
# `detail` as the SAME object shape (see server/errors.Explained) and the terminal gets
# one compact line instead of an uncaught-500 traceback dump.
@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    e = errors.from_http_detail(exc.detail, exc.status_code)
    return JSONResponse(status_code=exc.status_code, content={"detail": errors.as_dict(e)},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    e = errors.explain(exc)
    errors.log_error(e, where=request.url.path)
    return JSONResponse(status_code=e.status, content={"detail": errors.as_dict(e)})


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

# One lock per file, from the shared registry in server.locks — the SAME registry
# server.pages and server.variants use, so two modules locking the same path can
# never end up holding two different locks. Kept under the old name because every
# state.json writer already calls it.
_state_lock = file_lock


@contextmanager
def mutate_state(path: str | Path):
    """Load → mutate → save state.json with no other thread interleaving.

    An unguarded load/mutate/save is a read-modify-write race: two threads each read the
    file, each apply their own change to their own copy, and whichever saves last silently
    discards the other's work — a chapter the worker just finished, erased because the
    user pressed Accept on a different one at the same moment. The atomic write alone
    can't prevent that; it makes each save all-or-nothing, not the pair of them ordered.

    Yields a FRESHLY LOADED State: anything read before the lock was taken is already
    stale by definition, so mutate what is yielded here, not an older copy.

    Keep the body short — it holds off the translation worker. Do any validating or
    fetching before entering, not inside.
    """
    with _state_lock(path):
        state = State.load(path)
        yield state
        state.save(path)


# ----------------------------------------------------------------------------- helpers
def load_global_config() -> Config:
    if not CONFIG_PATH.exists():
        raise HTTPException(400, "config.toml not found. Complete setup first.")
    try:
        return Config.load(CONFIG_PATH)
    except Exception as exc:
        # Malformed/hand-edited config.toml — surface a clean 400 instead of a 500
        # so the UI can prompt a fix rather than appearing broken.
        raise HTTPException(400, f"config.toml is invalid: {exc}") from exc


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
        if project.get("source_type") in ("text", "images"):
            # Pasted/uploaded text, or chapters built from transcribed page images —
            # read from the stored source, no network. This one branch is the whole
            # integration: from here on an image novel is an ordinary novel.
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
        # MUST be _output_total, not len(): on the offline fallback above, the cache
        # can hold FEWER chapters than the novel really has (a state-only rebuild).
        # Passing the short count re-padded chapter-001.md down to chapter-01.md while
        # every reader still looked for the 3-digit name — a novel's finished
        # translations would vanish while state.json still called them validated, and
        # re-translating them re-billed the whole book.
        _normalize_chapter_padding(pid, _output_total(pid, _chapter_cache[pid]))
    return _chapter_cache[pid]


def _normalize_chapter_padding(pid: str, total: int) -> None:
    """Keep chapter-NN.md filenames at the width the current chapter count implies.

    The pad width is derived from the total (``max(2, len(str(total)))``), so a
    source doc that grows past a digit boundary — 96 tabs to 100 — silently
    orphans every existing translation: the app starts looking for
    ``chapter-001.md`` while the files on disk are still ``chapter-01.md``, and
    a fully translated novel reads as untranslated over its Korean source.
    Re-pad on load so the library heals itself instead. Idempotent, and it
    never overwrites a name that is already taken.

    ``variants/`` is included because per-paragraph history is named from the same
    ``chapter_filename`` stem. It was left out originally, so a novel crossing 99 to
    100 chapters silently orphaned every rewrite the reader had kept: the app looked
    for ``chapter-007.json`` while the file on disk was still ``chapter-07.json``."""
    if total <= 0:
        return
    width = max(2, len(str(total)))
    base = pj.PROJECTS_DIR / pid
    for sub, ext in (("chapters", "md"), ("previous", "md"), ("audit", "md"),
                     ("variants", "json")):
        d = base / sub
        if not d.is_dir():
            continue
        try:
            stale = list(d.glob(f"chapter-*.{ext}"))
        except OSError:
            continue
        for f in stale:
            tail = f.stem.split("-", 1)[-1]
            if not tail.isdigit() or len(tail) == width:
                continue
            dest = d / f"chapter-{int(tail):0{width}d}.{ext}"
            if dest.exists():
                continue  # a newer translation already owns the canonical name
            try:
                f.rename(dest)
            except OSError:
                pass  # never block reading a novel on a rename


def _safe_read(path: Path) -> str | None:
    """Read a saved chapter/translation file, returning None instead of raising on a
    transient FS error, a file removed mid-request (a concurrent re-translate), or a
    non-UTF-8 stray file — so a single bad file never 500s the reader/search."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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
        "number": strip_source_header(ch.text)[1],  # real chapter number from the source header
        "language": lang,
        "paragraphs": m.paragraph_count,
        "dialogue": m.dialogue_count,
        "chars": m.char_count,
        "status": status_val,
        "cost_usd": rec.get("cost_usd", 0.0),
        # Recorded per chapter by State.add_usage since forever; surfaced in the UI now.
        "usage": rec.get("usage", {}),
        "failures": rec.get("failures", []),
        # The KINDS of problem flagged (e.g. ["pronoun"]), so lists can badge and filter
        # by what is actually wrong instead of re-parsing failure sentences client-side.
        "flags": failure_flags(rec.get("failures", [])),
        "has_output": (cfg.paths.output_dir / chapter_filename(ch.index, total)).exists(),
    }


# ----------------------------------------------------------------------------- status / settings
@app.get("/api/status")
def status() -> dict:
    cfg_exists = CONFIG_PATH.exists()
    cfg = None
    config_error = None
    if cfg_exists:
        try:
            cfg = Config.load(CONFIG_PATH)
        except Exception as exc:
            # A broken config must not 500 the very first call the UI makes, or the
            # whole app (including the setup screen meant to fix it) appears dead.
            config_error = str(exc)
    creds_file = cfg.google.credentials_file if cfg else Path("client_secret.json")
    token_file = cfg.google.token_file if cfg else Path("token.json")
    return {
        "config_present": cfg_exists,
        "config_error": config_error,
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
        text = setkey(text, "model", s.model, section="anthropic")
    if s.effort is not None:
        text = setkey(text, "effort", s.effort, section="anthropic")
    if s.deep_check is not None:
        text = setkey(text, "deep_check", s.deep_check, section="translation")
    # Atomic: a half-written config.toml makes load_global_config raise, and every
    # request after that answers 400 — the app becomes unusable until the file is
    # repaired by hand. This is the one file atomic_write_text exists for that wasn't
    # using it.
    atomic_write_text(CONFIG_PATH, text)
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
        "tokens": state.totals().get("tokens", {}),
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


# --- turning the cryptic validation failures into human guidance + an action ------

# A pronoun failure as validate.py writes it. Chapters validated before the conflict
# was stored structurally only have this sentence, so parsing it back is what lets the
# repair work on records already on disk — no state.json migration needed.
_PRONOUN_FAILURE_RE = re.compile(
    r"^(?P<name>.+?) is tagged '(?P<expected>he|she|they)' in the glossary "
    r"but the chapter uses the opposite pronoun (?P<hits>\d+)x$")

# What the chapter wrongly called them, for the plain-English explanation.
_OPPOSITE_OF = {"he": "she/her", "she": "he/him", "they": "he/him or she/her"}


def pronoun_conflicts(rec: dict | None) -> list[dict]:
    """The mis-gendered characters in a chapter record, as ``{name, expected, hits}``.

    Prefers the structured list written by the validator; falls back to parsing the
    failure sentences so chapters flagged before that existed are still repairable.
    """
    rec = rec or {}
    stored = rec.get("pronoun_conflicts")
    if isinstance(stored, list) and stored:
        return [c for c in stored if isinstance(c, dict) and c.get("name")]
    out: list[dict] = []
    for f in rec.get("failures") or []:
        m = _PRONOUN_FAILURE_RE.match(str(f).strip())
        if m:
            out.append({"name": m["name"], "expected": m["expected"],
                        "hits": int(m["hits"])})
    return out


def glossary_for(cfg: Config, chapter: Chapter) -> list:
    """The glossary entries the validator should judge this chapter against.

    Matches what the pipeline passes during translation (``relevant_to``), so the
    scan path reaches the same verdict rather than a stricter or looser one."""
    try:
        return Glossary.load(cfg.paths.glossary_json).relevant_to(chapter.text)
    except Exception:  # noqa: BLE001 — a broken glossary must never 500 a scan
        return []


def diagnose(failures: list[str], metrics: dict | None = None) -> list[dict]:
    """Map each raw validation failure to a plain-language explanation + a suggested
    action (autofix | ai_resolve | fix_pronouns | retranslate | accept) the UI can act on."""
    out: list[dict] = []
    for f in failures or []:
        fl = f.lower()
        pron = _PRONOUN_FAILURE_RE.match(str(f).strip())
        if pron:
            # Mis-gendering is its own problem with its own repair: the glossary already
            # knows the right pronoun, so rewriting them beats re-translating the chapter.
            wrong = _OPPOSITE_OF.get(pron["expected"], "the opposite pronoun")
            times = int(pron["hits"])
            out.append({"message": f"{pron['name']} is {pron['expected']} in your glossary, "
                                   f"but this chapter refers to them as {wrong} "
                                   f"{times} time{'' if times == 1 else 's'}.",
                        "kind": "pronoun", "action": "fix_pronouns"})
        elif "leaked" in fl:
            out.append({"message": "The AI left some of its own notes/reasoning in the text.",
                        "kind": "leak", "action": "autofix"})
        elif "untranslated korean" in fl:
            out.append({"message": "Some Korean was left untranslated.",
                        "kind": "korean", "action": "autofix"})
        elif "length ratio" in fl and "below" in fl:
            out.append({"message": "The translation looks shorter than the original — a passage may have been skipped or summarized.",
                        "kind": "omission", "action": "ai_resolve"})
        elif "length ratio" in fl and "above" in fl:
            out.append({"message": "The translation looks longer than the original — content may have been added or over-expanded.",
                        "kind": "embellishment", "action": "ai_resolve"})
        elif "paragraph count" in fl:
            out.append({"message": "The paragraph structure drifted from the source (paragraphs merged, split, or dropped).",
                        "kind": "structure", "action": "ai_resolve"})
        elif "dialogue" in fl:
            out.append({"message": "The number of dialogue lines differs from the source — usually minor.",
                        "kind": "dialogue", "action": "accept"})
        else:
            out.append({"message": f, "kind": "other", "action": "ai_resolve"})
    return out


def failure_flags(failures: list[str]) -> list[str]:
    """The distinct problem kinds in a failure list, in first-seen order."""
    seen: list[str] = []
    for d in diagnose(failures):
        if d["kind"] not in seen:
            seen.append(d["kind"])
    return seen


def _corrective_instruction(failures: list[str], conflicts: list[dict] | None = None) -> str:
    """Build a failure-targeted correction to feed the AI re-translation."""
    joined = " ".join(failures or []).lower()
    parts: list[str] = []
    # Gender first: it is the most common flag, and the one the model cannot recover
    # from on its own — Korean drops the subject, so without being told outright it
    # just re-guesses and reproduces the same mistake.
    for c in conflicts or []:
        forms = Translator._PRONOUN_FORMS.get(c.get("expected", ""))
        if not forms or not c.get("name"):
            continue
        parts.append(
            f"GENDER: {c['name']} is {c['expected']}. Use {forms} for EVERY reference to "
            f"{c['name']}, in narration and in dialogue, for the whole chapter. The "
            f"previous attempt used the opposite pronoun {c.get('hits', 0)} time(s). "
            f"Never contradict this.")
    if "length ratio" in joined and "below" in joined:
        parts.append("The previous attempt was too SHORT — it likely omitted or summarized content. "
                     "Translate the chapter COMPLETELY: render every sentence and detail, omit nothing, condense nothing.")
    if "length ratio" in joined and "above" in joined:
        parts.append("The previous attempt was too LONG — it likely added or over-expanded content. "
                     "Translate faithfully and tightly: do not add, embellish, or pad anything not in the source.")
    if "paragraph count" in joined:
        parts.append("Match the source's paragraph structure exactly — keep the same paragraph breaks; do not merge or split paragraphs.")
    if "untranslated korean" in joined:
        parts.append("Translate ALL Korean into English — leave no Korean in the output except intentional sound effects.")
    if "leaked" in joined:
        parts.append("Output ONLY the finished translation — no notes, reasoning, or meta-commentary of any kind.")
    if not parts:
        parts.append("The previous attempt failed an automated fidelity check. Translate completely and faithfully — "
                     "omit nothing, add nothing, and match the source structure.")
    return "CORRECTION REQUIRED. " + " ".join(parts)


@app.get("/api/review")
def review_inbox() -> dict:
    """Every chapter flagged needs-review or failed, across all novels — assembled
    purely from saved state (no network, no source needed)."""
    flagged = {"needs-review", "failed"}
    items = []
    for project in pj.list_projects():
        try:
            cfg = pj.project_config(load_global_config(), project)
            state = State.load(cfg.paths.state_file)
        except Exception:  # noqa: BLE001 — one broken project never breaks the inbox
            continue
        chapters = state.chapters if isinstance(state.chapters, dict) else {}
        for idx_str, rec in chapters.items():
            if not isinstance(rec, dict) or rec.get("status") not in flagged:
                continue
            try:
                index = int(idx_str)
            except (TypeError, ValueError):
                continue
            val = rec.get("validation") or {}
            failures = rec.get("failures", [])
            items.append({
                "project_id": project["id"],
                "project_name": project.get("name", "Untitled novel"),
                "index": index,
                "title": rec.get("title") or f"Chapter {index}",
                "status": rec.get("status"),
                "failures": failures,
                "diagnosis": diagnose(failures, val),
                "flags": failure_flags(failures),
                "length_ratio": val.get("length_ratio"),
            })
    items.sort(key=lambda r: (r["project_name"].lower(), r["index"]))
    return {"items": items}


# ----------------------------------------------------------------------------- backup / move
def _unlink_quietly(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass  # a leftover temp file is harmless; a failed download is not


def _bundle_response(path: Path, filename: str) -> Response:
    """Stream a bundle from disk, removing the temp file once it has been sent."""
    return FileResponse(path, media_type="application/zip", filename=filename,
                        background=BackgroundTask(_unlink_quietly, path))


@app.get("/api/backup")
def export_all_bundle(images: bool = False) -> Response:
    """Download every novel as one portable .zip (a full library backup).

    Page images are left out unless asked for: a photographed library runs to
    gigabytes, and the extracted text (which is what makes a novel readable and
    translatable) always travels regardless.
    """
    pids = [p["id"] for p in pj.list_projects()]
    if not pids:
        raise HTTPException(400, "No novels to back up yet.")
    path = pj.export_bundle(pids, include_images=images)
    return _bundle_response(path, "night-reader-backup.zip")


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
        except FileNotFoundError as exc:
            # Missing OAuth client secret — point at setup.
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            msg = str(exc)
            if "invalid_grant" in msg or "RefreshError" in type(exc).__name__ or "renewing" in msg:
                raise HTTPException(
                    400,
                    "Your Google sign-in has expired or was revoked. Click "
                    "“Connect Google” / Login to reconnect, then try the link again.",
                ) from exc
            # Don't leak raw internals; give a friendly, actionable message.
            raise HTTPException(
                400,
                "Couldn't open that document. Check the link is a Google Doc you can "
                "access, then make sure Google is connected in setup.",
            ) from exc
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


class CreateImagesProject(BaseModel):
    name: str = ""


@app.get("/api/scans")
def scans_overview() -> dict:
    """Every photographed novel and how much of it still wants attention.

    Cheap: one small JSON read per image novel, the same shape the Review inbox uses.
    """
    novels = []
    for project in pj.list_projects():
        if project.get("source_type") != "images" or project.get("archived"):
            continue
        doc = pages_mod.load_pages(project["id"])
        novels.append({
            "id": project["id"],
            "name": project.get("name", "Untitled novel"),
            "counts": pages_mod.counts(doc),
            "built": bool(doc.get("build")),
            "chapter_count": project.get("chapter_count", 0),
            "cost_usd": (doc.get("totals") or {}).get("cost_usd", 0.0),
        })
    novels.sort(key=lambda n: (-(n["counts"]["needs-check"] + n["counts"]["new"]), n["name"]))
    return {"novels": novels}


@app.post("/api/projects/images")
def create_images_project(body: CreateImagesProject) -> dict:
    """Create an empty novel whose source is photographed or scanned pages.

    It has no chapters yet: pages are uploaded and transcribed first, then built.
    """
    project = pj.create_images_project(body.name or "Untitled novel")
    _chapter_cache[project["id"]] = []
    return project_summary(project)


# ----------------------------------------------------------------------------- scanned pages
# NOTE ON ROUTE ORDER: every literal path below (/pages/reorder, /pages/build, ...)
# MUST stay declared before /pages/{page_id}, or FastAPI captures "reorder" as a
# page id and the action silently 404s.

def require_images_project(pid: str) -> dict:
    project = require_project(pid)
    if project.get("source_type") != "images":
        raise HTTPException(400, "That novel doesn't use page images.")
    return project


def _page_by_id(pid: str, page_id: str) -> tuple[dict, dict]:
    doc = pages_mod.load_pages(pid)
    page = pages_mod.find_page(doc, page_id)
    if page is None:
        raise HTTPException(404, "That page isn't in this novel.")
    return doc, page


@app.post("/api/projects/{pid}/pages")
async def upload_page(pid: str, request: Request, batch: str = "",
                      label: str = "", name: str = "") -> dict:
    """Add one page image, sent as the raw request body.

    One image per request rather than a multipart batch: it keeps the app free of the
    python-multipart dependency (the same reasoning as /api/import), gives per-file
    progress for free, and means one unreadable photo fails on its own instead of
    taking a sixty-file drop down with it.
    """
    require_images_project(pid)

    # Read incrementally so an oversized upload is refused before it is all resident.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > pages_mod.MAX_IMAGE_BYTES:
            raise HTTPException(
                413, f"That image is bigger than "
                     f"{pages_mod.MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(400, "No image was uploaded.")

    # The BYTES decide what this is — the Content-Type header is only advisory, and
    # the client's filename is never trusted for anything but a display label.
    ext = pages_mod.sniff_image(data[:32])
    if ext is None:
        raise HTTPException(400, pages_mod.unsupported_reason(data[:32]))
    digest = pages_mod.sha256_of(data)

    with pages_mod.mutate_pages(pid) as doc:
        existing = pages_mod.find_by_hash(doc, digest)
        if existing is not None:
            # Re-dropping the same folder is a no-op, not a doubled novel.
            return {"page": existing, "duplicate": True,
                    "total": len(doc.get("pages", []))}
        if len(doc.get("pages", [])) >= pages_mod.MAX_PAGES_PER_PROJECT:
            raise HTTPException(
                400, f"This novel already has {pages_mod.MAX_PAGES_PER_PROJECT} pages.")

        known = {b.get("id") for b in doc.get("batches", [])}
        batch_id = batch if (batch and batch in known) else pages_mod.new_batch(doc, label)

        page = pages_mod.add_page(doc, ext=ext, data_len=len(data), digest=digest,
                                  batch=batch_id, name=name)
        target = pages_mod.pages_dir(pid) / page["file"]
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            # Never leave a manifest entry pointing at a file that isn't there.
            doc["pages"].remove(page)
            raise HTTPException(500, f"Couldn't save that image: {exc}")
        return {"page": dict(page), "duplicate": False, "batch": batch_id,
                "total": len(doc["pages"])}


@app.get("/api/projects/{pid}/pages")
def list_pages(pid: str) -> dict:
    """The page rail's payload. Page text is omitted — a few hundred pages of Korean
    would be several megabytes on every tab switch."""
    require_project(pid)
    return pages_mod.summary(pages_mod.load_pages(pid))


class ReorderPages(BaseModel):
    ids: list[str]


@app.post("/api/projects/{pid}/pages/reorder")
def reorder_pages(pid: str, body: ReorderPages) -> dict:
    require_images_project(pid)
    with pages_mod.mutate_pages(pid) as doc:
        if not pages_mod.reorder(doc, body.ids):
            raise HTTPException(400, "That page order doesn't match this novel's pages.")
    return {"ok": True}


class DeletePages(BaseModel):
    ids: list[str]


@app.post("/api/projects/{pid}/pages/delete")
def delete_pages(pid: str, body: DeletePages) -> dict:
    require_images_project(pid)
    with pages_mod.mutate_pages(pid) as doc:
        removed = pages_mod.delete_pages(doc, body.ids)
    folder = pages_mod.pages_dir(pid)
    for page in removed:
        try:
            (folder / str(page.get("file") or "")).unlink(missing_ok=True)
        except OSError:
            pass  # the manifest entry is gone; a stray file is harmless
    return {"ok": True, "removed": len(removed)}


class PageWork(BaseModel):
    ids: list[str] = Field(default_factory=list)
    only_new: bool = False


def _release_queued_pages(pid: str, seqs: set[int] | None = None) -> int:
    """Put pages back to "not read yet" when their queued work will never run.

    A page is marked ``queued`` when work is accepted for it. If that work is then
    dropped — Stop, a rate-limit give-up, or a worker that died — nothing used to
    reset it, and the default sweep only selects ``new``/``failed``. The page showed
    "Queued" forever and "Read N pages" answered *"There are no pages to do that to"*,
    leaving hand-selecting every stranded page as the only way out.

    Reads before opening the manifest for writing, so a novel with no pages at all
    never gets a ``pages.json`` created as a side effect of pressing Stop.
    """
    doc = pages_mod.load_pages(pid)
    stranded = [p for p in doc.get("pages", [])
                if p.get("status") == pages_mod.STATUS_QUEUED
                and (seqs is None or int(p.get("seq") or -1) in seqs)]
    if not stranded:
        return 0
    wanted = {p.get("id") for p in stranded}
    with pages_mod.mutate_pages(pid) as live:
        for rec in live.get("pages", []):
            if rec.get("id") in wanted:
                rec["status"] = pages_mod.STATUS_NEW
    return len(stranded)


def _queue_page_work(pid: str, body: PageWork, kind: str) -> dict:
    project = require_images_project(pid)
    cfg = pj.project_config(load_global_config(), project)
    doc = pages_mod.load_pages(pid)

    if body.ids:
        wanted = [p for p in doc.get("pages", []) if p.get("id") in set(body.ids)]
    elif kind == TASK_OCR:
        wanted = [p for p in doc.get("pages", [])
                  if p.get("status") in (pages_mod.STATUS_NEW, pages_mod.STATUS_FAILED)]
    else:
        # Verifying every page of a long novel roughly doubles the spend, so the
        # default sweep only re-reads the pages the model was unsure about.
        wanted = [p for p in doc.get("pages", [])
                  if p.get("status") == pages_mod.STATUS_NEEDS_CHECK]

    wanted = [p for p in wanted if p.get("status") != pages_mod.STATUS_SKIPPED]
    if not wanted:
        raise HTTPException(400, "There are no pages to do that to.")

    items = [(int(p["seq"]), True, kind) for p in wanted]
    result = _enqueue_task(pid, cfg, items)
    accepted = {int(i) for i in (result.get("queued") or [])}

    # Mark ONLY what the queue actually took. Marking everything up front stranded
    # every page the dedup rejected: it read "Queued" forever while nothing was
    # coming for it, and the default sweep skips that status.
    if accepted:
        with pages_mod.mutate_pages(pid) as live:
            for page in wanted:
                if int(page.get("seq") or -1) in accepted:
                    rec = pages_mod.find_page(live, page["id"])
                    if rec is not None:
                        rec["status"] = pages_mod.STATUS_QUEUED
    elif wanted:
        # One operation per page at a time is deliberate (it stops two model calls
        # racing on one page), but silently answering 200 with an empty queue looked
        # like the button was broken.
        raise HTTPException(
            409, "Those pages are already queued or being read. Wait for the current "
                 "run to finish, or press Stop first.")

    result["skipped_busy"] = len(wanted) - len(accepted)
    return result


@app.post("/api/projects/{pid}/pages/ocr")
async def run_page_ocr(pid: str, body: PageWork) -> dict:
    """Read the Korean out of the selected pages (or every unread page)."""
    return _queue_page_work(pid, body, TASK_OCR)


@app.post("/api/projects/{pid}/pages/verify")
async def run_page_verify(pid: str, body: PageWork) -> dict:
    """Proof-read the selected pages against their photos (or every flagged page)."""
    return _queue_page_work(pid, body, TASK_OCR_VERIFY)


class StitchRequest(BaseModel):
    use_model: bool = True
    max_model_seams: int = 25


@app.post("/api/projects/{pid}/pages/stitch")
async def stitch_pages(pid: str, body: StitchRequest) -> dict:
    """Work out how each page joins to the one before it.

    Cheapest first: deterministic rules settle most seams for free, and only the
    genuinely ambiguous ones are batched into a single text-only model call. A seam
    the reader has decided by hand is never revisited.
    """
    project = require_images_project(pid)
    cfg = pj.project_config(load_global_config(), project)
    doc = pages_mod.load_pages(pid)
    pages = [p for p in doc.get("pages", [])
             if p.get("status") != pages_mod.STATUS_SKIPPED]

    decided: dict[str, dict] = {}
    unsure: list[tuple[str, str, str]] = []
    for prev, cur in zip(pages, pages[1:]):
        if cur.get("join_prev_source") == "user":
            continue  # the reader's own decision stands
        join = propose_join(prev.get("text") or "", cur.get("text") or "", prev, cur)
        decided[cur["id"]] = {"join_prev": join.kind, "join_glue": join.glue,
                              "join_prev_source": "auto", "join_reason": join.reason}
        if join.needs_model:
            unsure.append((cur["id"], prev.get("text") or "", cur.get("text") or ""))

    usage, cost, asked = {}, 0.0, 0
    if body.use_model and unsure:
        batch = unsure[:max(1, body.max_model_seams)]
        asked = len(batch)
        translator = Translator(cfg.anthropic, cfg.translation)
        decisions, usage, cost = await run_in_threadpool(
            ocr.stitch_boundaries, translator, [(a, b) for _id, a, b in batch])
        for decision in decisions:
            page_id = batch[decision.i - 1][0]
            decided[page_id] = {"join_prev": decision.join, "join_glue": decision.glue,
                                "join_prev_source": "model",
                                "join_reason": decision.note or "decided by reading both pages"}

    with pages_mod.mutate_pages(pid) as live:
        for page_id, fields in decided.items():
            rec = pages_mod.find_page(live, page_id)
            if rec is not None:
                rec.update(fields)
        totals = live.setdefault("totals", {})
        totals["cost_usd"] = round(float(totals.get("cost_usd") or 0.0) + cost, 6)

    gaps = sum(1 for f in decided.values() if f["join_prev"] == "gap")
    return {"ok": True, "seams": len(decided), "asked_model": asked,
            "gaps": gaps, "cost_usd": cost, "usage": usage,
            "pages": pages_mod.summary(pages_mod.load_pages(pid))}


class BuildChapters(BaseModel):
    mode: str = "batch"           # batch | heading | separator | single
    separator: str = "---"
    include: str = "approved"     # approved | all
    append: bool = True
    force: bool = False


@app.post("/api/projects/{pid}/pages/build")
def build_pages_into_chapters(pid: str, body: BuildChapters) -> dict:
    """Turn the transcribed pages into chapters (source.json).

    After this the novel is ordinary: translate, validate, read and export all work
    unchanged. Appending is the default so photographing chapter 13 never renumbers
    chapters 1-12 or orphans their finished translations.
    """
    project = require_images_project(pid)
    cfg = pj.project_config(load_global_config(), project)
    doc = pages_mod.load_pages(pid)

    existing = pj.load_text_chapters(pid)
    state = State.load(cfg.paths.state_file)
    if body.append:
        built_ids = set((doc.get("build") or {}).get("used_page_ids") or [])
        if built_ids:
            doc = {**doc, "pages": [p for p in doc.get("pages", [])
                                    if p.get("id") not in built_ids]}
        start_index = len(existing) + 1
    else:
        finished = [i for i in range(1, len(existing) + 1)
                    if (state.get(i) or {}).get("status") in state_mod.DONE_STATUSES]
        if finished and not body.force:
            raise HTTPException(
                400, f"Rebuilding would renumber {len(finished)} chapter(s) you've "
                     f"already translated. Add new pages as an append instead, or "
                     f"confirm the rebuild to redo them.")
        start_index = 1

    try:
        chapters, warnings, page_map = ocr_build.build_chapters(
            doc, mode=body.mode, separator=body.separator,
            include=body.include, start_index=start_index)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if not chapters:
        raise HTTPException(
            400, "No pages are ready to build. Read the pages first, then accept the "
                 "ones that look right.")

    combined = (existing[:start_index - 1] + chapters) if body.append else chapters
    for i, chapter in enumerate(combined, start=1):
        chapter.index = i

    pj.cache_source(pid, combined)
    project["chapter_count"] = len(combined)
    pj._atomic_write_json(pj.PROJECTS_DIR / pid / "project.json", project)
    # A stale cache would translate the PREVIOUS build's chapters.
    _chapter_cache.pop(pid, None)
    _normalize_chapter_padding(pid, len(combined))
    _invalidate_consistency(pid)

    used = [p.get("id") for p in ocr_build.usable_pages(doc, include=body.include)]
    with pages_mod.mutate_pages(pid) as live:
        prior = set((live.get("build") or {}).get("used_page_ids") or []) if body.append else set()
        live["build"] = {"mode": body.mode, "at": pages_mod.now_iso(),
                         "chapters": len(combined), "warnings": warnings,
                         "page_map": page_map,
                         "used_page_ids": sorted(prior | set(used))}

    return {"ok": True, "chapters": len(combined), "added": len(chapters),
            "warnings": warnings, "project": project_summary(project)}


@app.get("/api/projects/{pid}/pages/{page_id}")
def get_page(pid: str, page_id: str) -> dict:
    require_project(pid)
    _doc, page = _page_by_id(pid, page_id)
    return page


@app.get("/api/projects/{pid}/pages/{page_id}/image")
def get_page_image(pid: str, page_id: str) -> Response:
    """Serve one page photo.

    The path comes from the manifest, never from the URL — a page id is only ever a
    lookup key. File bytes are never rewritten, so the response is immutable.
    """
    require_project(pid)
    path = pages_mod.resolve_page_file(pid, page_id)
    if path is None:
        raise HTTPException(404, "That page image isn't here.")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


class PageUpdate(BaseModel):
    text: str | None = None
    status: str | None = None
    join_prev: str | None = None
    join_glue: str | None = None
    heading: str | None = None
    hint: str | None = None


@app.post("/api/projects/{pid}/pages/{page_id}")
def update_page(pid: str, page_id: str, body: PageUpdate) -> dict:
    """Save a reader's corrections to one page."""
    require_images_project(pid)
    _page_by_id(pid, page_id)

    if body.status is not None and body.status not in pages_mod.STATUSES:
        raise HTTPException(400, "Unknown page status.")
    if body.join_prev is not None and body.join_prev not in pages_mod.JOIN_KINDS:
        raise HTTPException(400, "Unknown page join.")

    with pages_mod.mutate_pages(pid) as doc:
        rec = pages_mod.find_page(doc, page_id)
        if rec is None:
            raise HTTPException(404, "That page isn't in this novel.")
        if body.text is not None:
            rec["text"] = body.text
            rec["chars"] = len(body.text)
            rec["hangul_fraction"] = round(hangul_fraction(body.text), 3)
            # A hand-edited page is the reader's word, so it stops being "needs check"
            # unless they explicitly said otherwise in the same request.
            if body.status is None:
                rec["status"] = pages_mod.STATUS_EDITED
        if body.status is not None:
            rec["status"] = body.status
        if body.join_prev is not None:
            rec["join_prev"] = body.join_prev
            rec["join_prev_source"] = "user"  # never overwritten by a later re-run
            rec["join_reason"] = "set by you"
        if body.join_glue is not None:
            rec["join_glue"] = body.join_glue
        if body.heading is not None:
            rec["heading"] = body.heading.strip() or None
        if body.hint is not None:
            rec["hint"] = body.hint[:500]
        updated = dict(rec)
    return updated


class ProjectUpdate(BaseModel):
    name: str | None = None
    style_note: str | None = None
    instructions: str | None = None
    honorific_note: str | None = None
    archived: bool | None = None


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
        text = _safe_read(path)
        if text is None:
            continue
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
        text = _safe_read(path)
        if text is not None:
            out.append((ch.index, ch.title, text))
    return out


_CHAPTER_FILE_RE = re.compile(r"^chapter-(\d+)\.md$")


def _translated_chapters_local(cfg: Config) -> list[tuple[int, str, str]]:
    """Same as ``_translated_chapters`` but from DISK ONLY — no source document fetch.

    ``_translated_chapters`` goes through ``get_chapters``, which on a cold cache fetches
    the live Google Doc (``includeTabsContent=True``) purely to enumerate indices and
    titles. That's a multi-second network round trip per novel, so a library-wide sweep
    across dozens of novels took minutes and hammered the Docs API for data it then threw
    away — the text being scanned is the English output, which is already on disk.

    Titles come from ``state.json`` (recorded there when each chapter was written), so
    this needs no network and works offline.
    """
    out_dir = cfg.paths.output_dir
    try:
        names = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    except OSError:
        return []
    state = State.load(cfg.paths.state_file)
    out: list[tuple[int, str, str]] = []
    for name in names:
        m = _CHAPTER_FILE_RE.match(name)
        if not m:
            continue
        idx = int(m.group(1))
        text = _safe_read(out_dir / name)
        if text is None:
            continue
        title = (state.get(idx) or {}).get("title") or f"Chapter {idx}"
        out.append((idx, title, text))
    out.sort(key=lambda r: r[0])
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
def export_project_bundle(pid: str, images: bool = False) -> Response:
    """Download this one novel as a portable .zip (chapters, glossary, progress,
    and the cached source) — move it to another device or keep it as a backup.

    ``images=1`` also packs the original page photos of a scanned novel, so the
    copy can be re-read and re-checked against its sources on the other device.
    """
    project = require_project(pid)
    path = pj.export_bundle([pid], include_images=images)
    name = _safe_name(project.get("name", "novel"))
    return _bundle_response(path, f"{name}.novel.zip")


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
        # Atomic write so a concurrent reader / crash can't truncate project.json
        # (a corrupt project.json would otherwise hide the whole novel).
        pj._atomic_write_json(pj.PROJECTS_DIR / pid / "project.json", project)
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
    translation = _safe_read(out_path) if out_path.exists() else None
    if translation is None:
        # A needs-review chapter's translation lives only in audit/ — surface it so the
        # chapter is readable and reviewable instead of appearing untranslated.
        translation = read_audit_translation(cfg.paths.audit_dir, index, total)
    rec = State.load(cfg.paths.state_file).get(index) or {}
    # Show the Korean AS THE MODEL SAW IT — export header (incl. the repeated chapter
    # number) and closing copyright notice removed — so the side-by-side view lines up.
    clean_source, number = strip_source_header(ch.text)
    clean_source = strip_export_footer(clean_source)
    return {
        "index": index,
        "title": ch.title,
        "number": number,
        "language": classify(ch, cfg),
        "source": clean_source,
        "translation": translation,
        "status": rec.get("status", "pending"),
        "validation": rec.get("validation"),
        "failures": rec.get("failures", []),
        "diagnosis": diagnose(rec.get("failures", []), rec.get("validation")),
        "flags": failure_flags(rec.get("failures", [])),
        "manual_edit": rec.get("manual_edit", False),
        "has_previous": previous_chapter_path(cfg.paths.output_dir, index, total).exists(),
        "offline": pid in _offline_projects,
        # For a scanned novel, the photos this chapter was built from — the truest
        # source there is, and what makes a translation traceable back to the page.
        # Only batch builds attribute pages to a specific chapter; a whole-novel
        # split genuinely can't, so it returns nothing rather than guessing.
        "page_ids": _chapter_page_ids(project, index),
    }


def _chapter_page_ids(project: dict, index: int) -> list[str]:
    if project.get("source_type") != "images":
        return []
    build = pages_mod.load_pages(project["id"]).get("build") or {}
    return list((build.get("page_map") or {}).get(str(index)) or [])


@app.get("/api/projects/{pid}/chapters/{index}/previous")
def chapter_previous(pid: str, index: int) -> dict:
    """The retained prior translation (kept when a re-translate/edit overwrites it),
    so the reader can show old-vs-new and offer a one-click revert."""
    project, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    prev = previous_chapter_path(cfg.paths.output_dir, index, total)
    if not prev.exists():
        raise HTTPException(404, "no previous version")
    return {"index": index, "translation": prev.read_text(encoding="utf-8")}


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
    with mutate_state(cfg.paths.state_file) as state:
        state.update(
            index,
            status=state_mod.STATUS_VALIDATED,
            title=ch.title,
            source_hash=ch.metrics.content_hash,
            failures=[],
            manual_edit=True,
        )
    return {"ok": True, "status": state_mod.STATUS_VALIDATED}


def _chapter_problems(pid: str, cfg: Config, index: int) -> dict:
    """Scan a chapter's saved translation for problems: leaked AI reasoning,
    untranslated Korean, and the length/structure validation checks."""
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)  # match the on-disk chapter-NN.md pad width
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
    # Pass the glossary: without it the pronoun check is skipped entirely, so "Check
    # chapter" reported a mis-gendered chapter as clean — and Fix automatically could
    # re-mark it validated, silently clearing the flag.
    # Measure against the source WITHOUT the export header/footer — the same text the
    # pipeline validated against, so "Check chapter" can't disagree with the run that
    # produced the chapter (see pipeline.stripped_chapter).
    val = validate_translation(stripped_chapter(ch), text, cfg.validation, glossary_for(cfg, ch))
    for f in val.failures:
        if "leaked" in f or "untranslated Korean" in f:
            continue  # already reported above with a fix path
        if _PRONOUN_FAILURE_RE.match(f.strip()):
            problems.append({"type": "pronoun", "severity": "high", "auto_fixable": False,
                             "message": diagnose([f])[0]["message"]})
            continue
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
    total = _output_total(pid, chapters)  # match the on-disk chapter-NN.md pad width
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
    total = _output_total(pid, chapters)  # match the on-disk chapter-NN.md pad width
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
        val = validate_translation(stripped_chapter(ch), cleaned, cfg.validation, glossary_for(cfg, ch))
        with mutate_state(cfg.paths.state_file) as state:
            state.update(index, title=ch.title,
                         status=state_mod.STATUS_VALIDATED if val.ok else state_mod.STATUS_NEEDS_REVIEW,
                         failures=val.failures, manual_edit=True)
    return {"fixed": fixed, "removed": len(removed) + snip_removed, **_chapter_problems(pid, cfg, index)}


@app.post("/api/projects/{pid}/chapters/{index}/resolve")
async def resolve_chapter(pid: str, index: int) -> dict:
    """AI resolve: re-translate a flagged chapter with a correction targeting its exact
    failures, always writing the result (the prior version is kept in previous/ so the
    reader can compare and revert). Uses your plan.

    Queued on the novel's worker rather than run inline, so it streams into the Activity
    view, survives the browser closing, and rides out a rate limit like any other work.
    """
    _, cfg = project_cfg(pid)
    # AI-resolve re-translates, so it must also see the CURRENT source: re-fetch the live
    # doc (same reason as start_translation) so resolving an edited chapter uses the edited
    # Korean, not the stale cached snapshot. Falls back to the local copy if unfetchable.
    chapters = await run_in_threadpool(get_chapters, pid, cfg, True)
    ch = next((c for c in chapters if c.index == index), None)
    if ch is None:
        raise HTTPException(404, f"chapter {index} not found")
    if classify(ch, cfg) != "korean":
        raise HTTPException(400, "Only Korean chapters can be re-translated.")
    return _enqueue_task(pid, cfg, [(index, True, TASK_RESOLVE)])


def _recheck_saved(pid: str, cfg: Config, indices: list[int]) -> dict[int, list[dict]]:
    """Re-run validation on chapters' SAVED English and persist the verdict.

    Free — no model call and no source re-fetch — because the text being judged is
    already on disk. A flag stored in state.json is only ever a snapshot of what the
    checks said at translation time; the glossary has usually moved on since (pronouns
    filled in, a name's spelling fixed), and the checks themselves improve. Re-running
    them first means the repair is asked for only where a problem still exists, rather
    than spending a model call to "fix" a chapter that already reads correctly.

    Returns the pronoun conflicts that survive, keyed by chapter index.
    """
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    by_index = {c.index: c for c in chapters}
    state = State.load(cfg.paths.state_file)
    remaining: dict[int, list[dict]] = {}
    verdicts: dict[int, object] = {}
    # Judge first, persist second. Validation is pure CPU over text already on disk, and
    # "Fix all flagged" can hand us every flagged chapter in the novel — running it while
    # holding the state lock would stall the translation worker for the whole sweep.
    for index in indices:
        ch = by_index.get(index)
        rec = state.get(index)
        if ch is None or not rec:
            continue
        prose = current_translation(cfg, index, total)
        if not prose or not prose.strip():
            remaining[index] = pronoun_conflicts(rec)  # nothing to re-judge; leave as-is
            continue
        val = validate_translation(stripped_chapter(ch), prose, cfg.validation, glossary_for(cfg, ch))
        remaining[index] = val.pronoun_conflicts
        verdicts[index] = val
    if verdicts:
        # Re-read under the lock and compare against THAT: the records judged above are a
        # snapshot, and the worker may have rewritten one of these chapters since.
        with _state_lock(cfg.paths.state_file):
            fresh = State.load(cfg.paths.state_file)
            changed = False
            for index, val in verdicts.items():
                rec = fresh.get(index) or {}
                if val.failures == (rec.get("failures") or []):
                    continue  # verdict unchanged — don't rewrite state for nothing
                changed = True
                fresh.update(
                    index,
                    status=state_mod.STATUS_VALIDATED if val.ok else state_mod.STATUS_NEEDS_REVIEW,
                    validation=val.metrics,
                    failures=val.failures,
                    pronoun_conflicts=val.pronoun_conflicts,
                )
            if changed:
                fresh.save(cfg.paths.state_file)
    return remaining


@app.post("/api/projects/{pid}/chapters/{index}/fix-pronouns")
async def fix_chapter_pronouns(pid: str, index: int) -> dict:
    """Correct the pronouns of mis-gendered characters in ONE chapter.

    Unlike AI resolve this does not re-translate: the glossary already knows each
    character's pronoun, so the saved English is sent back to be rewritten with only
    the pronouns changed. Cheaper, and it keeps prose the user may have edited.

    The stored flag is re-checked against the saved text first, so a chapter that is
    already correct clears itself here instead of queueing a model call that would
    find nothing to change and leave it flagged all over again.
    """
    _, cfg = project_cfg(pid)
    state = State.load(cfg.paths.state_file)
    if not pronoun_conflicts(state.get(index)):
        raise HTTPException(400, "This chapter has no mis-gendered characters to fix. "
                                 "Pronouns are checked against the glossary, so a character "
                                 "needs a pronoun set on the Glossary tab first.")
    remaining = await run_in_threadpool(_recheck_saved, pid, cfg, [index])
    if not remaining.get(index):
        rec = State.load(cfg.paths.state_file).get(index) or {}
        return {"cleared": [index], "queued": [], "job_id": None,
                "status": rec.get("status"),
                "message": "This chapter's pronouns already read correctly — "
                           "the old flag was out of date and has been cleared."}
    return {"cleared": [], **_enqueue_task(pid, cfg, [(index, True, TASK_PRONOUNS)])}


@app.post("/api/projects/{pid}/pronouns/fix-flagged")
async def fix_flagged_pronouns(pid: str) -> dict:
    """Queue a pronoun fix for every chapter in this novel flagged as mis-gendered.

    Chapters whose flag no longer holds are cleared for free (see ``_recheck_saved``)
    and never reach the queue.
    """
    _, cfg = project_cfg(pid)
    state = State.load(cfg.paths.state_file)
    indices = sorted(
        int(k) for k, rec in (state.chapters or {}).items()
        if isinstance(rec, dict) and k.lstrip("-").isdigit() and pronoun_conflicts(rec)
    )
    if not indices:
        raise HTTPException(400, "No chapters are flagged for wrong pronouns.")
    remaining = await run_in_threadpool(_recheck_saved, pid, cfg, indices)
    todo = [i for i in indices if remaining.get(i)]
    cleared = [i for i in indices if i not in todo]
    if not todo:
        return {"cleared": cleared, "queued": [], "job_id": None,
                "message": f"All {len(cleared)} chapter{'' if len(cleared) == 1 else 's'} "
                           "already read correctly — the old flags were out of date and "
                           "have been cleared."}
    return {"cleared": cleared,
            **_enqueue_task(pid, cfg, [(i, True, TASK_PRONOUNS) for i in todo])}


@app.post("/api/projects/{pid}/chapters/{index}/accept")
def accept_chapter(pid: str, index: int) -> dict:
    """Mark a flagged chapter as fine (user override) — clears the failures and sets it
    validated, keeping the existing translation. For false positives (e.g. the
    paragraph-count check tripping on chat/SNS-format chapters)."""
    _, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    ch = next((c for c in chapters if c.index == index), None)
    if ch is None:
        raise HTTPException(404, f"chapter {index} not found")
    out_path = cfg.paths.output_dir / chapter_filename(index, total)
    if not out_path.exists():
        # An original needs-review chapter was written ONLY to audit/, never chapters/.
        # Accepting must MATERIALIZE its translation into chapters/, or it would become a
        # validated-but-empty chapter (the reader/search/export would still show nothing).
        prose = read_audit_translation(cfg.paths.audit_dir, index, total)
        if not prose:
            raise HTTPException(409, "No saved translation to accept — re-translate this chapter first.")
        write_chapter_file(cfg.paths.output_dir, index, total, prose)
    with mutate_state(cfg.paths.state_file) as state:
        state.update(index, status=state_mod.STATUS_VALIDATED, title=ch.title,
                     source_hash=ch.metrics.content_hash, failures=[], manual_edit=True)
    return {"ok": True, "status": state_mod.STATUS_VALIDATED}


# ------------------------------------------------------------------- paragraph rewrites
# Retranslate or rephrase ONE paragraph of a finished chapter, keeping every version so
# the reader can compare and pick.
#
# Two design points carry the rest:
#   1. A paragraph is addressed by ordinal and PROVED by its exact text. The chapter
#      file stays the single source of truth, so nothing here can drift out of sync
#      with a whole-chapter edit, an AI resolve or a consistency rename.
#   2. Generating writes only to variants/. Only the reader's pick touches the chapter.
#      With no file to race, generation runs inline instead of queueing behind a
#      40-chapter translation — which would make an interactive action unusable.

MIN_ALIGNMENT_CONFIDENCE = 0.4


class ParagraphRef(BaseModel):
    paragraph: int
    expected_text: str
    group_id: str | None = None


class ParagraphRephrase(ParagraphRef):
    instruction: str = ""


class ParagraphPick(BaseModel):
    group_id: str
    variant_id: str


class ParagraphDiscard(BaseModel):
    group_id: str
    variant_id: str | None = None   # None discards the whole group's history


def _paragraph_context(pid: str, index: int):
    """(project, cfg, chapter, total, translation, blocks) or the right HTTP error."""
    project, cfg = project_cfg(pid)
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    ch = next((c for c in chapters if c.index == index), None)
    if ch is None:
        raise HTTPException(404, f"chapter {index} not found")
    # Resolves chapters/ first, then audit/ — a needs-review chapter's translation
    # lives only in audit/, and it is exactly the kind you want to fix a line of.
    text = current_translation(cfg, index, total)
    if not (text or "").strip():
        raise HTTPException(400, "That chapter hasn't been translated yet.")
    return project, cfg, ch, total, text, split_blocks(text)


def _require_block(blocks, paragraph: int, expected: str) -> int:
    k = locate_block(blocks, expected, paragraph)
    if k is None:
        raise HTTPException(
            409, "This chapter has changed since you opened it. Reload and try again.")
    return k


def _worker_owns_chapter(pid: str, index: int) -> bool:
    """Whether the novel's worker is about to rewrite this whole chapter.

    Not a file race — generating writes nothing to the chapter — but rewriting one
    paragraph of a chapter that is about to be replaced wholesale is wasted spend and
    instantly stale.
    """
    job = _jobs.get(_active_job_by_project.get(pid) or "")
    if job is None or job.done:
        return False
    if job.current == index and job.kind not in PAGE_TASK_KINDS:
        return True
    return any(i == index and k not in PAGE_TASK_KINDS
               for i, _f, k in job.snapshot_pending())


def _alignment_for(pid: str, ch: Chapter, blocks, k: int):
    """(alignment, reason-it-is-unavailable). Rephrase never needs this."""
    if pid in _offline_projects:
        return None, ("This novel is open as a saved copy, so its Korean source isn't "
                      "available here. You can still rephrase the English.")
    korean = stripped_chapter(ch).paragraphs
    if not korean:
        return None, "There's no Korean source stored for this chapter."
    if hangul_fraction(ch.text) < 0.15:
        return None, "This chapter's source is already English."
    alignment = align_korean([b.text for b in blocks], k, korean)
    if alignment.index < 0 or alignment.confidence < MIN_ALIGNMENT_CONFIDENCE:
        return alignment, ("No reliable Korean match for this paragraph — the chapter's "
                           "paragraphs don't line up with the source. You can still "
                           "rephrase the English.")
    return alignment, None


@app.get("/api/projects/{pid}/chapters/{index}/variants")
def chapter_variants(pid: str, index: int) -> dict:
    """This chapter's paragraph history, re-anchored to the text as it stands now."""
    _project, cfg, ch, total, _text, blocks = _paragraph_context(pid, index)
    # Read-only: this is a GET, and it used to enter mutate_variants, which saves
    # unconditionally on exit. Every single chapter you opened therefore created and
    # rewrote projects/<id>/variants/chapter-NN.json — mkdir, serialise, fsync,
    # replace — even for a chapter with no rewrite history at all. Re-anchoring is
    # recomputed on every read anyway, so nothing is lost by not persisting it; the
    # paths that actually change something (generate, apply, discard) still do.
    path = variants_mod.variants_path(pid, index, total)
    doc = variants_mod.load_variants(path, index)
    # A whole-chapter edit may have moved paragraphs since these were written.
    variants_mod.relocate(doc, [b.text for b in blocks])
    groups = [dict(g) for g in doc.get("groups", [])]
    _alignment, reason = _alignment_for(pid, ch, blocks, 0)
    return {"index": index, "paragraph_count": len(blocks), "groups": groups,
            "retranslate_available": reason is None, "retranslate_reason": reason}


@app.post("/api/projects/{pid}/chapters/{index}/paragraph/source")
def paragraph_source(pid: str, index: int, body: ParagraphRef) -> dict:
    """The Korean this paragraph probably came from, as a window around the best guess."""
    _project, _cfg, ch, _total, _text, blocks = _paragraph_context(pid, index)
    k = _require_block(blocks, body.paragraph, body.expected_text)
    alignment, reason = _alignment_for(pid, ch, blocks, k)
    if reason:
        return {"paragraph": k, "korean": [], "focus": 0, "alignment": None,
                "available": False, "reason": reason}
    window, focus = korean_window(stripped_chapter(ch).paragraphs, alignment)
    return {"paragraph": k, "korean": window, "focus": focus,
            "alignment": vars(alignment), "available": True, "reason": None}


async def _generate_paragraph(pid: str, index: int, body: ParagraphRef, mode: str) -> dict:
    _project, cfg, ch, total, _text, blocks = _paragraph_context(pid, index)
    if _worker_owns_chapter(pid, index):
        raise HTTPException(
            409, "That chapter is being worked on right now — try again when it finishes.")
    k = _require_block(blocks, body.paragraph, body.expected_text)
    before = blocks[k].text

    path = variants_mod.variants_path(pid, index, total)
    doc = variants_mod.load_variants(path, index)
    variants_mod.relocate(doc, [b.text for b in blocks])
    group = (variants_mod.find_group(doc, body.group_id) if body.group_id
             else variants_mod.group_for_paragraph(doc, k))
    # Everything already produced, so the model is told not to repeat itself. Without
    # this a regenerate returns near-identical text and the whole point is lost.
    prior = variants_mod.prior_texts(group) if group else [before]

    glossary = Glossary.load(cfg.paths.glossary_json)
    entries = glossary.relevant_to(ch.text)
    translator = Translator(cfg.anthropic, cfg.translation,
                            canonical_names=glossary.canonical())

    context_before = blocks[k - 1].text if k > 0 else ""
    context_after = blocks[k + 1].text if k + 1 < len(blocks) else ""

    alignment = None
    source_ko = None
    try:
        if mode == variants_mod.KIND_RETRANSLATE:
            alignment, reason = _alignment_for(pid, ch, blocks, k)
            if reason:
                raise HTTPException(400, reason)
            korean = stripped_chapter(ch).paragraphs
            window, focus = korean_window(korean, alignment)
            source_ko = korean[alignment.index]
            raw, usage, cost = await run_in_threadpool(
                translator.retranslate_paragraph,
                english=before, korean_window=window, focus=focus,
                context_before=context_before, context_after=context_after,
                glossary_entries=entries, avoid=prior,
                extra_instruction=cfg.translation.extra_instruction or "")
        else:
            raw, usage, cost = await run_in_threadpool(
                translator.rephrase_paragraph,
                english=before, instruction=getattr(body, "instruction", ""),
                context_before=context_before, context_after=context_after,
                glossary_entries=entries, avoid=prior)
    except RateLimitedError as exc:
        raise HTTPException(429, f"{exc} (this used your plan's allowance — try again later)")
    except TranslatorError as exc:
        raise HTTPException(502, f"Couldn't rewrite that paragraph: {exc}")

    # The attempt was billed whether or not the reply is usable, so record it either
    # way rather than dropping it from the novel's totals.
    with mutate_state(cfg.paths.state_file) as state:
        state.add_usage(index, usage, cost)

    check = check_paragraph_result(before, raw, mode=mode, source_ko=source_ko,
                                   glossary=entries, prior=tuple(prior))
    if not check.ok:
        # A malformed reply is a normal outcome, not an error dialog.
        return {"ok": False, "reasons": check.reasons, "usage": usage, "cost_usd": cost}

    with variants_mod.mutate_variants(path, index) as live:
        variants_mod.relocate(live, [b.text for b in blocks])
        target = (variants_mod.find_group(live, group["id"]) if group else None)
        if target is None:
            target = variants_mod.new_group(
                live, k, before, source_ko=source_ko,
                alignment=(vars(alignment) if alignment else None))
        variant = variants_mod.add_variant(
            target, kind=mode, text=check.text,
            instruction=(getattr(body, "instruction", "") or None
                         if mode == variants_mod.KIND_REPHRASE else None),
            usage=usage, cost=cost, warnings=tuple(check.warnings))
        variants_mod.prune(live)
        out_group = dict(target)

    return {"ok": True, "group": out_group, "variant_id": variant["id"],
            "duplicate": check.duplicate, "warnings": check.warnings,
            "alignment": (vars(alignment) if alignment else None),
            "usage": usage, "cost_usd": cost}


@app.post("/api/projects/{pid}/chapters/{index}/paragraph/retranslate")
async def retranslate_paragraph(pid: str, index: int, body: ParagraphRef) -> dict:
    """Translate this paragraph again from the Korean."""
    return await _generate_paragraph(pid, index, body, variants_mod.KIND_RETRANSLATE)


@app.post("/api/projects/{pid}/chapters/{index}/paragraph/rephrase")
async def rephrase_paragraph(pid: str, index: int, body: ParagraphRephrase) -> dict:
    """Reword this paragraph in English. Never needs the Korean, so it keeps working
    on saved-copy novels and chapters whose source doesn't line up."""
    return await _generate_paragraph(pid, index, body, variants_mod.KIND_REPHRASE)


@app.post("/api/projects/{pid}/chapters/{index}/paragraph/apply")
def apply_paragraph(pid: str, index: int, body: ParagraphPick) -> dict:
    """Put one version into the chapter. Picking v0 is revert-to-original."""
    # `_text` deliberately unused: the authoritative read happens inside the file lock
    # below. This one only serves the cheap pre-checks.
    _project, cfg, ch, total, _text, blocks = _paragraph_context(pid, index)
    if _worker_owns_chapter(pid, index):
        raise HTTPException(
            409, "That chapter is being worked on right now — try again when it finishes.")

    path = variants_mod.variants_path(pid, index, total)
    doc = variants_mod.load_variants(path, index)
    variants_mod.relocate(doc, [b.text for b in blocks])
    group = variants_mod.find_group(doc, body.group_id)
    if group is None:
        raise HTTPException(404, "That paragraph's history is gone.")
    if group.get("stale"):
        raise HTTPException(409, "That paragraph has changed since these versions were made.")
    variant = variants_mod.find_variant(group, body.variant_id)
    if variant is None:
        raise HTTPException(404, "That version is gone.")

    if not 0 <= group.get("paragraph", -1) < len(blocks):
        raise HTTPException(409, "That paragraph is no longer where it was.")

    out_path = cfg.paths.output_dir / chapter_filename(index, total)
    with file_lock(out_path):
        # Re-read INSIDE the lock and re-anchor against what is on disk NOW. The copy
        # fetched at the top of this request was taken before the lock was held, so a
        # translation, AI resolve, pronoun fix or manual save landing in between would
        # have been silently thrown away when this splice wrote the stale text back.
        fresh = current_translation(cfg, index, total)
        if not (fresh or "").strip():
            raise HTTPException(
                409, "That chapter's translation is no longer there. Reload and try again.")
        fresh_blocks = split_blocks(fresh)
        variants_mod.relocate(doc, [b.text for b in fresh_blocks])
        if group.get("stale"):
            raise HTTPException(
                409, "That paragraph changed while you were choosing. Reload and try again.")
        k = group.get("paragraph", -1)
        if not 0 <= k < len(fresh_blocks):
            raise HTTPException(409, "That paragraph is no longer where it was.")

        # A needs-review chapter lives only in audit/. Materialize it first — exactly
        # as accepting one does — or the splice would write a file the reader can see
        # while chapters/ stays empty.
        if not out_path.exists():
            write_chapter_file(cfg.paths.output_dir, index, total, fresh)
        try:
            updated = splice_block(fresh, k, variant.get("text", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        # snapshot=False: this edit keeps its own history in variants/, and letting
        # every pick overwrite the single previous/ slot would destroy the
        # whole-chapter snapshot after one click.
        write_chapter_file(cfg.paths.output_dir, index, total, updated, snapshot=False)

    # Re-judge OUTSIDE the state lock (the mutate_state body must stay short), then
    # persist — the same order _recheck_saved uses.
    verdict = validate_translation(stripped_chapter(ch), updated, cfg.validation,
                                   glossary_for(cfg, ch))
    with mutate_state(cfg.paths.state_file) as state:
        rec = state.get(index) or {}
        # A pick is a user-approved edit, like saving the chapter by hand. It does NOT
        # clear a needs-review flag (that stays "Mark as fine"'s job), and it does not
        # touch source_hash: that fingerprints the KOREAN, which hasn't changed.
        state.update(index, manual_edit=True, validation=verdict.metrics)
    _invalidate_consistency(pid)

    with variants_mod.mutate_variants(path, index) as live:
        target = variants_mod.find_group(live, body.group_id)
        if target is not None:
            target["current_id"] = body.variant_id
            target["updated_at"] = variants_mod.now_iso()
            variants_mod.relocate(live, [b.text for b in split_blocks(updated)])
            out_group = dict(target)
        else:
            out_group = dict(group)

    return {"ok": True, "translation": updated, "group": out_group,
            "status": rec.get("status"), "validation": verdict.metrics}


@app.post("/api/projects/{pid}/chapters/{index}/paragraph/discard")
def discard_paragraph_history(pid: str, index: int, body: ParagraphDiscard) -> dict:
    """Throw away one version, or a paragraph's whole history. Never automatic."""
    _project, _cfg, _ch, total, _text, _blocks = _paragraph_context(pid, index)
    path = variants_mod.variants_path(pid, index, total)
    with variants_mod.mutate_variants(path, index) as doc:
        group = variants_mod.find_group(doc, body.group_id)
        if group is None:
            raise HTTPException(404, "That paragraph's history is gone.")
        if body.variant_id is None:
            doc["groups"] = [g for g in doc["groups"] if g.get("id") != body.group_id]
        else:
            if body.variant_id in ("v0", group.get("current_id")):
                raise HTTPException(
                    400, "That version is either the original or the one currently in "
                         "the chapter, so it can't be discarded.")
            group["variants"] = [v for v in group.get("variants", [])
                                 if v.get("id") != body.variant_id]
        groups = [dict(g) for g in doc.get("groups", [])]
    return {"ok": True, "groups": groups}


# --------------------------------------------------------------------- name consistency
# Proper-noun candidates: a capitalized word, or run of them ("Go Won"), not all-caps.
_PROPER_RE = re.compile(r"\b[A-Z][a-z]+(?:[ \-][A-Z][a-z]+)*\b")
# Common capitalized words (sentence starts, pronouns, etc.) that aren't names.
_NAME_STOP = {
    "The", "A", "An", "I", "He", "She", "It", "They", "We", "You", "His", "Her", "Its",
    "Their", "Our", "Your", "My", "Me", "Him", "Them", "Us", "This", "That", "These",
    "Those", "But", "And", "Or", "So", "Yet", "For", "Nor", "If", "Then", "When", "While",
    "As", "At", "By", "In", "On", "Of", "To", "Up", "Out", "No", "Yes", "Not", "Now",
    "What", "Why", "How", "Who", "Whom", "Whose", "Where", "Which", "Oh", "Ah", "Eh",
    "Hey", "Well", "Okay", "Ok", "Maybe", "Even", "Just", "Still", "After", "Before",
    "Because", "Since", "Though", "Although", "However", "Anyway", "Sir", "Madam",
    "Mr", "Mrs", "Ms", "Miss", "Lord", "Lady", "God", "Chapter",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
}


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@app.get("/api/projects/{pid}/consistency")
def consistency_scan(pid: str) -> dict:
    """Scan a novel's translations for proper nouns spelled inconsistently across
    chapters, and frequent ones missing from the glossary. Lexical — no AI."""
    _, cfg = project_cfg(pid)
    return _consistency_report(pid, cfg)


def _consistency_report(pid: str, cfg: Config) -> dict:
    """The scan itself, callable per-novel or in a loop for the cross-novel Upkeep view.

    Reads from disk only (see ``_translated_chapters_local``): the scan looks at the
    English output, so fetching the Korean source document would be pure cost. Still
    I/O-heavy across a whole library, so the library-wide caller runs it off the event
    loop and caches the per-novel result.
    """
    chapters = _translated_chapters_local(cfg)  # (index, title, markdown)
    g = Glossary.load(cfg.paths.glossary_json)
    gloss_norm: dict[str, str] = {}
    for e in g.entries():
        if e.english:
            gloss_norm.setdefault(_norm_name(e.english), e.english)

    spell_count: Counter = Counter()
    spell_chapters: dict[str, set] = defaultdict(set)
    for idx, _title, md in chapters:
        for spell, c in Counter(_PROPER_RE.findall(md)).items():
            if spell in _NAME_STOP or len(spell) < 2:
                continue
            spell_count[spell] += c
            spell_chapters[spell].add(idx)

    by_norm: dict[str, list] = defaultdict(list)
    for spell in spell_count:
        by_norm[_norm_name(spell)].append(spell)

    def opt(s: str) -> dict:
        return {"spelling": s, "count": spell_count[s], "chapters": sorted(spell_chapters[s])}

    variants, missing = [], []
    for normkey, spellings in by_norm.items():
        distinct = sorted(set(spellings), key=lambda s: -spell_count[s])
        canonical = gloss_norm.get(normkey)
        if canonical:
            if any(s != canonical for s in distinct):  # some chapter used a non-canonical spelling
                variants.append({"canonical": canonical, "glossary_spelling": canonical,
                                 "options": [opt(s) for s in distinct]})
        elif len(distinct) > 1:  # spelled multiple ways, none in glossary
            variants.append({"canonical": distinct[0], "glossary_spelling": None,
                             "options": [opt(s) for s in distinct]})
        elif spell_count[distinct[0]] >= 5:  # frequent single spelling, not in glossary
            missing.append(opt(distinct[0]))

    variants.sort(key=lambda v: -sum(o["count"] for o in v["options"]))
    missing.sort(key=lambda m: -m["count"])
    return {"scanned": len(chapters), "variants": variants[:200], "missing": missing[:200]}


class ReplaceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    froms: list[str] = Field(default_factory=list, alias="from")  # spellings to unify
    to: str
    chapters: list[int] = []


@app.post("/api/projects/{pid}/consistency/replace")
def consistency_replace(pid: str, body: ReplaceRequest) -> dict:
    """Whole-word replace one or more spellings → a canonical one across the given
    chapters' saved translations (instant, exact, free). ALL spellings are applied in a
    single pass per chapter, so each changed chapter is snapshotted to previous/ exactly
    once — keeping the edit cleanly revertible from the reader."""
    _, cfg = project_cfg(pid)
    to = (body.to or "").strip()
    if not to:
        raise HTTPException(400, "Replacement text is empty.")
    # Longest first so "Go Won" wins over "Go"; drop blanks and self-replacements.
    froms = sorted({f.strip() for f in body.froms if f and f.strip() and f.strip() != to},
                   key=len, reverse=True)
    if not froms:
        return {"replaced": 0, "chapters": []}
    # Bound the alternation so a huge/garbled request can't build a pathological regex.
    if len(froms) > 200 or any(len(f) > 200 for f in froms):
        raise HTTPException(400, "Too many or too-long spellings to replace at once.")
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    pattern = re.compile(r"\b(" + "|".join(re.escape(f) for f in froms) + r")\b")
    targets = set(body.chapters) if body.chapters else None
    replaced, changed = 0, []
    for ch in chapters:
        if targets is not None and ch.index not in targets:
            continue
        path = cfg.paths.output_dir / chapter_filename(ch.index, total)
        text = _safe_read(path)
        if text is None:
            continue
        new, n = pattern.subn(to, text)
        if n and new != text:
            write_chapter_file(cfg.paths.output_dir, ch.index, total, new)  # one snapshot per chapter
            replaced += n
            changed.append(ch.index)
    return {"replaced": replaced, "chapters": changed}


# --------------------------------------------------------------------- upkeep (all novels)
# Glossary approval and consistency checks used to exist only per-novel, which doesn't
# scale: with dozens of novels, staying on top of them meant opening every one by hand.
# These endpoints follow the /api/review pattern — walk every project, isolate each in its
# own try/except so one unreadable novel can't blank the page, and return flat rows.

# The consistency scan reads every translated chapter file, so a library-wide sweep is
# thousands of reads. Cache the per-novel summary and invalidate it when that novel's
# chapters change (translation completes, a unify runs, an edit is saved).
_consistency_cache: dict[str, dict] = {}


def _invalidate_consistency(pid: str) -> None:
    _consistency_cache.pop(pid, None)


def _each_project():
    """Yield (project, cfg) for every non-archived novel, skipping any that won't load."""
    for project in pj.list_projects():
        try:
            yield project, pj.project_config(load_global_config(), project)
        except Exception:  # noqa: BLE001 — one broken novel never breaks the sweep
            continue


@app.get("/api/glossary/pending")
def all_pending_terms() -> dict:
    """Every novel's unapproved glossary suggestions, grouped by novel. Cheap: one small
    JSON read per project."""
    novels = []
    for project, cfg in _each_project():
        try:
            pending = load_pending(cfg.paths.glossary_pending)
        except Exception:  # noqa: BLE001
            continue
        if pending:
            novels.append({"pid": project["id"],
                           "name": project.get("name", "Untitled novel"),
                           "pending": pending})
    novels.sort(key=lambda n: n["name"].lower())
    return {"novels": novels, "total": sum(len(n["pending"]) for n in novels)}


def _summarize_consistency(project: dict, cfg: Config) -> dict:
    report = _consistency_report(project["id"], cfg)
    return {
        "pid": project["id"],
        "name": project.get("name", "Untitled novel"),
        "scanned": report["scanned"],
        "variants": len(report["variants"]),
        "missing": len(report["missing"]),
        # Variants whose canonical spelling is already fixed by the glossary have an
        # unambiguous correct target, so they're the only ones safe to bulk-fix.
        "auto_fixable": sum(1 for v in report["variants"] if v.get("glossary_spelling")),
    }


_consistency_scan_task: asyncio.Task | None = None


async def _scan_consistency_missing(pids_to_scan: list[str]) -> None:
    """Fill the summary cache one novel at a time, off the event loop.

    Deliberately incremental rather than one big batch: each novel lands in the cache as
    soon as it's done, so the page fills in progressively instead of showing nothing for
    the whole sweep. A cold sweep of a large library is genuinely slow — thousands of
    files, and on Windows each read may be virus-scanned — so it must never block a
    request.
    """
    by_id = {p["id"]: (p, c) for p, c in _each_project()}
    for pid in pids_to_scan:
        entry = by_id.get(pid)
        if entry is None:
            continue
        try:
            _consistency_cache[pid] = await run_in_threadpool(
                _summarize_consistency, entry[0], entry[1]
            )
        except Exception as exc:  # noqa: BLE001 — one bad novel never stalls the sweep
            _consistency_cache[pid] = {
                "pid": pid, "name": entry[0].get("name", "Untitled novel"),
                "scanned": 0, "variants": 0, "missing": 0, "auto_fixable": 0,
                "error": errors.explain(exc).title,
            }


@app.get("/api/consistency/summary")
async def all_consistency_summary(refresh: bool = False) -> dict:
    """Per-novel COUNTS only, served from cache and filled in in the background.

    Two deliberate limits. First, counts rather than full reports: shipping every variant
    for every novel would be megabytes the user hasn't asked to see, so the page fetches
    one novel's full report from /api/projects/{pid}/consistency when a row is expanded.
    Second, this never blocks — it answers immediately with whatever is cached and reports
    how many novels are still being scanned, so the client can poll and watch rows appear.
    """
    global _consistency_scan_task

    projects = [p for p, _c in _each_project()]
    if refresh:
        _consistency_cache.clear()

    missing = [p["id"] for p in projects if p["id"] not in _consistency_cache]
    if missing and (_consistency_scan_task is None or _consistency_scan_task.done()):
        _consistency_scan_task = asyncio.create_task(_scan_consistency_missing(missing))
        _running_tasks.add(_consistency_scan_task)
        _consistency_scan_task.add_done_callback(_running_tasks.discard)

    rows = [_consistency_cache[p["id"]] for p in projects if p["id"] in _consistency_cache]
    rows.sort(key=lambda r: (-r["variants"], r["name"].lower()))
    return {"novels": rows,
            "scanning": len(missing) > 0,
            "remaining": len(missing),
            "total": len(projects),
            "total_variants": sum(r["variants"] for r in rows),
            "total_missing": sum(r["missing"] for r in rows),
            "total_auto_fixable": sum(r["auto_fixable"] for r in rows)}


class BulkGlossaryReview(BaseModel):
    # pid -> the same {approve, reject} payload the per-novel endpoint takes.
    by_project: dict[str, GlossaryReview] = {}


@app.post("/api/glossary/review-bulk")
def review_glossary_bulk(body: BulkGlossaryReview) -> dict:
    """Approve/reject terms across several novels in one request.

    One round trip instead of dozens of sequential ones from the browser, and each novel
    is isolated so a single failure is reported rather than aborting the whole batch.
    """
    results, failed = [], []
    for pid, review in body.by_project.items():
        try:
            res = review_glossary(pid, review)
            results.append({"pid": pid, **res})
        except HTTPException as exc:
            failed.append({"pid": pid, "error": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"pid": pid, "error": errors.explain(exc).title})
    return {"novels": results, "failed": failed,
            "approved": sum(r["approved"] for r in results),
            "rejected": sum(r["rejected"] for r in results)}


class BulkUnify(BaseModel):
    pids: list[str] = []       # empty = every novel with auto-fixable variants


@app.post("/api/consistency/unify-bulk")
async def consistency_unify_bulk(body: BulkUnify) -> dict:
    """Unify only the spellings with an unambiguous target — those where the glossary
    already fixes the canonical spelling and some chapters disagree with it.

    Variants with no glossary entry are deliberately skipped: picking a winner there is a
    judgement call (which spelling is right?) and belongs to the user, not a bulk button.
    """
    def run() -> tuple[list[dict], list[dict]]:
        done, failed = [], []
        wanted = set(body.pids) if body.pids else None
        for project, cfg in _each_project():
            pid = project["id"]
            if wanted is not None and pid not in wanted:
                continue
            try:
                report = _consistency_report(pid, cfg)
                fixed = 0
                for v in report["variants"]:
                    target = v.get("glossary_spelling")
                    if not target:
                        continue
                    others = [o for o in v["options"] if o["spelling"] != target]
                    if not others:
                        continue
                    chapters = sorted({c for o in others for c in o["chapters"]})
                    res = consistency_replace(pid, ReplaceRequest(
                        **{"from": [o["spelling"] for o in others], "to": target,
                           "chapters": chapters}))
                    fixed += res["replaced"]
                if fixed:
                    _invalidate_consistency(pid)
                    done.append({"pid": pid, "name": project.get("name", ""), "replaced": fixed})
            except Exception as exc:  # noqa: BLE001
                failed.append({"pid": pid, "error": errors.explain(exc).title})
        return done, failed

    done, failed = await run_in_threadpool(run)
    return {"novels": done, "failed": failed,
            "replaced": sum(d["replaced"] for d in done)}


# ----------------------------------------------------------------------------- glossary
@app.get("/api/projects/{pid}/glossary")
def glossary(pid: str) -> dict:
    _, cfg = project_cfg(pid)
    g = Glossary.load(cfg.paths.glossary_json)
    return {"locked": [asdict(e) for e in g.entries()],
            "pending": load_pending(cfg.paths.glossary_pending)}


class TermDecision(BaseModel):
    # Accept the wire key "register" but avoid shadowing a BaseModel attribute.
    model_config = ConfigDict(populate_by_name=True)
    korean: str
    english: str
    type: str = "other"
    note: str = ""
    pronoun: str = ""   # character profile: he / she / they
    speech_register: str = Field("", alias="register")


class GlossaryReview(BaseModel):
    approve: list[TermDecision] = []
    reject: list[str] = []  # korean keys to drop from pending


@app.post("/api/projects/{pid}/glossary/review")
def review_glossary(pid: str, review: GlossaryReview) -> dict:
    _, cfg = project_cfg(pid)
    # Under the glossary lock: the translation worker calls queue_new_terms on every
    # finished chapter, and it reads pending too. Without ordering, a chapter's newly
    # found names were discarded by whichever save landed last — approving three terms
    # at the wrong moment quietly threw away the six the worker had just queued.
    with glossary_lock(cfg.paths.glossary_json):
        g = Glossary.load(cfg.paths.glossary_json)
        for t in review.approve:
            g.add(GlossaryEntry(korean=t.korean, english=t.english, type=t.type, note=t.note,
                                pronoun=normalize_pronoun(t.pronoun),
                                register=t.speech_register.strip()))
        g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
        decided = {t.korean for t in review.approve} | set(review.reject)
        remaining = [p for p in load_pending(cfg.paths.glossary_pending)
                     if p["korean"] not in decided]
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


class TermsDelete(BaseModel):
    terms: list[TermDelete] = []


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


@app.post("/api/projects/{pid}/glossary/term/delete-bulk")
def delete_glossary_terms(pid: str, body: TermsDelete) -> dict:
    """Drop several locked terms in one save — the multi-select delete on the
    glossary page. A term that's already gone is counted as missing rather than
    failing the whole batch, so a stale selection can't block the rest."""
    _, cfg = project_cfg(pid)
    g = Glossary.load(cfg.paths.glossary_json)
    removed, missing = 0, []
    for t in body.terms:
        korean = t.korean.strip()
        english = t.english.strip()
        if not korean and not english:
            continue
        if g.remove(korean) if korean else g.remove_english(english):
            removed += 1
        else:
            missing.append(korean or english)
    if removed:
        g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return {**_locked_payload(g), "removed": removed, "missing": missing}


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
    return {**_locked_payload(g), "imported": imported,
            "skipped": len(body.entries) - imported}


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
                            note=t.get("note", ""),
                            pronoun=normalize_pronoun(t.get("pronoun", ""))))
        existing_english.add(english.lower())
        added += 1
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return {"learned": added, "from_chapters": len(english_chs), **_locked_payload(g)}


def _sample_project_english(pid: str, cfg: Config) -> str:
    """English text for this novel: translated output chapters where they exist,
    plus source chapters that are already English. Same spread/budget approach as
    ``_sample_english`` so late-introduced characters are covered."""
    chapters = get_chapters(pid, cfg)
    total = _output_total(pid, chapters)
    sampled = []  # Chapter or a title/text namespace — _sample_english reads only those
    for ch in chapters:
        translated = _safe_read(cfg.paths.output_dir / chapter_filename(ch.index, total))
        if translated and translated.strip():
            sampled.append(SimpleNamespace(title=ch.title, text=translated))
        elif classify(ch, cfg) == "english":
            sampled.append(ch)
    return _sample_english(sampled)


@app.post("/api/projects/{pid}/glossary/detect-pronouns")
async def detect_pronouns(pid: str) -> dict:
    """Fill in the pronoun for character entries that don't have one yet, judged
    from the novel's own English text (translated chapters + already-English source).

    Only empty pronoun fields on `name` entries are filled — a value the user set
    by hand is never overwritten, and "unknown" results are left empty. Re-runnable;
    clearing a field in the table is the undo.
    """
    _, cfg = project_cfg(pid)
    g = Glossary.load(cfg.paths.glossary_json)
    targets = [e for e in g.entries() if e.type == "name" and e.english and not e.pronoun]
    if not targets:
        if any(e.type == "name" and e.english for e in g.entries()):
            raise HTTPException(400, "Every character already has a pronoun set.")
        raise HTTPException(400, "No character entries in the glossary yet.")
    sample = _sample_project_english(pid, cfg)
    if not sample.strip():
        raise HTTPException(400, "No English text yet — translate some chapters first.")
    translator = Translator(cfg.anthropic, cfg.translation)
    try:
        detected = await run_in_threadpool(
            translator.detect_pronouns, [e.english for e in targets], sample
        )
    except RateLimitedError as exc:
        raise HTTPException(429, f"{exc} (this used your plan's allowance — try again later)")
    except TranslatorError as exc:
        raise HTTPException(502, f"Couldn't read the chapters: {exc}")

    filled, unresolved = [], []
    for e in targets:
        pronoun = detected.get(e.english.lower(), "")
        if pronoun:
            e.pronoun = pronoun  # entries() returns the live objects — mutate in place
            filled.append({"english": e.english, "pronoun": pronoun})
        else:
            unresolved.append(e.english)
    if filled:
        g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return {"filled": filled, "unresolved": unresolved, **_locked_payload(g)}


class BulkTermIn(BaseModel):
    # Not TermUpsert: its `type = "other"` default would make "no type given"
    # unrepresentable, and here "" is the signal for "auto-classify this row".
    model_config = ConfigDict(populate_by_name=True)
    korean: str = ""
    english: str = ""  # validated per-row so bad rows are reported, not 422'd
    type: str = ""
    note: str = ""
    pronoun: str = ""
    speech_register: str = Field("", alias="register")


class GlossaryBulkAdd(BaseModel):
    text: str = ""                           # legacy flat paste (old clients / curl)
    entries: list[BulkTermIn] | None = None  # structured rows parsed client-side


@app.post("/api/projects/{pid}/glossary/bulk-add")
async def bulk_add_glossary(pid: str, body: GlossaryBulkAdd) -> dict:
    """Add a pasted batch of terms — full entries, per-type groups, or a flat list.

    Rows that arrive with a recognizable type are added directly and never touch
    the user's Claude plan; only unlabeled rows go through one classify_terms
    call. Typed rows are saved even if that call fails (the response then carries
    `classify_error` + `unclassified` so the client can offer a one-click retry);
    the legacy all-untyped path keeps its all-or-nothing 429/502 behavior.
    """
    _, cfg = project_cfg(pid)
    if body.entries is not None:
        rows = [{"korean": t.korean, "english": t.english, "type": t.type, "note": t.note,
                 "pronoun": t.pronoun, "register": t.speech_register} for t in body.entries]
    else:
        rows = [{"english": t} for t in split_flat(body.text)]
    if not rows:
        raise HTTPException(400, "Nothing to add — paste terms separated by commas, or a JSON/CSV block.")
    if len(rows) > MAX_ROWS:
        raise HTTPException(400, f"Too many rows at once (max {MAX_ROWS}) — split the paste.")

    g = Glossary.load(cfg.paths.glossary_json)
    plan = prepare_bulk_rows(rows, g.entries())
    if len(plan.untyped) > MAX_UNTYPED:
        raise HTTPException(400, f"{len(plan.untyped)} terms need type detection — max {MAX_UNTYPED} "
                                 "per paste. Add types to the rows, or split the paste.")
    if not (plan.typed or plan.untyped or plan.updated):
        return {**_locked_payload(g), "added": [], "updated": [], "skipped": plan.skipped}

    added, updated = [], []
    for e in plan.typed:
        g.add(e)
        added.append({"english": e.english, "type": e.type})
    for e in plan.updated:
        g.add(e)
        updated.append({"english": e.english, "korean": e.korean, "type": e.type})

    classify_error = None
    if plan.untyped:
        translator = Translator(cfg.anthropic, cfg.translation)  # constructed only when needed
        try:
            types = await run_in_threadpool(translator.classify_terms,
                                            [u["english"] for u in plan.untyped])
        except RateLimitedError as exc:
            if not (added or updated):
                raise HTTPException(429, f"{exc} (this used your plan's allowance — try again later)")
            classify_error = f"{exc} — try those again later, or add them with a type."
        except TranslatorError as exc:
            if not (added or updated):
                raise HTTPException(502, f"Couldn't classify the terms: {exc}")
            classify_error = f"Couldn't classify the terms: {exc}"
        if classify_error is None:
            for u in plan.untyped:
                typ = types.get(u["english"].lower(), "other")  # model omissions stay "other"
                e = GlossaryEntry(korean=u["korean"], english=u["english"], type=typ,
                                  note=u["note"], pronoun=u["pronoun"], register=u["register"])
                g.add(e)
                added.append({"english": e.english, "type": typ})

    if added or updated:
        g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    resp = {**_locked_payload(g), "added": added, "updated": updated, "skipped": plan.skipped}
    if classify_error:
        resp["classify_error"] = classify_error
        resp["unclassified"] = [u["english"] for u in plan.untyped]
    return resp


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


# What a queued item asks the worker to do. All of these run through the same Job so
# they show up in the Activity view, stream to the live console, and honour Stop and
# the rate-limit auto-resume without any of that being reimplemented per operation.
TASK_TRANSLATE = "translate"   # translate the Korean source (the original behaviour)
TASK_RESOLVE = "resolve"       # AI resolve: re-translate, corrected for what failed
TASK_PRONOUNS = "pronouns"     # rewrite only the pronouns of mis-gendered characters
TASK_OCR = "ocr"               # read Korean text out of one scanned page image
TASK_OCR_VERIFY = "ocr-verify"  # proof-read one page's text against its photo
TASK_KINDS = (TASK_TRANSLATE, TASK_RESOLVE, TASK_PRONOUNS, TASK_OCR, TASK_OCR_VERIFY)

# Kinds whose index is a PAGE sequence number rather than a chapter index.
PAGE_TASK_KINDS = (TASK_OCR, TASK_OCR_VERIFY)

# How each kind is described in the UI and the terminal.
TASK_LABEL = {
    TASK_TRANSLATE: "Translating",
    TASK_RESOLVE: "AI resolve on",
    TASK_PRONOUNS: "Fixing pronouns in",
    TASK_OCR: "Reading page",
    TASK_OCR_VERIFY: "Double-checking page",
}


def _queue_key(idx: int, kind: str) -> str:
    """Dedup key for the pending set.

    Pages and chapters share one worker but NOT one number space: once a scanned
    novel has been built, page 5 and chapter 5 both exist and are different things.
    Deduping on the bare index would make queueing a page silently drop a chapter (or
    the reverse). ``queue_state()`` still reports plain integer indices, so the
    Activity views and /api/queue are unaffected.
    """
    return f"{'pg' if kind in PAGE_TASK_KINDS else 'ch'}:{idx}"


class TaskRefused(Exception):
    """A repair declined to write anything, leaving the chapter exactly as it was.

    Distinct from a failure: nothing broke and nothing changed, so the chapter must
    keep its existing status rather than being marked failed. Raised when the pronoun
    rewrite comes back having altered more than pronouns, or when there is nothing
    saved to correct.
    """


def _run_task(
    kind: str,
    ch: Chapter,
    total: int,
    translator: Translator,
    glossary: Glossary,
    cfg: Config,
    state: State,
    hooks: StreamHooks,
) -> str:
    """Run one queued item. Blocking — always called via run_in_threadpool."""
    if kind == TASK_RESOLVE:
        rec = state.get(ch.index) or {}
        instruction = _corrective_instruction(rec.get("failures", []), pronoun_conflicts(rec))
        return repair_chapter(ch, total, translator, glossary, cfg, state,
                              instruction=instruction)
    if kind == TASK_PRONOUNS:
        try:
            return fix_pronouns_chapter(ch, total, translator, glossary, cfg, state,
                                        conflicts=pronoun_conflicts(state.get(ch.index)),
                                        hooks=hooks)
        except ValueError as exc:
            # The guard rejected the rewrite, or there was nothing to correct. Either
            # way the chapter on disk is untouched — say so instead of failing it.
            raise TaskRefused(str(exc)) from exc
    return process_chapter(ch, total, translator, glossary, cfg, state, hooks)


class Job:
    """A per-project translation worker fed by an APPENDABLE FIFO queue. Chapters can
    be enqueued while it runs, so the user never has to wait for one to finish before
    queuing the next. One worker per project keeps writes to state.json serialized."""

    def __init__(self, job_id: str, pid: str):
        self.id = job_id
        self.pid = pid
        # (chapter index, force, kind). `kind` is what to DO with the chapter:
        # "translate" (the default), "resolve" (AI re-translate targeting its failures),
        # or "pronouns" (rewrite only the mis-gendered pronouns). Routing them all
        # through this one queue is what puts them in the Activity view for free, and
        # keeps one worker per novel so writes to state.json stay serialized.
        self.pending: deque[tuple[int, bool, str]] = deque()
        # Namespaced "ch:<i>" / "pg:<i>" keys pending or in-flight (for dedup) — see
        # _queue_key: a page and a chapter can share a number.
        self.queued: set[str] = set()
        self.current: int | None = None       # chapter being worked on right now
        self.kind: str = TASK_TRANSLATE       # what is being done to it
        self.history: list[dict] = []         # every event so far, replayed on (re)connect
        self.subscribers: list[asyncio.Queue] = []  # one queue per live SSE consumer
        self.done = False
        self.cancelled = False
        self.terminal: dict | None = None      # final event, replayable for late consumers
        # Live view of the chapter in flight: the Korean the model was given plus the
        # English streamed back so far. Replayed as one frame when a stream (re)connects
        # mid-chapter, so a reload doesn't drop the user into a blank console.
        self.live: dict | None = None
        # Cooperative stop for the in-flight chapter. The translator polls this between
        # streamed messages; a threadpool thread can't be killed from out here.
        self.abort = threading.Event()
        # Set while the worker sleeps out a rate limit, waiting for the plan's usage
        # window to refresh: {resume_at, resets_at, message, since}. None otherwise.
        self.waiting: dict | None = None
        self.wake = asyncio.Event()            # cancel/resume-now interrupts the sleep
        # The worker mutates `pending` on the event loop while request THREADS read it
        # (/api/queue polls every few seconds, and Apply checks whether this chapter is
        # busy). Iterating a deque that changes size raises RuntimeError, which surfaced
        # as intermittent 500s that blanked the dashboard. Every touch of `pending` goes
        # through the helpers below.
        self._pending_lock = threading.Lock()

    # ---- queue access (always under _pending_lock) ----
    def enqueue(self, items: list[tuple[int, bool, str]]) -> list[int]:
        """Append (index, force, kind) triples, skipping ones already queued/in-flight."""
        added = []
        with self._pending_lock:
            for idx, force, kind in items:
                key = _queue_key(idx, kind)
                if key in self.queued:
                    continue
                self.queued.add(key)
                self.pending.append((idx, force, kind))
                added.append(idx)
        return added

    def snapshot_pending(self) -> list[tuple[int, bool, str]]:
        """A stable copy, safe to iterate from any thread."""
        with self._pending_lock:
            return list(self.pending)

    def take_next(self) -> tuple[int, bool, str] | None:
        with self._pending_lock:
            return self.pending.popleft() if self.pending else None

    def put_back(self, item: tuple[int, bool, str]) -> None:
        """Return an interrupted item to the head so it is retried first."""
        with self._pending_lock:
            self.pending.appendleft(item)

    def drain(self) -> list[tuple[int, bool, str]]:
        """Remove and return everything still waiting."""
        with self._pending_lock:
            dropped = list(self.pending)
            self.pending.clear()
            return dropped

    def queue_state(self) -> dict:
        pending = self.snapshot_pending()
        # Before anything starts, `kind` is the default and would mislabel a queued
        # repair as "Translating" until its start event lands — so fall back to what
        # is at the head of the queue.
        kind = self.kind if self.current is not None else (
            pending[0][2] if pending else self.kind)
        return {"current": self.current, "kind": kind,
                "pending": [i for i, _, _ in pending],
                "waiting": self.waiting}

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
        console.print_event(self.pid, ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def publish_live(self, ev: dict) -> None:
        """Fan out a high-frequency streaming event WITHOUT recording it in history.

        Deltas arrive many times per chapter; appending them would fill the 1000-event
        replay buffer with fragments and evict the real start/chapter/done events. A
        (re)connecting consumer gets ``self.live`` as a single catch-up frame instead.

        Must be called on the event loop — the translator runs in a worker thread and
        marshals here via ``loop.call_soon_threadsafe``.
        """
        console.print_event(self.pid, ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def live_frame(self) -> dict | None:
        """The current chapter's accumulated state as a replayable single event."""
        return {"type": "live", **self.live, **self.queue_state()} if self.live else None


def _persist_chapter_state(state: State, path: Path, idx: int) -> State:
    """Persist ONLY chapter ``idx``'s record without clobbering concurrent edits.

    The worker holds one in-memory ``state`` for the whole job, but the user can
    edit/accept/resolve OTHER chapters while it runs (each via its own load→save).
    Saving the worker's stale whole-state would discard those edits. So reload the
    on-disk state, overlay just this chapter, save atomically, and hand the merged
    state back for the worker to keep using (so later is_done/cost reads are current)."""
    rec = state.chapters.get(str(idx))
    with mutate_state(path) as fresh:
        if rec is not None:
            fresh.chapters[str(idx)] = rec
    return fresh


# Rate-limit waiting policy: how the worker rides out an exhausted usage window.
_RATE_LIMIT_BUFFER = 60          # sec past resets_at before retrying (clock skew slack)
_FALLBACK_WAIT = 5 * 60          # first retry when the SDK gave no resets_at
_FALLBACK_WAIT_MAX = 60 * 60     # backoff cap for unknown reset times
_MAX_WAIT = 12 * 3600            # sanity cap: distrust reset times further out than this
_MAX_STRIKES = 6                 # consecutive rate-limited retries before giving up


async def _sleep_until(job: Job, when: float) -> None:
    """Sleep until ``when`` (epoch sec), waking early if the job is cancelled,
    resume-now is clicked (job.wake), or the queue empties. Chunked so a laptop
    sleeping through the deadline or a cleared queue is noticed within a minute."""
    job.wake.clear()
    while time.time() < when and job.pending and not job.cancelled:
        try:
            await asyncio.wait_for(job.wake.wait(), timeout=min(when - time.time(), 60))
            return  # woken explicitly
        except asyncio.TimeoutError:
            continue


def _build_hooks(job: Job, loop: asyncio.AbstractEventLoop) -> tuple[StreamHooks, Callable[[], None]]:
    """Live-progress hooks for one chapter.

    ``process_chapter`` runs in a threadpool, so every callback here executes on a
    WORKER THREAD while ``job.publish_live`` touches asyncio queues that belong to the
    event loop. Each hook therefore marshals across with ``call_soon_threadsafe`` and
    does no other work inline.

    Deltas are coalesced on the worker-thread side: the SDK emits many small text
    blocks, and scheduling a loop callback plus an SSE frame for each one would flood
    both. We flush on a size or time threshold instead.

    Returns the hooks plus a ``flush()`` the caller MUST invoke once the chapter ends —
    otherwise the final sub-threshold fragment stays buffered and the live view is
    permanently missing the chapter's last couple of sentences.
    """
    buf: list[str] = []
    pending = {"chars": 0, "last": 0.0}
    lock = threading.Lock()

    FLUSH_CHARS = 200
    FLUSH_SECONDS = 0.1

    def _apply_text(text: str) -> None:
        # Runs on the loop: mutate job.live, then fan out the accumulated snapshot.
        if job.live is None:
            return
        job.live["english"] = job.live.get("english", "") + text
        job.publish_live({
            "type": "delta", "index": job.live.get("index"), "text": text,
            "paragraphs": _paragraph_count(job.live["english"]),
            "chunk": job.live.get("chunk", [1, 1]),
        })

    def _flush(force: bool = False) -> None:
        with lock:
            now = time.monotonic()
            if not buf:
                return
            if not force and pending["chars"] < FLUSH_CHARS \
                    and now - pending["last"] < FLUSH_SECONDS:
                return
            text = "".join(buf)
            buf.clear()
            pending["chars"] = 0
            pending["last"] = now
        loop.call_soon_threadsafe(_apply_text, text)

    def on_text(chunk: str) -> None:
        with lock:
            buf.append(chunk)
            pending["chars"] += len(chunk)
        _flush()

    def on_source(paragraphs: list[str]) -> None:
        def apply() -> None:
            if job.live is None:
                return
            job.live["source"] = paragraphs
            job.publish_live({"type": "source", "index": job.live.get("index"),
                              "source": paragraphs, "paragraphs": len(paragraphs)})
        loop.call_soon_threadsafe(apply)

    def on_reset(reason: str) -> None:
        _flush(force=True)

        def apply() -> None:
            if job.live is None:
                return
            # "retry" redoes the whole chapter, so everything streamed is void. A
            # reconnect/restart only lost the current chunk, so keep what earlier
            # chunks committed (see StreamHooks' docstring).
            job.live["english"] = "" if reason == "retry" else job.live.get("committed", "")
            job.publish_live({"type": "reset", "index": job.live.get("index"),
                              "reason": reason, "english": job.live["english"]})
        loop.call_soon_threadsafe(apply)

    def on_chunk(i: int, n: int) -> None:
        _flush(force=True)

        def apply() -> None:
            if job.live is None:
                return
            # Chunk boundary = commit point: prose from completed chunks is final.
            job.live["committed"] = job.live.get("english", "")
            job.live["chunk"] = [i, n]
            job.publish_live({"type": "chunk", "index": job.live.get("index"),
                              "chunk": [i, n]})
        loop.call_soon_threadsafe(apply)

    hooks = StreamHooks(on_source=on_source, on_text=on_text, on_reset=on_reset,
                        on_chunk=on_chunk, abort=job.abort)
    return hooks, lambda: _flush(force=True)


def _paragraph_count(text: str) -> int:
    """Complete paragraphs in the streamed English so far — drives the progress bar
    and the source-alignment highlight."""
    if not text:
        return 0
    return len([p for p in re.split(r"\n\s*\n", text) if p.strip()])


def _rate_limit_resume_at(exc: Exception, strikes: int) -> tuple[float, float | None]:
    """When to resume after a rate limit, plus the plan's own reset time if known.

    One copy of the policy, so the chapter and page paths can never drift on how long
    they wait out a limit.
    """
    resets_at = getattr(getattr(exc, "info", None), "resets_at", None)
    now = time.time()
    if resets_at and now < resets_at <= now + _MAX_WAIT:
        return resets_at + _RATE_LIMIT_BUFFER, resets_at
    # No or stale reset time (e.g. a bare 429): back off instead.
    return now + min(_FALLBACK_WAIT * (2 ** (strikes - 1)), _FALLBACK_WAIT_MAX), resets_at


def _run_page_task(kind: str, pid: str, page: dict, cfg: Config,
                   translator: Translator, hooks: StreamHooks) -> dict:
    """Run one queued page item. Blocking — always called via run_in_threadpool.

    Returns the fields to merge into the page's record in pages.json.
    """
    image = pages_mod.resolve_page_file(pid, page.get("id", ""))
    if image is None:
        raise TaskRefused("that page's image file is missing")

    if kind == TASK_OCR_VERIFY:
        text = (page.get("text") or "").strip()
        if not text:
            raise TaskRefused("there is nothing transcribed on that page to check yet")
        result = ocr.verify_page(translator, image, text, hooks)
        return {
            "verify": {
                "at": pages_mod.now_iso(),
                "verdict": result.verdict,
                "issues": [vars(i) for i in result.issues],
                "cost_usd": result.cost_usd,
            },
            "status": (pages_mod.STATUS_NEEDS_CHECK if result.verdict == "discrepancies"
                       else pages_mod.STATUS_OK),
            "_usage": result.usage,
            "_cost": result.cost_usd,
        }

    result = ocr.transcribe_page(translator, image, hint=page.get("hint") or None,
                                 hooks=hooks)
    text = result.text
    attempts = int(((page.get("ocr") or {}).get("attempts") or 0)) + 1
    return {
        "text": text,
        "raw_text": text,
        "confidence": result.confidence,
        "notes": result.notes,
        "heading": result.heading,
        "starts_mid_sentence": result.starts_mid_sentence,
        "ends_mid_sentence": result.ends_mid_sentence,
        "ends_mid_word": result.ends_mid_word,
        "hangul_fraction": round(hangul_fraction(text), 3),
        "chars": len(text),
        "hint": "",
        "verify": None,
        "error": None,
        # A page the model itself was unsure about goes to the reader, not straight
        # into the novel. Everything else is provisionally fine.
        "status": (pages_mod.STATUS_OK if result.confidence == "high"
                   else pages_mod.STATUS_NEEDS_CHECK),
        "ocr": {"at": pages_mod.now_iso(), "attempts": attempts,
                "usage": result.usage, "cost_usd": result.cost_usd},
        "_usage": result.usage,
        "_cost": result.cost_usd,
    }


def _apply_page_result(pid: str, page_id: str, fields: dict) -> dict:
    """Merge a finished page result into pages.json and return the updated record.

    OCR spend is accumulated HERE rather than in state.json: that file is keyed by
    chapter index, and a page sequence number would corrupt a real chapter's totals.
    """
    usage = fields.pop("_usage", {}) or {}
    cost = float(fields.pop("_cost", 0.0) or 0.0)
    with pages_mod.mutate_pages(pid) as doc:
        rec = pages_mod.find_page(doc, page_id)
        if rec is None:
            return {}
        rec.update(fields)
        totals = doc.setdefault("totals", {})
        totals["cost_usd"] = round(float(totals.get("cost_usd") or 0.0) + cost, 6)
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)):
                totals[key] = (totals.get(key) or 0) + value
        return dict(rec)


def _set_page_status(pid: str, page_id: str, status: str, **fields) -> None:
    with pages_mod.mutate_pages(pid) as doc:
        rec = pages_mod.find_page(doc, page_id)
        if rec is not None:
            rec["status"] = status
            rec.update(fields)


async def _run_page_item(job: Job, cfg: Config, idx: int, force: bool, kind: str,
                         translator: Translator, loop, strikes: int) -> tuple[int, str]:
    """Run one queued PAGE item on the shared worker.

    Pages ride the same Job as chapters so Stop, the live console, the Activity view
    and the rate-limit auto-resume all work without being reimplemented — but their
    bookkeeping lives in pages.json, and ``idx`` is a page sequence number, not a
    chapter index. Returns ``(strikes, outcome)`` with outcome
    ``"continue" | "break" | "return"``.
    """
    key = _queue_key(idx, kind)
    doc = pages_mod.load_pages(job.pid)
    page = next((p for p in doc.get("pages", []) if p.get("seq") == idx), None)
    if page is None:
        job.queued.discard(key)
        job.current = None
        return strikes, "continue"

    page_id = page.get("id", "")
    prior_status = page.get("status") or pages_mod.STATUS_NEW
    label = page.get("name") or f"Page {idx}"

    job.abort.clear()
    job.live = {"index": idx, "title": label, "chars": 0, "source": [], "english": "",
                "committed": "", "chunk": [1, 1], "started_at": time.time(),
                "page_id": page_id}
    hooks, flush_live = _build_hooks(job, loop)
    _set_page_status(job.pid, page_id, pages_mod.STATUS_RUNNING)
    job.publish({"type": "start", "index": idx, "title": label, "chars": 0,
                 "kind": kind, "page_id": page_id,
                 "started_at": job.live["started_at"],
                 "model": cfg.anthropic.model, "effort": cfg.anthropic.effort})

    def done(status: str, **extra) -> None:
        job.live = None
        job.current = None
        job.publish({"type": "chapter", "index": idx, "kind": kind,
                     "page_id": page_id, "title": label, "status": status, **extra})

    try:
        fields = await run_in_threadpool(
            _run_page_task, kind, job.pid, page, cfg, translator, hooks)
    except TranslationAborted:
        flush_live()
        _set_page_status(job.pid, page_id, prior_status)
        job.queued.discard(key)
        done(prior_status, aborted=True)
        return strikes, ("break" if job.cancelled else "continue")
    except RateLimitedError as exc:
        flush_live()
        job.live = None
        # Nothing was written, so the page simply goes back to what it was and is
        # re-queued at the head to be retried when the window refreshes.
        _set_page_status(job.pid, page_id, prior_status)
        job.put_back((idx, force, kind))
        job.current = None
        strikes += 1
        resume_at, resets_at = _rate_limit_resume_at(exc, strikes)
        if strikes >= _MAX_STRIKES:
            job.done = True
            job.publish({"type": "paused", "index": idx, "message": str(exc),
                         "resets_at": resets_at, "current": None,
                         "pending": [i for i, _, _ in job.snapshot_pending()]})
            return strikes, "return"
        job.waiting = {"resume_at": resume_at, "resets_at": resets_at,
                       "message": str(exc), "since": time.time()}
        job.publish({"type": "waiting", "index": idx, "message": str(exc),
                     "resets_at": resets_at, "resume_at": resume_at})
        await _sleep_until(job, resume_at)
        job.waiting = None
        if job.cancelled or not job.pending:
            return strikes, "break"
        job.publish({"type": "resumed"})
        return strikes, "continue"
    except TaskRefused as exc:
        flush_live()
        _set_page_status(job.pid, page_id, prior_status)
        job.queued.discard(key)
        done(prior_status, refused=True, error=str(exc))
        return 0, "continue"
    except Exception as exc:  # isolation: one bad page never kills the queue
        flush_live()
        _set_page_status(job.pid, page_id, pages_mod.STATUS_FAILED,
                         error=f"{type(exc).__name__}: {exc}")
        job.queued.discard(key)
        done(pages_mod.STATUS_FAILED, error=str(exc),
             explain=errors.as_dict(errors.explain(exc)))
        return 0, "continue"

    flush_live()
    rec = _apply_page_result(job.pid, page_id, fields)
    job.queued.discard(key)
    done(rec.get("status", pages_mod.STATUS_NEEDS_CHECK),
         confidence=rec.get("confidence", ""), notes=rec.get("notes", []),
         chars=rec.get("chars", 0))
    return 0, "continue"


async def _run_worker(job: Job, cfg: Config) -> None:
    loop = asyncio.get_running_loop()
    chapters = get_chapters(job.pid, cfg)
    total = _output_total(job.pid, chapters)  # match the on-disk chapter-NN.md pad width
    by_index = {c.index: c for c in chapters}
    glossary = Glossary.load(cfg.paths.glossary_json)
    state = State.load(cfg.paths.state_file)
    translator = Translator(cfg.anthropic, cfg.translation, canonical_names=glossary.canonical())

    # Drain the queue. The awaits are run_in_threadpool and the rate-limit sleep, so
    # an enqueue arriving mid-flight is always observed on a later iteration (no lost
    # work — start_translation keeps appending to this job while it waits).
    strikes = 0  # consecutive rate-limit hits; any completed chapter resets it
    while not job.cancelled:
        item = job.take_next()
        if item is None:
            break
        idx, force, kind = item
        job.current = idx
        job.kind = kind
        # A page item's index is a page sequence number, so it must NOT be looked up
        # in the chapter map below — after a build, page 5 and chapter 5 both exist.
        if kind in PAGE_TASK_KINDS:
            strikes, outcome = await _run_page_item(
                job, cfg, idx, force, kind, translator, loop, strikes)
            if outcome == "return":
                return
            if outcome == "break":
                break
            continue
        ch = by_index.get(idx)
        if ch is None or kind != TASK_TRANSLATE:
            # by_index is captured once when the worker starts; a repair queued later
            # (and the fresh source its endpoint just fetched) would otherwise be read
            # from a stale snapshot. get_chapters is cached, so this is cheap.
            chapters = get_chapters(job.pid, cfg)
            total = _output_total(job.pid, chapters)
            by_index = {c.index: c for c in chapters}
            ch = by_index.get(idx)
        if ch is None:
            job.queued.discard(_queue_key(idx, kind))
            job.current = None
            continue
        # Whether this chapter was ALREADY validated on disk before this attempt. Used
        # both to skip non-forced re-runs and to protect a good translation from being
        # clobbered if a forced re-translate is interrupted by a rate limit below.
        already_done = state.is_done(idx, ch.metrics.content_hash)
        # Only a plain translation may be skipped as already-done. A resolve or a
        # pronoun fix is explicitly requested ON an already-translated chapter, so
        # skipping it there would silently do nothing.
        if kind == TASK_TRANSLATE and not force and already_done:
            job.queued.discard(_queue_key(idx, kind))
            job.current = None
            job.publish({"type": "chapter", "index": idx, "kind": kind,
                         "status": "validated",
                         "title": ch.title, "skipped": True})
            continue
        # A fresh live buffer per chapter, so a reconnecting stream replays THIS
        # chapter's text and never the previous one's.
        job.abort.clear()
        job.live = {"index": idx, "title": ch.title, "chars": ch.metrics.char_count,
                    "source": [], "english": "", "committed": "", "chunk": [1, 1],
                    "started_at": time.time()}
        hooks, flush_live = _build_hooks(job, loop)
        job.publish({"type": "start", "index": idx, "title": ch.title,
                     "chars": ch.metrics.char_count, "kind": kind,
                     "started_at": job.live["started_at"],
                     "model": cfg.anthropic.model, "effort": cfg.anthropic.effort})
        try:
            status_val = await run_in_threadpool(
                _run_task, kind, ch, total, translator, glossary, cfg, state, hooks
            )
        except TranslationAborted:
            # A deliberate stop, not a failure. Leave a chapter that already had a good
            # translation on disk marked validated (same reasoning as the rate-limit
            # branch below); only revert one that was genuinely unfinished.
            flush_live()
            # Only a stopped TRANSLATION leaves the chapter unfinished. A stopped repair
            # never touched the saved text, so resetting it to "pending" would throw away
            # a needs-review status and its failure list for no reason.
            if kind == TASK_TRANSLATE and not already_done:
                state.update(idx, status=state_mod.STATUS_PENDING, title=ch.title)
                state = _persist_chapter_state(state, cfg.paths.state_file, idx)
            job.queued.discard(_queue_key(idx, kind))
            job.current = None
            job.live = None
            rec = state.get(idx) or {}
            job.publish({"type": "chapter", "index": idx, "kind": kind,
                         "status": rec.get("status", state_mod.STATUS_PENDING),
                         "title": ch.title, "aborted": True})
            if job.cancelled:
                break
            continue
        except RateLimitedError as exc:
            flush_live()
            job.live = None
            # A rate limit mid-flight must NOT downgrade a chapter that was already
            # validated on disk (e.g. an interrupted force-retranslate): that would
            # revert it to "pending"/"Queued" even though its finished English file
            # is still on disk. Only mark genuinely-unfinished chapters pending so
            # they resume; a done chapter keeps its validated status. A repair likewise
            # left the saved text alone, so it keeps whatever status it already had.
            if kind == TASK_TRANSLATE and not already_done:
                state.update(idx, status=state_mod.STATUS_PENDING, title=ch.title)
                state = _persist_chapter_state(state, cfg.paths.state_file, idx)
            # Put the interrupted chapter back at the head (it stays in job.queued)
            # and ride out the limit HERE — the worker stays alive and resumes by
            # itself when the plan's window refreshes, no browser needed.
            job.put_back((idx, force, kind))
            job.current = None
            strikes += 1
            resume_at, resets_at = _rate_limit_resume_at(exc, strikes)
            if strikes >= _MAX_STRIKES:
                # Something is off (limit hit right back N times in a row) — stop
                # burning retries and hand resumption to the user/client instead.
                remaining = [i for i, _, _ in job.snapshot_pending()]
                job.done = True
                job.publish({"type": "paused", "index": idx, "message": str(exc),
                             "resets_at": resets_at,
                             "current": None, "pending": remaining})
                return
            job.waiting = {"resume_at": resume_at, "resets_at": resets_at,
                           "message": str(exc), "since": time.time()}
            job.publish({"type": "waiting", "index": idx, "message": str(exc),
                         "resets_at": resets_at, "resume_at": resume_at})
            await _sleep_until(job, resume_at)
            job.waiting = None
            if job.cancelled or not job.pending:
                break  # cancelled/cleared during the wait → normal terminal 'done'
            job.publish({"type": "resumed"})
            continue
        except TaskRefused as exc:
            # Nothing was written and nothing broke: keep the chapter's current status
            # and report why, so the user sees "not applied" rather than "failed".
            flush_live()
            job.live = None
            strikes = 0
            job.queued.discard(_queue_key(idx, kind))
            job.current = None
            # The attempt was still billed even though nothing was written, so persist
            # the usage rather than silently dropping it from the novel's totals.
            state = _persist_chapter_state(state, cfg.paths.state_file, idx)
            rec = state.get(idx) or {}
            job.publish({"type": "chapter", "index": idx, "kind": kind,
                         "status": rec.get("status", state_mod.STATUS_NEEDS_REVIEW),
                         "title": ch.title, "refused": True, "error": str(exc),
                         "totals": state.totals()})
            continue
        except Exception as exc:  # isolation: one bad chapter never kills the queue
            flush_live()
            job.live = None
            strikes = 0  # Claude answered (badly) — the rate limit isn't the problem
            state.update(idx, status=state_mod.STATUS_FAILED, title=ch.title,
                         error=f"{type(exc).__name__}: {exc}")
            state = _persist_chapter_state(state, cfg.paths.state_file, idx)
            job.queued.discard(_queue_key(idx, kind))
            job.current = None
            # Carry the same plain-English explanation the HTTP layer produces, so a
            # mid-queue failure opens the identical "what went wrong / how to fix it"
            # dialog instead of dumping a raw exception string into the log.
            job.publish({"type": "chapter", "index": idx, "kind": kind,
                         "status": "failed",
                         "title": ch.title, "error": str(exc),
                         "explain": errors.as_dict(errors.explain(exc))})
            continue
        flush_live()
        job.live = None
        strikes = 0
        state = _persist_chapter_state(state, cfg.paths.state_file, idx)
        rec = state.get(idx) or {}
        job.queued.discard(_queue_key(idx, kind))
        job.current = None
        totals = state.totals()
        job.publish({"type": "chapter", "index": idx, "kind": kind,
                     "status": status_val,
                     "title": ch.title, "cost_usd": totals["cost_usd"],
                     "tokens": rec.get("usage", {}), "totals": totals,
                     "failures": rec.get("failures", [])})

    job.done = True
    job.publish({"type": "done", "totals": State.load(cfg.paths.state_file).totals(),
                 "current": None, "pending": []})


async def _run_worker_guarded(job: Job, cfg: Config) -> None:
    """Run the worker, guaranteeing it always terminates VISIBLY.

    ``_run_worker`` handles errors per item, but a few steps sit outside that try —
    persisting chapter state, applying a page result. An OSError there (on Windows,
    an antivirus holding pages.json for a moment is the realistic one) escaped and
    killed the asyncio task outright. Nothing then published a terminal event, so
    every open SSE stream sat on keep-alives forever still showing "translating"; the
    page stayed "ocr-running"; and because ``job.done`` was never set, the job could
    not be evicted either, so its history leaked for the life of the process.

    Whatever happens, this leaves the job finished, says so on the stream, and hands
    back any pages whose work will now never run.
    """
    try:
        await _run_worker(job, cfg)
    except asyncio.CancelledError:
        raise  # shutdown, not a failure — let it propagate
    except Exception as exc:  # noqa: BLE001 — a worker must never die silently
        explained = errors.explain(exc)
        errors.log_error(explained, where=f"worker/{job.pid}")
        job.publish({"type": "chapter", "index": job.current, "kind": job.kind,
                     "status": "failed", "title": "",
                     "error": str(exc), "explain": errors.as_dict(explained)})
    finally:
        job.live = None
        job.current = None
        stranded = {i for i, _f, k in job.drain() if k in PAGE_TASK_KINDS}
        if stranded:
            try:
                _release_queued_pages(job.pid, stranded)
            except Exception:  # noqa: BLE001 — cleanup must not mask the real error
                pass
        job.done = True
        if job.terminal is None:
            # A normal finish and the rate-limit give-up both publish their own
            # terminal event; this only fires when the worker died on the way there.
            job.publish({"type": "done", "totals": None, "current": None, "pending": []})


def _spawn_worker(pid: str, cfg: Config, job: Job) -> None:
    # Bound memory: a long-lived server accrues a Job per run. Drop old finished jobs,
    # keeping the active ones plus the few most recent for a late stream's replay.
    if len(_jobs) > 40:
        finished = [jid for jid, j in _jobs.items() if j.done]
        for jid in finished[:-10]:
            _jobs.pop(jid, None)
    _jobs[job.id] = job
    _active_job_by_project[pid] = job.id
    task = asyncio.create_task(_run_worker_guarded(job, cfg))
    _running_tasks.add(task)  # strong ref so the task isn't garbage-collected

    def _cleanup(t: asyncio.Task) -> None:
        _running_tasks.discard(t)
        if _active_job_by_project.get(pid) == job.id:
            _active_job_by_project.pop(pid, None)

    task.add_done_callback(_cleanup)


def _resolve_items(pid: str, cfg: Config, req: TranslateRequest) -> list[tuple[int, bool, str]]:
    if req.indices:
        return [(i, req.force, TASK_TRANSLATE) for i in req.indices]
    chapters = get_chapters(pid, cfg)
    state = State.load(cfg.paths.state_file)
    korean = [c for c in chapters if classify(c, cfg) == "korean"]
    # A forced run with no explicit indices means "re-translate this whole novel", so the
    # already-done filter must NOT apply — otherwise every validated chapter is skipped
    # and the request appears to succeed while queuing nothing. Already-English and empty
    # tabs are still excluded: forcing those has no meaning.
    if req.force:
        return [(c.index, True, TASK_TRANSLATE) for c in korean]
    return [
        (c.index, False, TASK_TRANSLATE) for c in korean
        if not state.is_done(c.index, c.metrics.content_hash)
    ]


@app.post("/api/projects/{pid}/translate")
async def start_translation(pid: str, req: TranslateRequest) -> dict:
    _, cfg = project_cfg(pid)
    # Re-fetch the live source at the moment a translation is requested, so an edited
    # Google Doc is translated AS EDITED instead of a stale cached/snapshot copy. Without
    # this, get_chapters returns the in-memory _chapter_cache captured when the project
    # was first opened, so fixing the Korean in the doc and re-translating produced the
    # SAME OLD English (the source the model saw never changed, and its content_hash never
    # moved, so the done-guard also kept skipping it). Refreshing here updates the cache
    # AND the on-disk snapshot before BOTH _resolve_items (the done-guard) and the worker
    # read it, so a changed chapter re-translates and an unchanged one still short-circuits.
    # Run off the event loop: the fetch is blocking network I/O. Falls back to the local
    # snapshot + offline flag (below) if the doc genuinely can't be fetched here.
    await run_in_threadpool(get_chapters, pid, cfg, True)
    if pid in _offline_projects:
        raise HTTPException(409, "This novel is in read-only saved mode — its source "
                            "document isn't available on this device, so it can't be "
                            "translated here.")
    items = _resolve_items(pid, cfg, req)
    return _enqueue_task(pid, cfg, items)


def _enqueue_task(pid: str, cfg: Config, items: list[tuple[int, bool, str]]) -> dict:
    """Queue work on this novel's single worker, starting one if none is running.

    MUST be called from an ``async def`` endpoint: starting a worker schedules an
    asyncio task, and a sync endpoint runs in a threadpool with no running loop.

    Every operation — translate, AI resolve, pronoun fix — comes through here, which is
    why they all appear in Activity and why only one of them can touch a novel's
    state.json at a time. A repair requested while a translation runs simply queues
    behind it rather than racing it.
    """
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


class CancelRequest(BaseModel):
    # Default false preserves the original semantics ("clear the queue, let the running
    # chapter finish"); true also stops the chapter in flight.
    stop_current: bool = False


@app.post("/api/projects/{pid}/translate/cancel")
def cancel_queue(pid: str, req: CancelRequest = CancelRequest()) -> dict:
    """Drop the not-yet-started chapters from the queue.

    With ``stop_current`` the chapter being translated right now is stopped too. That
    can't be done by killing the worker thread, so it sets ``job.abort``, which the
    translator polls between streamed messages and turns into ``TranslationAborted``.
    A stopped chapter is never marked failed and never overwrites good output.
    """
    require_project(pid)
    jid = _active_job_by_project.get(pid)
    if jid and jid in _jobs and not _jobs[jid].done:
        job = _jobs[jid]
        # Pages whose work is about to be dropped have to come off "Queued", or they
        # sit there forever and the default sweep can't see them again.
        dropped_pages = {i for i, _f, k in job.drain() if k in PAGE_TASK_KINDS}
        if dropped_pages:
            _release_queued_pages(pid, dropped_pages)
        stopped = None
        if req.stop_current:
            stopped = job.current
            job.cancelled = True     # ends the drain loop after the current chapter unwinds
            job.abort.set()          # cooperative stop inside the in-flight agent call
            job.queued.clear()
        else:
            job.queued = ({_queue_key(job.current, job.kind)}
                          if job.current is not None else set())
        job.wake.set()  # a worker waiting out a rate limit exits promptly
        return {"ok": True, "current": None if req.stop_current else job.current,
                "pending": [], "stopped": stopped}
    return {"ok": True, "current": None, "pending": [], "stopped": None}


@app.post("/api/projects/{pid}/translate/resume")
def resume_translation(pid: str) -> dict:
    """Wake a worker that is waiting out a rate limit and retry immediately."""
    require_project(pid)
    jid = _active_job_by_project.get(pid)
    if jid and jid in _jobs and not _jobs[jid].done and _jobs[jid].waiting:
        _jobs[jid].wake.set()
        return {"ok": True, "resumed": True, **_jobs[jid].queue_state()}
    return {"ok": True, "resumed": False}


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
async def stream_job(pid: str, job_id: str, request: Request) -> StreamingResponse:
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
            # Deltas are deliberately not in history (they'd evict the real events), so
            # catch a mid-chapter consumer up with one snapshot of the text so far —
            # otherwise a reload during a long chapter lands on an empty console.
            frame = job.live_frame()
            if frame is not None:
                yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
            while True:
                # Wake periodically even with no events so a client that navigated away
                # or closed the tab is detected and its subscriber queue is released —
                # otherwise this coroutine blocks forever and publish() grows it without
                # bound. The comment line doubles as a keep-alive through proxies.
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "paused"):
                    break
        finally:
            if q in job.subscribers:
                job.subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ----------------------------------------------------------------------------- static (production)
if DIST_DIR.exists():
    # Serve the built React app when present (the launcher builds it). The frontend
    # uses client-side routing (react-router / BrowserRouter), so an unknown path
    # like /novel/abc must return index.html rather than 404 — otherwise a refresh or
    # a deep link would break. Real built assets (JS/CSS/favicon) are served from disk;
    # everything else falls through to index.html. The API routes above are registered
    # first, so they always take precedence over this catch-all.
    _INDEX_HTML = DIST_DIR / "index.html"
    _DIST_RESOLVED = DIST_DIR.resolve()

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):  # noqa: D401 - simple static handler
        # Never shadow the API: an unmatched /api/* path should 404, not return HTML.
        if full_path == "api" or full_path.startswith("api/"):
            return Response(status_code=404)
        candidate = DIST_DIR / full_path
        try:
            if full_path and candidate.is_file() and candidate.resolve().is_relative_to(_DIST_RESOLVED):
                return FileResponse(str(candidate))
        except (OSError, ValueError):
            pass
        return FileResponse(str(_INDEX_HTML))

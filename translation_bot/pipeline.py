"""End-to-end orchestration: extract -> translate -> validate -> write.

Processes chapters in tab order, skipping ones already validated (unless the
source hash changed or a re-run is forced). One chapter's failure is isolated and
never crashes the run. Suspect output is never written to ``chapters/`` as if it
were good — it is flagged ``needs-review`` and kept in the audit log instead.
"""

from __future__ import annotations

import re
import traceback
from pathlib import Path

from .config import Config
from .docs_extract import Chapter, extract_chapters, fetch_document, hangul_fraction
from .glossary import Glossary, queue_new_terms
from .google_auth import build_docs_service, get_credentials
from . import state as state_mod
from .state import State
from .translator import RateLimitedError, Translator, TranslationResult
from .validate import ValidationResult, validate_translation


def _pad_width(total: int) -> int:
    return max(2, len(str(total)))


def chapter_filename(index: int, total: int) -> str:
    return f"chapter-{index:0{_pad_width(total)}d}.md"


def write_chapter_file(output_dir: Path, index: int, total: int, prose: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / chapter_filename(index, total)
    # Preserve curly quotes — no smart-quote normalization anywhere.
    path.write_text(prose.rstrip() + "\n", encoding="utf-8")
    return path


def write_audit(
    audit_dir: Path,
    chapter: Chapter,
    total: int,
    result: TranslationResult,
    validation: ValidationResult,
    status: str,
) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / chapter_filename(chapter.index, total)
    lines = [
        f"# Chapter {chapter.index} — {chapter.title}",
        "",
        f"**Status:** {status}  ",
        f"**Chunks:** {result.n_chunks}  ",
        f"**Metrics:** {validation.metrics}",
        "",
    ]
    if validation.failures:
        lines += ["**Validation failures:**", *[f"- {f}" for f in validation.failures], ""]
    if validation.warnings:
        lines += ["**Validation warnings:**", *[f"- {w}" for w in validation.warnings], ""]
    if result.warnings:
        lines += ["**Translator warnings:**", *[f"- {w}" for w in result.warnings], ""]
    lines += [
        "---",
        "",
        "## Source (Korean)",
        "",
        chapter.text,
        "",
        "---",
        "",
        "## Translation (English)",
        "",
        result.prose,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _translate_with_retry(
    translator: Translator,
    chapter: Chapter,
    glossary: Glossary,
    cfg: Config,
    state: State,
) -> tuple[TranslationResult, ValidationResult]:
    """Translate, validate, and auto-retry once on failure with the same glossary."""
    relevant = glossary.relevant_to(chapter.text)
    extra = cfg.translation.extra_instruction

    result = translator.translate_chapter(chapter, relevant, extra_instruction=extra)
    state.add_usage(chapter.index, result.usage, result.cost_usd)
    state.update(chapter.index, status=state_mod.STATUS_TRANSLATED)
    validation = validate_translation(chapter, result.prose, cfg.validation)

    if validation.ok:
        return result, validation

    # One emphatic corrective retry with the same glossary.
    retry = translator.translate_chapter(chapter, relevant, extra_instruction=extra, retry_reminder=True)
    state.add_usage(chapter.index, retry.usage, retry.cost_usd)
    retry_validation = validate_translation(chapter, retry.prose, cfg.validation)
    retry.warnings = ["[retry attempt]", *retry.warnings]
    return retry, retry_validation


def process_chapter(
    chapter: Chapter,
    total: int,
    translator: Translator,
    glossary: Glossary,
    cfg: Config,
    state: State,
) -> str:
    """Run one chapter through translate/validate/write. Returns the final status."""
    if not chapter.paragraphs:
        state.update(
            chapter.index,
            status=state_mod.STATUS_EMPTY,
            title=chapter.title,
            note="blank tab — no prose to translate",
        )
        return state_mod.STATUS_EMPTY

    if cfg.translation.skip_non_korean:
        frac = hangul_fraction(chapter.text)
        if frac < cfg.translation.min_hangul_fraction:
            state.update(
                chapter.index,
                status=state_mod.STATUS_ENGLISH,
                title=chapter.title,
                source_hash=chapter.metrics.content_hash,
                note=f"already English (hangul {frac:.0%}) — left as-is",
            )
            return state_mod.STATUS_ENGLISH

    metrics = chapter.metrics
    result, validation = _translate_with_retry(translator, chapter, glossary, cfg, state)

    status = state_mod.STATUS_VALIDATED if validation.ok else state_mod.STATUS_NEEDS_REVIEW
    write_audit(cfg.paths.audit_dir, chapter, total, result, validation, status)

    queued = 0
    if validation.ok:
        write_chapter_file(cfg.paths.output_dir, chapter.index, total, result.prose)
        queued = queue_new_terms(
            cfg.paths.glossary_pending, glossary, result.new_terms, chapter.index
        )

    state.update(
        chapter.index,
        status=status,
        title=chapter.title,
        source_hash=metrics.content_hash,
        source_chars=metrics.char_count,
        chunks=result.n_chunks,
        validation=validation.metrics,
        failures=validation.failures,
        new_terms_queued=queued,
    )
    return status


def run(
    config_path: str = "config.toml",
    *,
    force: bool = False,
    only: list[int] | None = None,
) -> dict:
    """Run the pipeline. Returns a summary dict."""
    cfg = Config.load(config_path)

    creds = get_credentials(cfg.google.credentials_file, cfg.google.token_file)
    docs = build_docs_service(creds)
    document = fetch_document(docs, cfg.google.source_doc_id)
    chapters = extract_chapters(document, flatten_child_tabs=cfg.google.flatten_child_tabs)
    total = len(chapters)

    glossary = Glossary.load(cfg.paths.glossary_json)
    state = State.load(cfg.paths.state_file)
    translator = Translator(cfg.anthropic, cfg.translation, canonical_names=glossary.canonical())

    summary = {
        "total": total, "translated": 0, "needs_review": 0,
        "failed": 0, "skipped": 0, "empty": 0, "english": 0,
    }

    for chapter in chapters:
        if only and chapter.index not in only:
            continue
        if not force and state.is_done(chapter.index, chapter.metrics.content_hash):
            summary["skipped"] += 1
            print(f"[{chapter.index}/{total}] skip (already validated): {chapter.title}")
            continue

        print(f"[{chapter.index}/{total}] translating: {chapter.title} "
              f"({chapter.metrics.char_count} chars, {chapter.metrics.paragraph_count} paras)")
        try:
            status = process_chapter(chapter, total, translator, glossary, cfg, state)
        except RateLimitedError as exc:
            # Plan usage exhausted — stop cleanly. This chapter stays not-done so a
            # later re-run retries it. No quality failure, no wasted re-billing.
            state.update(chapter.index, status=state_mod.STATUS_PENDING, title=chapter.title)
            state.save(cfg.paths.state_file)
            print(f"\n  PAUSED: {exc}")
            summary["paused"] = True
            break
        except Exception as exc:  # isolation: never let one chapter crash the run
            state.update(
                chapter.index,
                status=state_mod.STATUS_FAILED,
                title=chapter.title,
                error=f"{type(exc).__name__}: {exc}",
            )
            print(f"    ERROR: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            status = state_mod.STATUS_FAILED
        finally:
            state.save(cfg.paths.state_file)  # save after every chapter for resumability

        if status == state_mod.STATUS_VALIDATED:
            summary["translated"] += 1
            print(f"    ok -> {chapter_filename(chapter.index, total)}")
        elif status == state_mod.STATUS_NEEDS_REVIEW:
            summary["needs_review"] += 1
            print("    needs-review (failed validation after retry) — see audit/")
        elif status == state_mod.STATUS_EMPTY:
            summary["empty"] += 1
            print("    empty tab — skipped (nothing to translate)")
        elif status == state_mod.STATUS_ENGLISH:
            summary["english"] += 1
            print("    already English — skipped (no translation needed)")
        else:
            summary["failed"] += 1

    totals = state.totals()
    summary["cost_usd"] = totals["cost_usd"]
    summary["tokens"] = totals["tokens"]
    headline = "Paused (usage limit) — re-run later to continue." if summary.get("paused") else "Done."
    print(
        f"\n{headline} translated={summary['translated']} needs_review={summary['needs_review']} "
        f"failed={summary['failed']} empty={summary['empty']} english={summary['english']} "
        f"skipped={summary['skipped']} plan_usage_equiv=${summary['cost_usd']}"
    )
    pending_path = Path(cfg.paths.glossary_pending)
    if pending_path.exists() and pending_path.stat().st_size > 2:
        print("New glossary terms are queued. Run `review` to approve/edit/reject them.")
    return summary


def merge_chapters(config_path: str = "config.toml") -> Path:
    """Concatenate validated chapter files into full-novel.md with headings."""
    cfg = Config.load(config_path)
    output_dir = Path(cfg.paths.output_dir)
    files = sorted(output_dir.glob("chapter-*.md"))
    if not files:
        raise FileNotFoundError(f"No chapter files in {output_dir} to merge.")
    out = Path("full-novel.md")
    parts: list[str] = []
    for path in files:
        m = re.search(r"chapter-(\d+)", path.stem)
        n = int(m.group(1)) if m else 0
        parts.append(f"# Chapter {n}\n\n{path.read_text(encoding='utf-8').rstrip()}\n")
    out.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return out

"""End-to-end orchestration: extract -> translate -> validate -> write.

Processes chapters in tab order, skipping ones already validated (unless the
source hash changed or a re-run is forced). One chapter's failure is isolated and
never crashes the run. Suspect output is never written to ``chapters/`` as if it
were good — it is flagged ``needs-review`` and kept in the audit log instead.
"""

from __future__ import annotations

import re
import shutil
import traceback
from pathlib import Path

from .atomic import atomic_write_text
from .config import Config
from .locks import file_lock
from .docs_extract import Chapter, extract_chapters, fetch_document, hangul_fraction
from .glossary import Glossary, queue_new_terms
from .google_auth import build_docs_service, get_credentials
from .sanitize import remove_snippets, strip_export_footer, strip_reasoning, strip_source_header
from . import state as state_mod
from .state import State
from .translator import (
    RateLimitedError,
    StreamHooks,
    TranslationAborted,
    TranslationResult,
    Translator,
)
from .validate import ValidationResult, validate_translation
from .textsplit import SEP_RE


def _pad_width(total: int) -> int:
    return max(2, len(str(total)))


def chapter_filename(index: int, total: int) -> str:
    return f"chapter-{index:0{_pad_width(total)}d}.md"


def existing_chapter_file(directory: Path, index: int) -> Path | None:
    """A file holding this chapter at ANY pad width, or None.

    The canonical name is derived from a chapter COUNT, so it changes the moment a
    novel crosses a digit boundary: 99 tabs to 100 turns ``chapter-07.md`` into
    ``chapter-007.md``. Anything holding a count captured earlier — the translation
    worker keeps one for its entire run — then looks for a name that no longer matches
    what is on disk, and a finished translation goes invisible.

    Resolving by INDEX makes a read independent of the count, which is what stops a
    chapter the user already paid for from silently disappearing.
    """
    try:
        candidates = list(directory.glob("chapter-*.md"))
    except OSError:
        return None
    for f in candidates:
        tail = f.stem.split("-", 1)[-1]
        if tail.isdigit() and int(tail) == index:
            return f
    return None


def chapter_path(directory: Path, index: int, total: int) -> Path:
    """Where chapter ``index`` lives.

    The canonical name wins when it exists; a file written at any other width is
    honoured next; otherwise the canonical name is returned so a caller can create it.
    """
    canonical = directory / chapter_filename(index, total)
    if canonical.exists():
        return canonical
    return existing_chapter_file(directory, index) or canonical


def stripped_chapter(chapter: Chapter) -> Chapter:
    """The chapter as the MODEL should see it: export header and footer removed.

    Everything that judges a translation has to measure it against this, not against the
    raw tab. The paragraph and length checks compare output to source, and the raw tab
    carries three header blocks plus a closing copyright notice that were never meant to
    be translated — counting them makes a faithful translation look short.

    The server used to validate against the raw chapter while the pipeline validated
    against the stripped one, so one chapter could get two different verdicts depending on
    which asked. Both go through here now.

    Returns the original object when there is nothing to strip, so the common path
    allocates nothing.
    """
    clean = strip_export_footer(strip_source_header(chapter.text)[0])
    if not clean or clean == chapter.text:
        return chapter
    return Chapter(index=chapter.index, title=chapter.title,
                   paragraphs=[p for p in SEP_RE.split(clean) if p.strip()])


def write_chapter_file(output_dir: Path, index: int, total: int, prose: str,
                       *, snapshot: bool = True) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Resolved by INDEX. If this chapter already exists under a different pad width,
    # write to THAT file instead of creating a second one for the same chapter.
    #
    # `total` can be stale: the worker captures a chapter count at job start and keeps
    # it for its whole run, while a Refresh press or a newly added tab re-pads every
    # file underneath it. Writing at the captured width produced chapter-50.md beside
    # an already-re-padded chapter-050.md — the reader saw one, the other was
    # invisible, previous/ snapshotted neither (the canonical name did not exist, so
    # the snapshot below was skipped), and the padding normalizer then refused to
    # reconcile them because its destination was taken. A translation the user had
    # paid for, marked validated in state.json, that could be neither read nor
    # recovered.
    #
    # Renaming is deliberately NOT done here — that belongs to the padding normalizer,
    # and doing it from a stale writer would drag the file back to an older name.
    path = chapter_path(output_dir, index, total)
    # Guarded here rather than at each call site, because there are seven of them
    # (translate, repair, pronoun fix, manual save, auto-fix, accept, consistency
    # rename) and one that forgets silently loses a whole translation: two writers
    # each read, each apply their own change, and the last os.replace wins. The lock
    # is re-entrant, so a caller doing read-modify-write can hold it across all three
    # steps and still call in here.
    with file_lock(path):
        return _write_chapter_locked(path, output_dir, index, total, prose, snapshot)


def _write_chapter_locked(path: Path, output_dir: Path, index: int, total: int,
                          prose: str, snapshot: bool) -> Path:
    # Before overwriting an existing translation, snapshot it to a sibling ``previous/``
    # folder so the reader can show old-vs-new and offer a one-click revert. Kept OUTSIDE
    # ``chapters/`` so it never matches the ``chapter-*.md`` globs used by scans/exports.
    #
    # ``snapshot=False`` is for edits that keep their OWN history — a per-paragraph
    # rewrite stores every version in ``variants/``, and letting each pick overwrite
    # the single ``previous/`` slot would destroy the whole-chapter snapshot taken
    # before the last translation or AI resolve after just one click.
    if snapshot and path.exists():
        prev_dir = output_dir.parent / "previous"
        prev_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, prev_dir / chapter_filename(index, total))
        except OSError:
            pass  # a missing backup must never block writing the real translation
    # Preserve curly quotes — no smart-quote normalization anywhere. Written atomically
    # so a crash or a concurrent reader never observes a half-written chapter.
    atomic_write_text(path, prose.rstrip() + "\n")
    return path


def previous_chapter_path(output_dir: Path, index: int, total: int) -> Path:
    """Path of the retained prior translation (sibling ``previous/`` folder).

    Resolved by index: a snapshot taken while the novel had a different chapter count
    carries that count's pad width, and looking for today's width would report "no
    previous version" for a snapshot that is sitting right there.
    """
    return chapter_path(output_dir.parent / "previous", index, total)


# The audit file records source + translation under fixed headings (see write_audit).
# It is the ONLY place a needs-review chapter's translation is saved, so this is how
# the app recovers that prose for display, for Accept, and for the pronoun repair.
_AUDIT_TRANSLATION_MARK = "## Translation (English)"


def read_audit_translation(audit_dir: Path, index: int, total: int) -> str | None:
    """Recover a chapter's translated prose from its audit copy.

    A needs-review chapter is written ONLY to audit/ (never chapters/), so without this
    its finished translation is invisible in the app and un-acceptable. The audit is a
    fixed-format doc — source, then the translation under a known heading — so the prose is
    everything after that heading."""
    try:
        # By index, not by count — the audit copy was written with whatever pad width
        # was current at the time, and this is the ONLY copy of a needs-review
        # chapter's translation.
        text = chapter_path(audit_dir, index, total).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text or _AUDIT_TRANSLATION_MARK not in text:
        return None
    prose = text.rsplit(_AUDIT_TRANSLATION_MARK, 1)[-1].strip()
    return prose or None


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
    # Atomic: a chapter that fails validation is written ONLY here, never to
    # chapters/, so this file is the sole copy of that translation and the only thing
    # Accept, Fix-pronouns and the paragraph panel can recover it from. A crash or an
    # antivirus lock partway through a plain write_text would lose it outright.
    atomic_write_text(path, "\n".join(lines))
    return path


def _translate_with_retry(
    translator: Translator,
    chapter: Chapter,
    glossary: Glossary,
    cfg: Config,
    state: State,
    hooks: StreamHooks | None = None,
) -> tuple[TranslationResult, ValidationResult]:
    """Translate, validate, and auto-retry once on failure with the same glossary."""
    relevant = glossary.relevant_to(chapter.text)
    extra = cfg.translation.extra_instruction

    result = translator.translate_chapter(chapter, relevant, extra_instruction=extra, hooks=hooks)
    state.add_usage(chapter.index, result.usage, result.cost_usd)
    state.update(chapter.index, status=state_mod.STATUS_TRANSLATED)
    validation = validate_translation(chapter, result.prose, cfg.validation, relevant)

    if validation.ok:
        return result, validation

    # One emphatic corrective retry with the same glossary. The first attempt's text
    # was already streamed to any watcher, so tell it to discard and start over.
    if hooks is not None:
        hooks.reset("retry")
    retry = translator.translate_chapter(chapter, relevant, extra_instruction=extra,
                                         retry_reminder=True, hooks=hooks)
    state.add_usage(chapter.index, retry.usage, retry.cost_usd)
    retry_validation = validate_translation(chapter, retry.prose, cfg.validation, relevant)
    retry.warnings = ["[retry attempt]", *retry.warnings]
    return retry, retry_validation


def _deep_check_and_fix(
    translator: Translator,
    chapter: Chapter,
    result: TranslationResult,
    validation: ValidationResult,
    cfg: Config,
    glossary=None,
) -> tuple[TranslationResult, ValidationResult]:
    """Have Claude read the finished translation and strip any non-story text the fast
    checks can't pattern-match. Runs as a check -> fix -> re-check loop, so a second AI
    pass only happens when the first found (and removed) something. Never blocks the
    pipeline: any deep-check failure leaves the chapter exactly as translated."""
    for _ in range(2):
        try:
            snippets = translator.find_meta_leaks(result.prose)
        except TranslationAborted:
            raise  # a user stop must propagate, not be swallowed as a failed deep check
        except Exception:  # noqa: BLE001 — the deep check is a bonus layer, never fatal
            break
        if not snippets:
            break
        cleaned, n_snip = remove_snippets(result.prose, snippets)
        cleaned, removed = strip_reasoning(cleaned)
        if not cleaned.strip() or cleaned == result.prose:
            break  # nothing actually removable -> stop (and don't blank the chapter)
        result.prose = cleaned
        result.warnings = [*result.warnings, f"deep-check removed {n_snip + len(removed)} leak block(s)"]
        validation = validate_translation(chapter, cleaned, cfg.validation, glossary)
    return result, validation


def process_chapter(
    chapter: Chapter,
    total: int,
    translator: Translator,
    glossary: Glossary,
    cfg: Config,
    state: State,
    hooks: StreamHooks | None = None,
) -> str:
    """Run one chapter through translate/validate/write. Returns the final status.

    ``hooks`` is optional live progress (the web worker uses it to stream the English
    into the UI and to honour a user stop); the CLI passes nothing and behaves as before.
    """
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

    metrics = chapter.metrics  # ORIGINAL — keeps the resumable "done" fingerprint stable
    # Strip the export cruft (URL, title, "N minutes", the repeated chapter number, and
    # the closing copyright notice) before the model sees it, so none of it can leak into
    # the translation. In-story part markers survive — see stripped_chapter. The state
    # hash stays on the original, so existing chapters aren't redone.
    src = stripped_chapter(chapter)

    # Publish the source AS THE MODEL SEES IT (post header-strip), so a live view lines
    # the Korean up against the English instead of showing cruft the model never got.
    if hooks is not None:
        hooks.source(src.paragraphs)

    result, validation = _translate_with_retry(translator, src, glossary, cfg, state, hooks)

    # Optional AI deep-check layer (off | flagged | always). "flagged" only spends a
    # call on chapters the fast checks already rejected; "always" checks every chapter.
    mode = getattr(cfg.translation, "deep_check", "flagged")
    if mode == "always" or (mode == "flagged" and not validation.ok):
        result, validation = _deep_check_and_fix(translator, src, result, validation, cfg, glossary.relevant_to(src.text))

    status = state_mod.STATUS_VALIDATED if validation.ok else state_mod.STATUS_NEEDS_REVIEW
    write_audit(cfg.paths.audit_dir, src, total, result, validation, status)

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
        pronoun_conflicts=validation.pronoun_conflicts,
        new_terms_queued=queued,
    )
    return status


def repair_chapter(
    chapter: Chapter,
    total: int,
    translator: Translator,
    glossary: Glossary,
    cfg: Config,
    state: State,
    *,
    instruction: str,
) -> str:
    """Re-translate one chapter with a failure-targeted corrective instruction, and
    ALWAYS write the result (unlike :func:`process_chapter`, which discards a still-
    imperfect attempt). This backs the user-triggered "AI resolve" on a flagged chapter:
    the new attempt is always written — snapshotting the prior one to ``previous/`` — so
    the reader can compare old-vs-new and revert if the fix turns out worse.
    Returns the final status."""
    if not chapter.paragraphs:
        state.update(chapter.index, status=state_mod.STATUS_EMPTY, title=chapter.title)
        return state_mod.STATUS_EMPTY

    metrics = chapter.metrics  # ORIGINAL hash — keeps the resumable fingerprint stable
    src = stripped_chapter(chapter)

    # Layer the corrective instruction on top of any per-novel one (cfg is a per-request copy).
    base = (cfg.translation.extra_instruction or "").strip()
    cfg.translation.extra_instruction = f"{base}\n\n{instruction}".strip() if base else instruction

    result, validation = _translate_with_retry(translator, src, glossary, cfg, state)
    mode = getattr(cfg.translation, "deep_check", "flagged")
    if mode == "always" or (mode == "flagged" and not validation.ok):
        result, validation = _deep_check_and_fix(translator, src, result, validation, cfg, glossary.relevant_to(src.text))

    status = state_mod.STATUS_VALIDATED if validation.ok else state_mod.STATUS_NEEDS_REVIEW
    write_audit(cfg.paths.audit_dir, src, total, result, validation, status)
    write_chapter_file(cfg.paths.output_dir, chapter.index, total, result.prose)  # always
    queued = queue_new_terms(cfg.paths.glossary_pending, glossary, result.new_terms, chapter.index)
    state.update(
        chapter.index,
        status=status,
        title=chapter.title,
        source_hash=metrics.content_hash,
        source_chars=metrics.char_count,
        chunks=result.n_chunks,
        validation=validation.metrics,
        failures=validation.failures,
        pronoun_conflicts=validation.pronoun_conflicts,
        new_terms_queued=queued,
    )
    return status


# --------------------------------------------------------------------------- pronoun repair
# Every pronoun token the repair pass is allowed to touch. Used to build a "skeleton"
# of the text with all pronouns blanked out: if the skeleton is unchanged, the model
# rewrote pronouns and nothing else, which is exactly the contract.
_PRONOUN_TOKEN_RE = re.compile(
    r"\b(?:he|him|his|himself|she|her|hers|herself|"
    r"they|them|their|theirs|themselves)\b", re.I)


def _pronoun_skeleton(text: str) -> str:
    """``text`` with every pronoun blanked and whitespace flattened.

    Whitespace is normalised because a model re-emitting a chapter often reflows a
    soft-wrapped line without changing a single word; that is harmless. Anything
    else that differs means real prose was altered.
    """
    return re.sub(r"\s+", " ", _PRONOUN_TOKEN_RE.sub("\x00", text)).strip()


def pronouns_only_changed(before: str, after: str) -> bool:
    """True when ``after`` differs from ``before`` in pronoun tokens ONLY.

    This is the safety net for the AI pronoun repair. The model is told not to touch
    anything but pronouns, but "told not to" is not a guarantee — and silently
    replacing a chapter the user has read (or hand-edited) with a re-written one
    would be far worse than not fixing the pronouns at all.
    """
    return _pronoun_skeleton(before) == _pronoun_skeleton(after)


def current_translation(cfg: Config, index: int, total: int) -> str | None:
    """The chapter's translated prose, wherever it currently lives.

    A chapter that failed validation is written ONLY to ``audit/`` — never to
    ``chapters/`` (see :func:`process_chapter`). Mis-gendered chapters are exactly
    that case, so the pronoun repair has to look in both places or it would find
    nothing to fix on the chapters that need it most.
    """
    # Resolved by index, not by the count: the worker can have written this chapter
    # under a different pad width than the caller's `total` implies.
    path = chapter_path(cfg.paths.output_dir, index, total)
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return read_audit_translation(cfg.paths.audit_dir, index, total)


def fix_pronouns_chapter(
    chapter: Chapter,
    total: int,
    translator: Translator,
    glossary: Glossary,
    cfg: Config,
    state: State,
    *,
    conflicts: list[dict],
    hooks: StreamHooks | None = None,
) -> str:
    """Correct the pronouns of mis-gendered characters IN PLACE, without re-translating.

    A wrong-gender chapter is usually a good translation with one wrong assumption in
    it, so re-translating from Korean (what "AI resolve" does) throws away sound prose
    and re-rolls every other decision. This sends the finished English back with the
    glossary's authoritative pronouns and accepts the result only if nothing but
    pronouns moved.

    The prior version is snapshotted to ``previous/`` by :func:`write_chapter_file`, so
    the reader's compare/revert works exactly as it does after an AI resolve.
    Returns the resulting status.
    """
    if not conflicts:
        return (state.get(chapter.index) or {}).get("status", state_mod.STATUS_NEEDS_REVIEW)

    before = current_translation(cfg, chapter.index, total)
    if not before or not before.strip():
        raise ValueError("This chapter has no saved translation to correct yet.")

    if hooks is not None:
        # Show the text being corrected in the live console's left-hand column, the
        # same slot the Korean source occupies during a translation.
        hooks.source([p for p in SEP_RE.split(before) if p.strip()])

    after, usage, cost = translator.fix_pronouns(before, conflicts, hooks=hooks)
    if usage:
        state.add_usage(chapter.index, usage, cost)

    if not pronouns_only_changed(before, after):
        # The model edited prose, not just pronouns. Leave the chapter untouched and
        # say so — a silent partial rewrite is the one outcome we must never produce.
        raise ValueError(
            "The AI changed more than the pronouns, so nothing was written. "
            "The chapter is exactly as it was — try AI resolve instead."
        )

    if after.strip() == before.strip():
        # Nothing was altered. The model is told to leave a pronoun alone when it can't
        # be sure who it belongs to, so this means the flagged pronouns read correctly
        # to it. Rewriting the file with identical text and re-running the same check
        # would just re-raise the same flag, leaving the user pressing a button that
        # visibly does nothing — so report the dead end instead.
        raise ValueError(
            "The AI found no pronouns it could safely change: the ones flagged here "
            "look correct in context, so they most likely belong to another character. "
            "Use “Mark fine” to clear the flag, or “AI resolve” to re-translate."
        )

    write_chapter_file(cfg.paths.output_dir, chapter.index, total, after)
    # stripped_chapter, like every other judge (process_chapter, _chapter_problems,
    # _recheck_saved, apply_paragraph). Measuring against the RAW tab counts the
    # Google-Docs export header and closing copyright notice as source, which
    # inflates the source length and drives the ratio under the floor — so a chapter
    # whose pronouns had just been repaired successfully was written to disk AND
    # flagged needs-review for a failure that wasn't real.
    source = stripped_chapter(chapter)
    validation = validate_translation(source, after, cfg.validation,
                                      glossary.relevant_to(source.text))
    status = state_mod.STATUS_VALIDATED if validation.ok else state_mod.STATUS_NEEDS_REVIEW
    state.update(
        chapter.index,
        status=status,
        title=chapter.title,
        source_hash=chapter.metrics.content_hash,
        validation=validation.metrics,
        failures=validation.failures,
        pronoun_conflicts=validation.pronoun_conflicts,
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

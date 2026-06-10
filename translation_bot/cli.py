"""Command-line interface.

Commands:
  auth      Run the Google OAuth flow and cache the token.
  extract   Fetch the doc and report chapters/metrics (optionally dump source text).
  run       Translate all pending chapters (resumable). --force / --only N,N
  review    Human gate for newly proposed glossary terms (approve/edit/reject).
  merge     Concatenate validated chapters into full-novel.md.
  status    Show per-chapter state, cost, and token usage.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from .config import Config
from .docs_extract import extract_chapters, fetch_document
from .glossary import (
    VALID_TYPES,
    Glossary,
    GlossaryEntry,
    load_pending,
    save_pending,
)
from .google_auth import build_docs_service, get_credentials
from . import pipeline
from .state import State


def _cmd_auth(args) -> int:
    cfg = Config.load(args.config)
    get_credentials(cfg.google.credentials_file, cfg.google.token_file)
    print(f"Authorized. Token cached at {cfg.google.token_file}.")
    return 0


def _cmd_extract(args) -> int:
    cfg = Config.load(args.config)
    creds = get_credentials(cfg.google.credentials_file, cfg.google.token_file)
    docs = build_docs_service(creds)
    document = fetch_document(docs, cfg.google.source_doc_id)
    chapters = extract_chapters(document, flatten_child_tabs=cfg.google.flatten_child_tabs)
    print(f"{len(chapters)} chapter(s) found:\n")
    print(f"{'#':>3}  {'paras':>5}  {'dialog':>6}  {'chars':>7}  title")
    for ch in chapters:
        m = ch.metrics
        print(f"{ch.index:>3}  {m.paragraph_count:>5}  {m.dialogue_count:>6}  "
              f"{m.char_count:>7}  {ch.title}")
    if args.save:
        out = Path(args.save)
        out.mkdir(parents=True, exist_ok=True)
        width = max(2, len(str(len(chapters))))
        for ch in chapters:
            (out / f"chapter-{ch.index:0{width}d}.txt").write_text(ch.text, encoding="utf-8")
        print(f"\nSource text written to {out}/")
    return 0


def _cmd_run(args) -> int:
    only = None
    if args.only:
        only = [int(x) for x in args.only.split(",") if x.strip()]
    pipeline.run(args.config, force=args.force, only=only)
    return 0


def _cmd_merge(args) -> int:
    out = pipeline.merge_chapters(args.config)
    print(f"Merged -> {out}")
    return 0


def _cmd_status(args) -> int:
    cfg = Config.load(args.config)
    state = State.load(cfg.paths.state_file)
    if not state.chapters:
        print("No state yet. Run `run` first.")
        return 0
    print(f"{'#':>3}  {'status':<13}  {'chunks':>6}  {'plan~$':>8}  title")
    for key in sorted(state.chapters, key=lambda k: int(k)):
        rec = state.chapters[key]
        print(f"{key:>3}  {rec.get('status', '?'):<13}  {rec.get('chunks', 0):>6}  "
              f"{rec.get('cost_usd', 0.0):>8.4f}  {rec.get('title', '')}")
        for f in rec.get("failures", []):
            print(f"        ! {f}")
        if rec.get("error"):
            print(f"        ! {rec['error']}")
    totals = state.totals()
    print(f"\nPlan usage (equivalent, not billed to you): ~${totals['cost_usd']}   "
          f"tokens: {totals['tokens']}")
    return 0


def _prompt(label: str, default: str = "") -> str:
    val = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    return val or default


def _cmd_review(args) -> int:
    cfg = Config.load(args.config)
    glossary = Glossary.load(cfg.paths.glossary_json)
    pending = load_pending(cfg.paths.glossary_pending)
    if not pending:
        print("No pending glossary terms to review.")
        return 0

    remaining: list[dict] = []
    approved = 0
    print(f"{len(pending)} pending term(s). [a]pprove  [e]dit  [r]eject  [s]kip  [q]uit\n")
    quit_early = False
    for i, item in enumerate(pending):
        if quit_early:
            remaining.append(item)
            continue
        ko, en = item.get("korean", ""), item.get("english", "")
        typ, note = item.get("type", "other"), item.get("note", "")
        conflict = item.get("conflict_with")
        chap = item.get("chapter", "?")
        print(f"[{i + 1}/{len(pending)}] {ko} -> {en}  ({typ})  ch.{chap}")
        if note:
            print(f"      note: {note}")
        if conflict:
            print(f"      ⚠ CONFLICT: glossary already has {ko} -> {conflict}")

        while True:
            choice = input("  a/e/r/s/q> ").strip().lower()
            if choice in {"a", "e", "r", "s", "q"}:
                break
            print("  please enter a, e, r, s, or q")

        if choice == "q":
            quit_early = True
            remaining.append(item)
            continue
        if choice == "s":
            remaining.append(item)
            continue
        if choice == "r":
            continue  # drop it
        if choice == "e":
            en = _prompt("  english", en)
            typ = _prompt("  type (name|place|skill|term|other)", typ)
            if typ not in VALID_TYPES:
                typ = "other"
            note = _prompt("  note", note)
        # approve (a or edited e)
        glossary.add(GlossaryEntry(korean=ko, english=en, type=typ, note=note))
        approved += 1

    glossary.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    save_pending(cfg.paths.glossary_pending, remaining)
    print(f"\nApproved {approved}. {len(remaining)} still pending. "
          f"Glossary saved ({len(glossary.entries())} entries).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="translation_bot", description=__doc__)
    p.add_argument("--config", default="config.toml", help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="run Google OAuth and cache the token").set_defaults(func=_cmd_auth)

    pe = sub.add_parser("extract", help="report chapters/metrics")
    pe.add_argument("--save", metavar="DIR", help="also dump per-chapter source text to DIR")
    pe.set_defaults(func=_cmd_extract)

    pr = sub.add_parser("run", help="translate pending chapters (resumable)")
    pr.add_argument("--force", action="store_true", help="re-translate even if already validated")
    pr.add_argument("--only", metavar="N,N", help="only these chapter numbers")
    pr.set_defaults(func=_cmd_run)

    sub.add_parser("review", help="approve/edit/reject queued glossary terms").set_defaults(
        func=_cmd_review
    )
    sub.add_parser("merge", help="concatenate chapters into full-novel.md").set_defaults(
        func=_cmd_merge
    )
    sub.add_parser("status", help="show per-chapter state and cost").set_defaults(func=_cmd_status)
    return p


def _force_utf8_stdio() -> None:
    """Korean titles and curly quotes are printed to the console; a cp1252
    Windows console would raise UnicodeEncodeError. Reconfigure to UTF-8."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

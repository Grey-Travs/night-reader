#!/usr/bin/env python
"""Renumber chapter-NN.md files when the source document's tab count changes width.

Chapter files are written as ``chapter-{index:0{width}d}.md`` where the width
comes from the total chapter count (``max(2, len(str(total)))``). So a novel
translated while its doc had 96 tabs is written ``chapter-01.md``; if the doc
later grows to 100 tabs the app starts looking for ``chapter-001.md``, finds
nothing, and shows every chapter as "not translated yet" over the Korean
source — even though the translations are sitting right there.

This renames the files in ``chapters/``, ``previous/`` and ``audit/`` to the
width the app now expects. It is a dry run by default.

    python tools/fix_chapter_padding.py                 # show what would change
    python tools/fix_chapter_padding.py --apply         # rename
    python tools/fix_chapter_padding.py --project ID    # just one novel
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

PROJECTS = pathlib.Path(__file__).resolve().parent.parent / "projects"
SUBDIRS = ("chapters", "previous", "audit")


def pad_width(total: int) -> int:
    """Mirror of translation_bot.pipeline._pad_width."""
    return max(2, len(str(total)))


def expected_total(pdir: pathlib.Path) -> int:
    """The total the app will use: the source tab count, or the recorded count."""
    total = 0
    try:
        meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
        total = int(meta.get("chapter_count") or 0)
    except (OSError, ValueError, TypeError):
        pass
    try:
        src = json.loads((pdir / "source.json").read_text(encoding="utf-8"))
        chapters = src.get("chapters", src) if isinstance(src, dict) else src
        total = max(total, len(chapters))
    except (OSError, ValueError, TypeError):
        pass
    return total


def plan(pdir: pathlib.Path) -> tuple[int, list[tuple[pathlib.Path, pathlib.Path]]]:
    """Return (width, [(src, dst), ...]) for every misnamed file."""
    total = expected_total(pdir)
    if not total:
        return 0, []
    width = pad_width(total)
    moves: list[tuple[pathlib.Path, pathlib.Path]] = []
    for sub in SUBDIRS:
        d = pdir / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("chapter-*.md")):
            tail = f.stem.split("-", 1)[-1]
            if not tail.isdigit() or len(tail) == width:
                continue
            moves.append((f, d / f"chapter-{int(tail):0{width}d}.md"))
    return width, moves


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", action="append", help="project id (default: all)")
    ap.add_argument("--apply", action="store_true", help="perform the renames")
    args = ap.parse_args()

    ids = args.project or [p.name for p in sorted(PROJECTS.iterdir())
                           if p.is_dir() and not p.name.startswith("_")]

    total_moves = 0
    for pid in ids:
        pdir = PROJECTS / pid
        if not (pdir / "project.json").exists():
            continue
        width, moves = plan(pdir)
        if not width:
            print(f"{pid}: cannot determine chapter count — skipped")
            continue
        if not moves:
            print(f"{pid}: OK ({width}-digit names)")
            continue

        # A rename must never clobber an existing translation.
        clashes = [(s, d) for s, d in moves if d.exists() and d not in
                   {m[0] for m in moves}]
        if clashes:
            print(f"{pid}: REFUSING — target already exists, e.g. "
                  f"{clashes[0][1].name}")
            continue

        print(f"{pid}: {len(moves)} files -> {width}-digit names")
        for s, d in moves[:3]:
            print(f"    {s.parent.name}/{s.name}  ->  {d.name}")
        if len(moves) > 3:
            print(f"    ... and {len(moves) - 3} more")
        total_moves += len(moves)

        if args.apply:
            for s, d in moves:
                s.rename(d)

    print()
    if args.apply:
        print(f"RENAMED {total_moves} files.")
    else:
        print(f"DRY RUN — {total_moves} files would be renamed. "
              f"Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Put a project's finished translations back on the tabs they were made from.

Why this is needed
------------------
A chapter's index is its tab's POSITION in the Google Doc, re-derived on every read
(``docs_extract`` enumerates the tabs; ``text_source.records_to_chapters`` rebuilds the
index from list position). Everything a translation costs money for is filed under that
index -- ``chapters/chapter-NN.md``, ``previous/``, ``audit/``, and the per-chapter record
in ``state.json``.

So inserting tabs anywhere but the END silently renames every chapter after them. Measured
on a real novel: 10 chapters were added to the FRONT of a 56-tab document, and all 56
finished translations were left one tab short of their own source. Every one of them then
read as stale -- ``source_changed`` compares the recorded hash against whatever now sits at
that index -- so the library offered to translate 56 chapters that were already paid for,
at roughly $0.50 each.

Nothing was lost in that case, and nothing is lost in general: the files are intact, they
are simply filed under the wrong number.

What it does
------------
It does NOT take an offset. An offset is a guess, and a wrong one would file 56 chapters
under 56 wrong numbers. Instead it matches each recorded translation to the tab that now
holds the exact source text it was translated FROM, using the same ``content_hash`` the app
already stores in ``state.json`` as ``source_hash``. The mapping is therefore evidence, not
arithmetic, and it handles any rearrangement -- a block inserted in the middle, tabs
reordered -- not only a uniform shift.

It refuses rather than guesses when:

* a recorded translation's source is not in the document at all (its tab was deleted or
  edited -- realigning would file it against text it was not made from);
* one source text appears on two tabs, so "where does this belong" has two answers;
* the resulting mapping is not one-to-one.

Nothing is deleted and nothing is overwritten: files move through a staging folder, so any
rearrangement is applied without a half-done state in which two chapters claim one name.

**Close Night Reader first.** This is a second process, so the app's in-process file locks
mean nothing to it, and renaming a chapter file out from under a running translation worker
is exactly the race those locks exist to prevent. ``--apply`` refuses while the app answers
on its port.

Dry run by default::

    python tools/realign_chapter_indices.py --project e4f57211a93c
    python tools/realign_chapter_indices.py --project e4f57211a93c --apply
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from translation_bot.text_source import records_to_chapters  # noqa: E402

PROJECTS = pathlib.Path(__file__).resolve().parent.parent / "projects"

# Every folder whose filenames are keyed on the chapter index. previous/ and audit/ matter
# as much as chapters/: a needs-review chapter is saved ONLY in audit/, so that copy is the
# one the reader serves, and previous/ is what Revert restores.
INDEXED_DIRS = ("chapters", "previous", "audit")
STAGING = ".realigning"


def app_is_running(port: int = 8000) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def source_hashes(pdir: pathlib.Path) -> dict[str, list[int]]:
    """``{content hash: [tab index, ...]}`` for the document as it reads right now."""
    sf = pdir / "source.json"
    if not sf.exists():
        raise SystemExit(f"{pdir.name}: no source.json — open the novel once to read it")
    records = json.loads(sf.read_text(encoding="utf-8-sig"))
    out: dict[str, list[int]] = {}
    for ch in records_to_chapters(records):
        out.setdefault(ch.metrics.content_hash, []).append(ch.index)
    return out


def plan(pdir: pathlib.Path) -> tuple[dict[int, int], list[str]]:
    """``({old index: new index}, problems)`` — only entries that actually move."""
    state = json.loads((pdir / "state.json").read_text(encoding="utf-8"))
    records = state.get("chapters") or {}
    hashes = source_hashes(pdir)

    moves: dict[int, int] = {}
    problems: list[str] = []
    for key in sorted((k for k in records if str(k).isdigit()), key=int):
        old = int(key)
        digest = (records[key] or {}).get("source_hash") or ""
        if not digest:
            problems.append(f"chapter {old}: no source_hash recorded — cannot place it")
            continue
        where = hashes.get(digest) or []
        if not where:
            problems.append(
                f"chapter {old}: the text it was translated from is not in the document "
                f"any more (tab deleted or edited) — left alone")
            continue
        if len(where) > 1:
            problems.append(
                f"chapter {old}: that exact source text is on tabs {where} — ambiguous")
            continue
        if where[0] != old:
            moves[old] = where[0]

    if len(set(moves.values())) != len(moves):
        problems.append("two chapters would land on one index — refusing")
        moves = {}
    return moves, problems


def _files_by_index(directory: pathlib.Path) -> dict[int, pathlib.Path]:
    out: dict[int, pathlib.Path] = {}
    if not directory.is_dir():
        return out
    for f in directory.glob("chapter-*.md"):
        stem = f.stem.split("-", 1)[-1]
        if stem.isdigit():
            out[int(stem)] = f
    return out


def apply_moves(pdir: pathlib.Path, moves: dict[int, int], width: int) -> list[str]:
    """Move every indexed file, via a staging folder so nothing is half-renamed."""
    done: list[str] = []
    for sub in INDEXED_DIRS:
        d = pdir / sub
        present = _files_by_index(d)
        staged = d / STAGING
        moving = {old: new for old, new in moves.items() if old in present}
        if not moving:
            continue
        staged.mkdir(exist_ok=True)
        # Out first, ALL of them, then back. Renaming in place would need an order that
        # exists only for a pure shift; going through staging works for any permutation.
        for old, new in moving.items():
            shutil.move(str(present[old]), str(staged / f"chapter-{new:0{width}d}.md"))
        for f in sorted(staged.glob("chapter-*.md")):
            dest = d / f.name
            if dest.exists():
                raise SystemExit(f"{dest} already exists — stopped with files in {staged}")
            shutil.move(str(f), str(dest))
        staged.rmdir()
        done.append(f"{sub}: {len(moving)} file(s)")
    return done


def apply_state(pdir: pathlib.Path, moves: dict[int, int]) -> None:
    path = pdir / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    records = state.get("chapters") or {}
    rebuilt = {}
    for key, value in records.items():
        if str(key).isdigit() and int(key) in moves:
            rebuilt[str(moves[int(key)])] = value
        else:
            rebuilt[str(key)] = value
    state["chapters"] = {k: rebuilt[k] for k in sorted(rebuilt, key=lambda x: int(x)
                                                       if str(x).isdigit() else 0)}
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def verify(pdir: pathlib.Path) -> list[str]:
    """Every recorded translation now sits on the tab it was translated from."""
    state = json.loads((pdir / "state.json").read_text(encoding="utf-8"))
    records = state.get("chapters") or {}
    hashes = source_hashes(pdir)
    bad = []
    for key in sorted((k for k in records if str(k).isdigit()), key=int):
        digest = (records[key] or {}).get("source_hash") or ""
        where = hashes.get(digest) or []
        if where and where != [int(key)]:
            bad.append(f"chapter {key} still points at tab(s) {where}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="project id under projects/")
    ap.add_argument("--apply", action="store_true", help="write (backs up first)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    pdir = PROJECTS / args.project
    if not pdir.is_dir():
        print(f"no project at {pdir}")
        return 1

    moves, problems = plan(pdir)
    for p in problems:
        print(f"  ! {p}")
    if not moves:
        print("\nNothing to realign — every translation already sits on its own tab.")
        return 0 if not problems else 1

    shifts = {new - old for old, new in moves.items()}
    print(f"\n{len(moves)} chapter(s) would move"
          + (f", every one by {shifts.pop():+d}" if len(shifts) == 1 else ""))
    for old in sorted(moves)[:5]:
        print(f"    chapter-{old:02d}  ->  chapter-{moves[old]:02d}")
    if len(moves) > 5:
        print(f"    … and {len(moves) - 5} more")

    if not args.apply:
        print("\nre-run with --apply to move them (the project is copied first)")
        return 0

    if app_is_running(args.port):
        print(f"\nNight Reader is answering on port {args.port}. Close it first: this "
              "renames files a running translation worker may be writing.")
        return 1

    backup = pdir.parent / f"{pdir.name}.before-realign"
    if backup.exists():
        print(f"\n{backup} already exists — move it aside first")
        return 1
    shutil.copytree(pdir, backup)
    print(f"\ncopied the project to {backup.name}")

    width = max(2, len(str(max(max(moves.values()), max(moves)))))
    for line in apply_moves(pdir, moves, width):
        print(f"    moved {line}")
    apply_state(pdir, moves)
    print("    rewrote state.json")

    bad = verify(pdir)
    if bad:
        print("\nVERIFY FAILED — the copy above still holds the original:")
        for b in bad:
            print(f"    {b}")
        return 1
    print("\nVerified: every recorded translation now sits on the tab it was made from.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

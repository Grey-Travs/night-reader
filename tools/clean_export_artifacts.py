#!/usr/bin/env python
"""Remove RIDI export cruft from chapters that were translated before it was stripped.

A source tab arrives wrapped in boilerplate::

    ridibooks.com/books/2847008759/view
    체리, 팝! (CHERRY POP!) 29화
    4-5 minutes
    29.                    <- the chapter number, repeated as its own block
    …the actual chapter…
    ※ 본 저작물의 권리는 저작권자에게 있습니다…   <- closing copyright notice

The first three were always stripped before the model saw them. The bare number was
deliberately kept, and nothing removed the notice, so both were translated and saved into
the chapter files. Neither is visible in the reader — a lone "29." is an *empty ordered
list item* in Markdown and the reading CSS gives lists no styling, so it takes up no
space — but both are still in the file, and they turn up the moment a chapter is copied,
edited, downloaded or exported.

``sanitize.strip_source_header``/``strip_export_footer`` now stop both at the source, so
new translations are clean. This repairs the ones already on disk.

Two rules, both deliberately conservative — this rewrites the prose of hundreds of files:

* a leading block is removed only when it is a bare number REPEATING that chapter's own
  number, taken from its source tab's header (``is_chapter_number_block``). An in-story
  part marker that differs, or any number in a chapter whose source has no header to
  compare against, is left alone.
* a trailing block is removed only when it matches the copyright notice, in Korean or in
  the English the model produced for it.

Anything that can't be accounted for is reported and skipped, never guessed at.

It is a dry run by default; nothing is written without ``--apply``.

    python tools/clean_export_artifacts.py                  # dry run, every project
    python tools/clean_export_artifacts.py --project 2bf3edf5213d
    python tools/clean_export_artifacts.py --apply          # write (backs up first)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from translation_bot.pipeline import _AUDIT_TRANSLATION_MARK  # noqa: E402
from translation_bot.sanitize import (  # noqa: E402
    is_chapter_number_block,
    strip_export_footer,
    strip_source_header,
)
from translation_bot.text_source import records_to_chapters  # noqa: E402

PROJECTS = pathlib.Path(__file__).resolve().parent.parent / "projects"

# Where the prose lives. audit/ is a structured record, handled separately below; it
# matters because a needs-review chapter is saved ONLY there, so it is the copy the
# reader serves and the one Copy would otherwise pollute.
PROSE_DIRS = ("chapters", "previous")
CHAPTER_RE = re.compile(r"^chapter-(\d+)\.md$")


def source_numbers(pdir: pathlib.Path) -> dict[int, str] | None:
    """Map chapter index -> the chapter number printed in its source tab's header.

    Some tabs are missing the "NNN화" line, so their number cannot be read directly. A gap
    is filled only from the tabs immediately around it, either bracketed (146 _ 148 -> 147)
    or continuing a run (145, 146, _ -> 147).

    Deliberately local. A whole-project offset looked simpler and is wrong: one project's
    source doc holds two works, numbered 101-146 and then restarting at 1, so no single
    offset describes it and the majority rule quietly inherits that noise. Neighbours
    either agree or they don't, and across the restart they don't.

    The inference only ever REMOVES a block equal to the number it predicts, so an
    in-story part marker still survives: chapter-17 of one project opens "33." against a
    predicted 117, and chapter-79 of the same opens "55." against a predicted 179.

    Returns None when the project has no cached source at all.
    """
    sf = pdir / "source.json"
    if not sf.exists():
        return None
    try:
        records = json.loads(sf.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    out: dict[int, str] = {}
    chapters = records_to_chapters(records)
    for ch in chapters:
        number = strip_source_header(ch.text)[1]
        if number:
            out[ch.index] = number

    def num(i: int) -> int | None:
        v = out.get(i)
        return int(v) if v is not None else None

    filled: dict[int, str] = {}
    for ch in chapters:
        i = ch.index
        if i in out:
            continue
        a2, a1, b1, b2 = num(i - 2), num(i - 1), num(i + 1), num(i + 2)
        if a1 is not None and b1 is not None and b1 - a1 == 2:
            filled[i] = str(a1 + 1)                       # bracketed: 146 _ 148
        elif a2 is not None and a1 is not None and a1 - a2 == 1:
            filled[i] = str(a1 + 1)                       # continues a run: 145, 146, _
        elif b1 is not None and b2 is not None and b2 - b1 == 1 and b1 > 1:
            filled[i] = str(b1 - 1)                       # precedes a run: _, 2, 3
    out.update(filled)
    return out


def clean_prose(prose: str, number: str | None) -> tuple[str, bool, bool]:
    """Return (cleaned, dropped_number, dropped_notice) for one chapter's English."""
    blocks = [b for b in re.split(r"\n\s*\n", (prose or "").strip()) if b.strip()]
    dropped_number = False
    if blocks and is_chapter_number_block(blocks[0], number):
        blocks = blocks[1:]
        dropped_number = True
    body = "\n\n".join(blocks)
    trimmed = strip_export_footer(body)
    return trimmed, dropped_number, trimmed != body


def process(path: pathlib.Path, number: str | None, apply: bool,
            backup_root: pathlib.Path) -> tuple[bool, bool, str | None]:
    """Clean one file in place. Returns (dropped_number, dropped_notice, skip_reason)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return False, False, f"unreadable ({exc.__class__.__name__})"

    is_audit = path.parent.name == "audit"
    if is_audit:
        # Only the translated prose is ours to touch. The recorded Korean above the mark
        # is the audit's evidence of what the model actually saw — leave it exactly.
        if _AUDIT_TRANSLATION_MARK not in text:
            return False, False, "audit file has no translation section"
        head, prose = text.rsplit(_AUDIT_TRANSLATION_MARK, 1)
        cleaned, n, c = clean_prose(prose, number)
        if not (n or c):
            return False, False, None
        new = f"{head}{_AUDIT_TRANSLATION_MARK}\n\n{cleaned}\n"
    else:
        cleaned, n, c = clean_prose(text, number)
        if not (n or c):
            return False, False, None
        new = cleaned + "\n"

    if apply:
        rel = path.relative_to(backup_root.parent)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(path, dest)
        path.write_text(new, encoding="utf-8")
    return n, c, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (backs up first)")
    ap.add_argument("--project", help="limit to one project id")
    ap.add_argument("--stamp", default="20260828", help="backup folder suffix")
    args = ap.parse_args()

    if not PROJECTS.is_dir():
        print(f"no projects directory at {PROJECTS}")
        return 1

    tot_num = tot_notice = tot_files = 0
    skips: list[str] = []

    for pdir in sorted(p for p in PROJECTS.iterdir() if p.is_dir()):
        if args.project and pdir.name != args.project:
            continue
        numbers = source_numbers(pdir)
        backup_root = pdir / f"export_artifacts_backup_{args.stamp}"
        rows: list[str] = []
        for sub in (*PROSE_DIRS, "audit"):
            d = pdir / sub
            if not d.is_dir():
                continue
            for f in sorted(d.glob("chapter-*.md")):
                m = CHAPTER_RE.match(f.name)
                if not m:
                    continue
                idx = int(m.group(1))
                if numbers is None:
                    # No cached source -> the number rule can't be verified. The footer
                    # rule still can, so run with number=None: only the notice can go.
                    number = None
                else:
                    number = numbers.get(idx)
                n, c, skip = process(f, number, args.apply, backup_root)
                if skip:
                    skips.append(f"{pdir.name}/{sub}/{f.name}: {skip}")
                    continue
                if n or c:
                    tot_files += 1
                    tot_num += int(n)
                    tot_notice += int(c)
                    what = " + ".join(x for x in (("number" if n else ""),
                                                  ("notice" if c else "")) if x)
                    rows.append(f"    {sub}/{f.name}: {what}")
        if rows:
            src = "" if numbers is not None else "   (no source.json — notice only)"
            print(f"\n{pdir.name}{src}")
            for r in rows[:6]:
                print(r)
            if len(rows) > 6:
                print(f"    … and {len(rows) - 6} more")

    print()
    print(f"{'APPLIED' if args.apply else 'DRY RUN'} — {tot_files} file(s) "
          f"{'cleaned' if args.apply else 'would be cleaned'}: "
          f"{tot_num} leading chapter number(s), {tot_notice} copyright notice(s)")
    if skips:
        print(f"\nskipped {len(skips)} file(s):")
        for s in skips[:10]:
            print(f"   {s}")
        if len(skips) > 10:
            print(f"   … and {len(skips) - 10} more")
    if not args.apply:
        print("\nre-run with --apply to write "
              "(originals are copied to projects/<id>/export_artifacts_backup_<stamp>/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

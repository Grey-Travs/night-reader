"""One-time repair for damage three fixed bugs left behind in existing novels.

The fixes on this branch stop the damage happening again; they cannot undo what
already happened, because the app is written to read whatever is on disk rather
than to rewrite it. This walks the library and repairs three things:

A. **Chapter files stranded at the wrong pad width.** Filenames are zero-padded to
   a width derived from the chapter COUNT, so a novel crossing 99 to 100 turns
   ``chapter-07.md`` into ``chapter-007.md``. ``_normalize_chapter_padding``
   renames stragglers on load, but it deliberately refuses when the canonical name
   is already taken — a newer translation owns it, and renaming over the top would
   destroy it. Which means a novel that was RE-translated after crossing the
   boundary keeps two full sets forever: the live one, and an invisible older
   generation that still turns up as duplicate hits in global search and rides
   along in every backup and export.

B. **Colliding variant ids.** Ids used to be ``f"v{len(variants)}"``, which reuses
   a live id as soon as ``prune`` drops one from the middle. ``find_variant``
   returns the first match, so picking a version could splice a *different*
   version's text into the chapter.

C. **Pages stranded mid-run.** A page set to ``queued``/``ocr-running`` was never
   reset when a job was stopped or gave up, and the default sweep only selects
   ``new``/``failed`` — so "Read all unread" reported nothing to do and the only
   way out was hand-selecting every page.

Nothing here deletes. A superseded chapter file is MOVED into a ``.superseded/``
folder beside it, because those files are real (older) translations the user paid
for; they are simply not the current ones. Reversing this repair is a drag-and-drop.

**Close Night Reader before repairing.** This is a second process, so the in-process
file locks the app relies on mean nothing to it. Renaming a chapter file out from
under a running translation worker is precisely the race the app was fixed to avoid:
the worker resolves a path, this tool renames it, and the worker's write then
recreates the old name — leaving two files for one chapter, the newer one invisible.
``--apply`` refuses while the app is answering on its usual port.

Dry run by default::

    python tools/repair_library.py                  # report, change nothing
    python tools/repair_library.py --apply          # actually repair (app closed)
    python tools/repair_library.py --project <pid>  # one novel
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launch import app_is_running  # noqa: E402
from server.pages import STATUS_NEW, STATUS_QUEUED, STATUS_RUNNING  # noqa: E402
from server.projects import PROJECTS_DIR  # noqa: E402
from translation_bot.atomic import atomic_write_text  # noqa: E402

# The directories whose files are named from the chapter index, and their extension.
PADDED_DIRS = (("chapters", "md"), ("previous", "md"), ("audit", "md"),
               ("variants", "json"))

SUPERSEDED_DIRNAME = ".superseded"

# A page still marked running after this long has no job behind it: an OCR call is a
# couple of minutes at worst, and the manifest's mtime moves on every status change.
DEFAULT_STALE_MINUTES = 30


@dataclass
class Change:
    project: str
    kind: str          # padding | variant-id | stuck-page
    detail: str


@dataclass
class Report:
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    projects_scanned: int = 0

    def add(self, project: str, kind: str, detail: str) -> None:
        self.changes.append(Change(project, kind, detail))

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for change in self.changes:
            out[change.kind] += 1
        return dict(out)


# ---- A. chapter files at a stale pad width -----------------------------------

def canonical_width(project_dir: Path) -> int:
    """The pad width this novel's filenames should use.

    Taken from the chapter count, floored by the highest index actually on disk so a
    ``project.json`` that lags behind the source doc can't shrink the width and make
    the tool call the CURRENT files stale.
    """
    total = 0
    meta = project_dir / "project.json"
    if meta.exists():
        try:
            total = int(json.loads(meta.read_text(encoding="utf-8"))
                        .get("chapter_count") or 0)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            total = 0
    chapters = project_dir / "chapters"
    if chapters.is_dir():
        for f in chapters.glob("chapter-*.md"):
            tail = f.stem.split("-", 1)[-1]
            if tail.isdigit():
                total = max(total, int(tail))
    return max(2, len(str(total))) if total else 2


def _by_index(directory: Path, ext: str) -> dict[int, dict[int, Path]]:
    """index -> {pad width: file}."""
    out: dict[int, dict[int, Path]] = defaultdict(dict)
    try:
        files = list(directory.glob(f"chapter-*.{ext}"))
    except OSError:
        return {}
    for f in files:
        tail = f.stem.split("-", 1)[-1]
        if tail.isdigit():
            out[int(tail)][len(tail)] = f
    return out


def repair_padding(project_dir: Path, report: Report, *, apply: bool) -> None:
    """Move superseded stale-width chapter files into ``.superseded/``.

    Only ever touches a file whose index ALSO exists at the canonical width. A stale
    file standing alone is the live copy of that chapter — ``_normalize_chapter_padding``
    renames those on load, and moving one aside here would make a chapter disappear.
    """
    pid = project_dir.name
    width = canonical_width(project_dir)
    for sub, ext in PADDED_DIRS:
        directory = project_dir / sub
        if not directory.is_dir():
            continue
        for index, at_width in sorted(_by_index(directory, ext).items()):
            canonical = at_width.get(width)
            if canonical is None:
                continue  # lone stale file: normalize_chapter_padding renames it
            for stale_width, stale in sorted(at_width.items()):
                if stale_width == width:
                    continue
                try:
                    newer = stale.stat().st_mtime > canonical.stat().st_mtime + 1
                except OSError:
                    continue
                if newer:
                    # The model of this damage says the canonical file is the newer
                    # generation. When it isn't, moving the stale one aside would hide
                    # the better copy — report it and let a human look.
                    report.warn(
                        f"{pid}/{sub}: {stale.name} is NEWER than {canonical.name} "
                        f"— left alone, inspect by hand")
                    continue
                dest_dir = directory / SUPERSEDED_DIRNAME
                dest = dest_dir / stale.name
                if dest.exists():
                    report.warn(f"{pid}/{sub}: {dest} already exists — left alone")
                    continue
                report.add(pid, "padding", f"{sub}/{stale.name} -> "
                                           f"{sub}/{SUPERSEDED_DIRNAME}/{stale.name} "
                                           f"(superseded by {canonical.name})")
                if apply:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        stale.rename(dest)
                    except OSError as exc:
                        report.warn(f"{pid}/{sub}: could not move {stale.name}: {exc}")


# ---- B. colliding variant ids ------------------------------------------------

def _repair_group_ids(group: dict) -> list[str]:
    """Make every id in one group unique. Returns a description per change.

    The FIRST occurrence keeps its id, because that is the one ``find_variant``
    already resolves to — so the text currently spliced into the chapter does not
    move. Only the shadowed later duplicates are renumbered.
    """
    variants = group.get("variants") or []
    seen: set[str] = set()
    highest = 0
    for variant in variants:
        vid = str(variant.get("id") or "")
        if vid.startswith("v") and vid[1:].isdigit():
            highest = max(highest, int(vid[1:]))

    notes: list[str] = []
    for variant in variants:
        vid = str(variant.get("id") or "")
        if vid and vid not in seen:
            seen.add(vid)
            continue
        highest += 1
        new_id = f"v{highest}"
        notes.append(f"group {group.get('id')}: {vid or '(blank)'} -> {new_id}")
        variant["id"] = new_id
        seen.add(new_id)

    if not isinstance(group.get("next_seq"), int) or group["next_seq"] <= highest:
        group["next_seq"] = highest + 1
        if not notes:
            notes.append(f"group {group.get('id')}: next_seq set to {highest + 1}")
    return notes


def repair_variants(project_dir: Path, report: Report, *, apply: bool) -> None:
    pid = project_dir.name
    directory = project_dir / "variants"
    if not directory.is_dir():
        return
    for f in sorted(directory.glob("chapter-*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            report.warn(f"{pid}/variants/{f.name}: unreadable ({exc.__class__.__name__})")
            continue
        if not isinstance(doc, dict):
            continue
        notes: list[str] = []
        for group in (doc.get("groups") or []):
            if isinstance(group, dict):
                notes.extend(_repair_group_ids(group))
        if not notes:
            continue
        for note in notes:
            report.add(pid, "variant-id", f"variants/{f.name} {note}")
        if apply:
            atomic_write_text(f, json.dumps(doc, ensure_ascii=False, indent=2))


# ---- C. pages stranded mid-run ------------------------------------------------

def repair_pages(project_dir: Path, report: Report, *, apply: bool,
                 app_running: bool = True,
                 stale_minutes: int = DEFAULT_STALE_MINUTES,
                 now: float | None = None) -> None:
    """Reset pages stranded in ``queued``/``ocr-running``.

    Liveness is decided by whether the APP is running, not by how old the manifest
    is. Keying it off the mtime had it exactly backwards: the commonest way a page
    strands is pressing Stop, and the stop handler WRITES pages.json as it strands
    them — so the manifest is freshest at the precise moment the damage is done, and
    an age rule refuses to repair the one case it exists for. The user who just hit
    the bug and reached for the tool built to fix it was told to come back in half an
    hour, with nothing naming the remedy.

    With the app closed nothing can be writing, so a page still marked queued or
    running is stranded by definition, whatever its timestamp says. The age threshold
    survives only for a dry run taken while the app is up, where a job really could
    be in flight.
    """
    pid = project_dir.name
    f = project_dir / "pages.json"
    if not f.exists():
        return
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
        age_minutes = ((now or time.time()) - f.stat().st_mtime) / 60
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        report.warn(f"{pid}/pages.json: unreadable ({exc.__class__.__name__})")
        return
    if not isinstance(doc, dict):
        return

    stuck = [p for p in (doc.get("pages") or [])
             if isinstance(p, dict) and p.get("status") in (STATUS_QUEUED, STATUS_RUNNING)]
    if not stuck:
        return
    if app_running and age_minutes < stale_minutes:
        report.warn(f"{pid}: {len(stuck)} page(s) queued/running, Night Reader is open, "
                    f"and pages.json was touched {age_minutes:.0f}min ago — a job may be "
                    f"live, skipped. Close the app and run this again.")
        return

    for page in stuck:
        report.add(pid, "stuck-page",
                   f"page {page.get('id')} ({page.get('status')}) -> {STATUS_NEW}")
        page["status"] = STATUS_NEW
    if apply:
        atomic_write_text(f, json.dumps(doc, ensure_ascii=False, indent=2))


# ---- driver -------------------------------------------------------------------

def repair_library(projects_dir: Path | None = None, *, apply: bool = False,
                   only: str | None = None, app_running: bool | None = None,
                   stale_minutes: int = DEFAULT_STALE_MINUTES) -> Report:
    """Walk the library and repair. ``app_running=None`` detects it."""
    if app_running is None:
        app_running = app_is_running()
    base = Path(projects_dir or PROJECTS_DIR)
    report = Report()
    if not base.is_dir():
        report.warn(f"no projects directory at {base}")
        return report
    for meta in sorted(base.glob("*/project.json")):
        project_dir = meta.parent
        if only and project_dir.name != only:
            continue
        report.projects_scanned += 1
        repair_padding(project_dir, report, apply=apply)
        repair_variants(project_dir, report, apply=apply)
        repair_pages(project_dir, report, apply=apply, app_running=app_running,
                     stale_minutes=stale_minutes)
    return report


def _print(report: Report, *, apply: bool, verbose: bool) -> None:
    verb = "Repaired" if apply else "Would repair"
    print(f"Scanned {report.projects_scanned} novel(s).")
    print()
    counts = report.by_kind()
    labels = {"padding": "chapter files stranded at a stale pad width",
              "variant-id": "colliding paragraph-variant ids",
              "stuck-page": "pages stranded in queued/ocr-running"}
    if not counts:
        print("Nothing to repair.")
    for kind, label in labels.items():
        if counts.get(kind):
            print(f"{verb}: {counts[kind]:>5}  {label}")

    if report.changes and (verbose or not apply):
        shown = report.changes if verbose else report.changes[:20]
        print()
        for change in shown:
            print(f"  {change.project} {change.detail}")
        if len(shown) < len(report.changes):
            print(f"  ... and {len(report.changes) - len(shown)} more (--verbose)")

    if report.warnings:
        print()
        print(f"{len(report.warnings)} thing(s) left alone for a human:")
        for warning in report.warnings:
            print(f"  ! {warning}")

    if report.changes and not apply:
        print()
        print("Nothing was changed. Re-run with --apply to repair.")
        print("Superseded files are MOVED, never deleted — reversing this is "
              "drag-and-drop out of the .superseded folders.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually repair (default: report only)")
    parser.add_argument("--project", metavar="PID",
                        help="repair a single novel instead of the whole library")
    parser.add_argument("--projects-dir", metavar="DIR",
                        help="override the library location (for testing)")
    parser.add_argument("--stale-minutes", type=int, default=DEFAULT_STALE_MINUTES,
                        help="only while the app is open: how old pages.json must be "
                             "before a queued/running page counts as stranded "
                             f"(default {DEFAULT_STALE_MINUTES}). With the app closed, "
                             "nothing can be writing, so age is not consulted.")
    parser.add_argument("--ignore-running-app", action="store_true",
                        help="repair even though something is answering on the app's "
                             "port (only if you are sure that is not Night Reader)")
    parser.add_argument("--verbose", action="store_true", help="list every change")
    args = parser.parse_args(argv)

    running = app_is_running()
    if args.apply and running and not args.ignore_running_app:
        # A separate process, so the app's in-process locks do not apply to it.
        # Renaming a chapter file out from under a running worker recreates exactly
        # the two-files-for-one-chapter damage this tool exists to clean up.
        print("Night Reader looks like it is still running.")
        print()
        print("  Close it first — this tool moves and rewrites the same files the app")
        print("  writes while it works, and it cannot take the app's locks from")
        print("  another process.")
        print()
        print("  Nothing was changed. Close the app and run this again, or re-run")
        print("  without --apply to see the report.")
        return 2

    report = repair_library(Path(args.projects_dir) if args.projects_dir else None,
                            apply=args.apply, only=args.project,
                            app_running=running, stale_minutes=args.stale_minutes)
    _print(report, apply=args.apply, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

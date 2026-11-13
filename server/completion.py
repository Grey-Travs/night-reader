"""Notice when a novel has ended, so its status says "completed" without anyone setting it.

The source already says so: the last chapter's heading reads "138화 (완결)" (and the last
side story's "외전 30화 (완결)"), or the final book closes with "<title> 마침". Measured on
the real library in November 2026, 17 documents carried one of those, and only one of
their novels was marked completed.

The status lives where the app already keeps it: on the series for a novel split across
documents (so all its parts agree), otherwise on the novel itself.
"""

from __future__ import annotations

from server import projects as pj
from server import series as series_mod
from translation_bot.chapter_numbers import find_ending

COMPLETED = "completed"


def record_ending(pid: str, chapters) -> dict | None:
    """Mark the novel completed the first time its source shows the end.

    Recorded once per finale: the tab that showed it is stored beside the status as
    ``ending``, and a novel whose stored ending is this same tab is left alone. So when
    the user sets a novel back to "ongoing" -- a false alarm, or a second season starting
    -- re-reading the document does not keep overriding them. A NEW finale (side stories
    added and closed later) is noticed again.

    Returns the ending it just recorded, or None when there was nothing new.
    """
    series = series_mod.series_for_project(pid)
    holder = series if series is not None else pj.get_project(pid)
    if holder is None:
        return None
    ending = find_ending(chapters)
    if not ending:
        return None
    seen = holder.get("ending") or {}
    if seen.get("project_id") == pid and seen.get("index") == ending["index"]:
        return None
    holder["ending"] = {
        "project_id": pid,
        "index": ending["index"],
        "kind": ending["kind"],
        "signal": ending["signal"],
        "at": series_mod.now(),
    }
    holder["status"] = COMPLETED
    if series is not None:
        series_mod.write_series(holder)
    else:
        pj.save_project(holder)
    return holder["ending"]


def recheck(pid: str) -> dict | None:
    """``record_ending`` from the novel's stored source, for when its status moves.

    Joining a series moves the status from the novel onto the series, and the source is
    not read again for that -- a pasted or imported novel is never re-read at all -- so a
    finished part 2 added to its series would otherwise leave the series "ongoing".
    Best-effort: a failure here must not fail the change that called it.
    """
    try:
        return record_ending(pid, pj.load_cached_source(pid))
    except Exception:  # noqa: BLE001
        return None

"""Live translation progress printed to the server's own terminal window.

`start.bat` leaves a console open for the whole session, but nothing in the web path
ever wrote to it: `server/` had no print/logging at all and uvicorn is pinned to
`log_level="warning"`, so a translation that takes minutes produced total silence.

Every job event funnels through here (see `Job.publish` / `Job.publish_live`), so the
terminal mirrors exactly what the in-app console shows:

    ▐ night-reader ─────────────────────────
    ▸ ch 37  "문이 열렸다"  4,812 ch
      ├ opus · effort high
      ├ ████████░░░░░░  12/47 ¶            <- redrawn in place, never scrolls
      └ ✓ validated  8.4s  1.2k tok

Design constraints worth keeping in mind when editing:

* This is called from the asyncio event loop. Printing is cheap but not free, so the
  streaming path only ever rewrites ONE line and rate-limits itself.
* Korean titles mean UTF-8 stdout is mandatory (see ``term.force_utf8_stdio``) and
  width maths must count Hangul as two columns (``term.visible_width``).
* Colour is optional and degrades to plain text; never assume ANSI works.
"""

from __future__ import annotations

import sys
import time

from translation_bot.term import force_utf8_stdio, paint, supports_color, truncate

# Resolved once at import: reconfigure stdio for UTF-8 before anything prints a title.
force_utf8_stdio()
_COLOR = supports_color()

_BAR_WIDTH = 14
_TITLE_WIDTH = 32
_HEADER = "▐ night-reader "

# Per-chapter render state, keyed by project id, so two novels translating at once
# don't interleave into an unreadable mess.
_state: dict[str, dict] = {}
_last_flush: dict[str, float] = {}
_open_line = False       # True when the cursor sits on an unfinished \r progress line
_banner_shown = False

_MIN_REDRAW_INTERVAL = 0.2  # seconds between progress-line repaints


def _c(text: str, *styles: str) -> str:
    return paint(text, *styles, enabled=_COLOR)


def _write(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — a closed/redirected console never breaks a run
        pass


def _end_open_line() -> None:
    """Terminate an in-place progress line so the next real line starts clean."""
    global _open_line
    if _open_line:
        _write("\n")
        _open_line = False


def _banner() -> None:
    global _banner_shown
    if _banner_shown:
        return
    _banner_shown = True
    _write(_c(_HEADER + "─" * 40, "grey") + "\n")


def _bar(done: int, total: int) -> str:
    if total <= 0:
        return "░" * _BAR_WIDTH
    filled = max(0, min(_BAR_WIDTH, round(_BAR_WIDTH * done / total)))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


_STATUS_STYLE = {
    # Chapter outcomes.
    "validated": ("✓", "green"),
    "needs-review": ("⚠", "yellow"),
    "failed": ("✗", "red"),
    "empty": ("·", "grey"),
    "english-source": ("·", "grey"),
    # Scanned-page outcomes. Page work rides the same Job and publishes the same
    # "chapter" event type, so without these every OCR result printed as the grey
    # unknown-status fallback and the terminal never showed whether a page read well.
    "ok": ("✓", "green"),
    "needs-check": ("⚠", "yellow"),
    "edited": ("✓", "cyan"),
    "skipped": ("·", "grey"),
    "queued": ("·", "grey"),
    "ocr-running": ("·", "grey"),
    "new": ("·", "grey"),
}


def print_event(pid: str, ev: dict) -> None:
    """Render one job event. Safe to call for every event type; unknown types are
    ignored so adding an event elsewhere can never crash a translation."""
    try:
        _render(pid, ev)
    except Exception:  # noqa: BLE001 — cosmetic output is never worth failing a job over
        pass


# How a non-translation task is tagged on the item's console line. Mirrors TASK_LABEL
# in app.py; the page kinds were missing, so an OCR run printed as an unlabelled
# chapter line and read as a translation.
_TASK_NOTE = {
    "resolve": "· AI resolve",
    "pronouns": "· fixing pronouns",
    "ocr": "· reading a page",
    "ocr-verify": "· checking a page",
}


def _render(pid: str, ev: dict) -> None:
    global _open_line
    kind = ev.get("type")

    if kind == "start":
        _banner()
        _end_open_line()
        st = _state[pid] = {
            "index": ev.get("index"),
            "started": time.monotonic(),
            "paras": 0,
            "source_paras": 0,
            "chunk": (1, 1),
        }
        title = truncate(str(ev.get("title") or f"chapter {st['index']}"), _TITLE_WIDTH)
        chars = ev.get("chars") or 0
        _write(
            _c("▸ ", "cyan")
            + _c(f"ch {st['index']}", "bold")
            + _c(f'  "{title}"', "grey")
            + _c(f"  {chars:,} ch", "dim")
            # A repair shares this line with a translation, so name it — otherwise a
            # pronoun fix looks identical to a full re-translation in the terminal.
            + (_c(f"  {_TASK_NOTE[ev['kind']]}", "magenta")
               if ev.get("kind") in _TASK_NOTE else "")
            + "\n"
        )
        model = ev.get("model")
        if model:
            effort = ev.get("effort")
            _write(_c(f"  ├ {model}" + (f" · effort {effort}" if effort else ""), "dim") + "\n")
        return

    if kind == "source":
        st = _state.get(pid)
        if st is not None:
            st["source_paras"] = int(ev.get("paragraphs") or 0)
        return

    if kind == "delta":
        st = _state.get(pid)
        if st is None:
            return
        st["paras"] = int(ev.get("paragraphs") or st["paras"])
        st["chunk"] = tuple(ev.get("chunk") or st["chunk"])
        now = time.monotonic()
        # Rate-limit repaints: deltas can arrive many times a second and each one is a
        # synchronous console write on the event loop.
        if now - _last_flush.get(pid, 0.0) < _MIN_REDRAW_INTERVAL:
            return
        _last_flush[pid] = now
        total = st["source_paras"]
        chunk_note = ""
        if st["chunk"][1] > 1:
            chunk_note = _c(f"  chunk {st['chunk'][0]}/{st['chunk'][1]}", "dim")
        line = (
            _c("  ├ ", "dim")
            + _c(_bar(st["paras"], total), "cyan")
            + _c(f"  {st['paras']}/{total or '?'} ¶", "dim")
            + chunk_note
        )
        _write("\r\033[K" + line if _COLOR else "\r" + line + "   ")
        _open_line = True
        return

    if kind == "reset":
        st = _state.get(pid)
        if st is not None:
            # "retry" restarts the whole chapter; a reconnect only drops the current
            # chunk's partial output. Either way the paragraph count no longer holds.
            st["paras"] = 0
        _end_open_line()
        reason = ev.get("reason") or "restart"
        _write(_c(f"  ├ ↻ {reason} — restarting this section", "yellow") + "\n")
        return

    if kind == "chapter":
        st = _state.pop(pid, None)
        _last_flush.pop(pid, None)
        _end_open_line()
        status = str(ev.get("status") or "")
        mark, colour = _STATUS_STYLE.get(status, ("·", "grey"))
        bits = [_c(f"{mark} {status}", colour)]
        if ev.get("aborted"):
            bits = [_c("■ stopped", "yellow")]
        elif ev.get("skipped"):
            bits = [_c("· already done", "grey")]
        elif st is not None:
            bits.append(_c(f"{time.monotonic() - st['started']:.1f}s", "dim"))
        tokens = ev.get("tokens") or {}
        used = int(tokens.get("input_tokens", 0)) + int(tokens.get("output_tokens", 0))
        if used:
            bits.append(_c(f"{_fmt_tokens(used)} tok", "dim"))
        if ev.get("error"):
            bits.append(_c(truncate(str(ev["error"]), 60), "red"))
        _write(_c("  └ ", "dim") + "  ".join(bits) + "\n")
        return

    if kind == "queued":
        added = ev.get("added") or []
        if added:
            _banner()
            _end_open_line()
            _write(_c(f"  + queued {len(added)} chapter(s): "
                      f"{', '.join(str(i) for i in added[:12])}"
                      f"{'…' if len(added) > 12 else ''}", "grey") + "\n")
        return

    if kind == "waiting":
        _end_open_line()
        when = ev.get("resume_at")
        at = time.strftime("%H:%M", time.localtime(when)) if when else "soon"
        _write(_c(f"  ⏸ rate limited — waiting for the plan to refresh, resuming ~{at}",
                  "yellow") + "\n")
        return

    if kind == "resumed":
        _end_open_line()
        _write(_c("  ▸ plan refreshed — resuming", "cyan") + "\n")
        return

    if kind == "paused":
        _end_open_line()
        _write(_c(f"  ⏸ paused: {truncate(str(ev.get('message') or '')  , 70)}", "yellow") + "\n")
        return

    if kind == "done":
        _state.pop(pid, None)
        _last_flush.pop(pid, None)
        _end_open_line()
        totals = ev.get("totals") or {}
        tok = totals.get("tokens") or {}
        used = int(tok.get("input_tokens", 0)) + int(tok.get("output_tokens", 0))
        cost = totals.get("cost_usd") or 0.0
        _write(_c("▐ done", "green")
               + _c(f"  ·  {_fmt_tokens(used)} tok  ·  ${cost:.2f} plan-equivalent", "dim")
               + "\n")
        return


def print_error(title: str, detail: str) -> None:
    """One compact line for a handled server error. The full traceback goes to
    ``logs/errors.log`` instead of the console — that wall of text was the problem."""
    _end_open_line()
    _write(_c("✗ " + title, "red") + _c(f"  {truncate(detail, 90)}", "dim") + "\n")

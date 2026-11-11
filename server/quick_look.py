"""A fast first look at each photo the phone app takes: can every word on it be read?

A small model answers in a few seconds, on two threads of its own outside the novel's
worker queue, so a look never waits behind a page being read or a chapter being
translated — the point is to answer while the phone is still pointed at the page.

The answer is advice. It never holds a chapter by itself; a photo it doubts gets the
full double-check, even after a confident read, and the phone shows its note at once.
A look that fails, or meets the plan's limit, comes back ``unknown``, which flags
nothing: it is never worth waiting for.

Nothing here imports app.py. app.py hands over what this needs with :func:`configure`.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from translation_bot import ocr
from translation_bot.translator import Translator

from . import pages as pages_mod

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quick-look")
_hooks: dict = {"config": None, "looked": None}
_pending: set = set()
_pending_lock = threading.Lock()


class _Moved(Exception):
    """The photo was replaced or deleted while it was being looked at."""


def configure(*, config, looked=None) -> None:
    """``config(pid)`` gives a novel's Config; ``looked(pid)`` hears that a look landed."""
    _hooks["config"] = config
    _hooks["looked"] = looked


def from_the_phone(box: dict | None) -> bool:
    """Only photos the phone app took get a look: shares, PDFs and the bot don't."""
    return bool(box and pages_mod.is_hands_free(box)
                and str(box.get("origin") or "").startswith("phone:"))


def request(pid: str, page_id: str, digest: str) -> None:
    """Look at a photo just saved, if the phone took it and looks are switched on.
    Returns at once; everything else happens on the look's own thread."""
    future = _pool.submit(_look, pid, page_id, digest)
    with _pending_lock:
        _pending.add(future)
    future.add_done_callback(_forget)


def _forget(future) -> None:
    with _pending_lock:
        _pending.discard(future)


def _look(pid: str, page_id: str, digest: str) -> None:
    config = _hooks["config"]
    if config is None:
        return
    try:
        cfg = config(pid)
    except Exception:  # noqa: BLE001 — no config, no look; the full read still comes
        return
    if not cfg.phone.quick_look:
        return
    doc = pages_mod.load_pages(pid)
    page = pages_mod.find_page(doc, page_id)
    if page is None or page.get("sha256") != digest:
        return
    if not from_the_phone(pages_mod.find_box(doc, page.get("batch"))):
        return
    path = pages_mod.resolve_page_file(pid, page_id, doc)
    if path is None:
        return
    model = cfg.anthropic.model_copy(update={"model": cfg.phone.quick_look_model,
                                             "thinking": False})
    try:
        look = ocr.quick_look_page(Translator(model, cfg.translation), path)
    except Exception:  # noqa: BLE001 — a plan limit included: never worth waiting for
        look = ocr.QuickLook()
    record = {"verdict": look.verdict, "why": list(look.problems), "note": look.note,
              "at": pages_mod.now_iso()}
    try:
        with pages_mod.mutate_pages(pid) as live:
            rec = pages_mod.find_page(live, page_id)
            if rec is None or rec.get("sha256") != digest:
                raise _Moved()
            rec["look"] = record
            totals = live.setdefault("totals", {})
            totals["cost_usd"] = round(float(totals.get("cost_usd") or 0.0) + look.cost_usd, 6)
    except _Moved:
        return
    looked = _hooks["looked"]
    if looked is not None:
        try:
            looked(pid)
        except Exception:  # noqa: BLE001
            pass


def wait_idle(timeout: float = 10.0) -> None:
    """For tests: wait for every look already asked for."""
    while True:
        with _pending_lock:
            waiting = list(_pending)
        if not waiting:
            return
        for future in waiting:
            future.result(timeout=timeout)

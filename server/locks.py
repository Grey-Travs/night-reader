"""One lock per on-disk file, shared by everything that mutates project state.

An atomic write makes a single save all-or-nothing; it does NOT order two saves.
Without a lock, load → mutate → save from two threads is a read-modify-write race:
both read, both apply their own change to their own copy, and whichever writes last
silently discards the other's work.

Every writer in this app runs on a THREAD — sync endpoints and ``run_in_threadpool``
alike — never on the event loop, so ``threading.Lock`` is the right primitive.

Lives in its own module so ``server.pages`` and ``server.variants`` can use it
without importing ``server.app`` (which imports them).
"""

from __future__ import annotations

import threading
from pathlib import Path

# Keyed by resolved path, so the same file always maps to the same lock however the
# caller spelled it.
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def file_lock(path: str | Path) -> threading.Lock:
    """The lock guarding ``path``. Stable for the process lifetime."""
    key = str(Path(path).resolve())
    with _guard:
        return _locks.setdefault(key, threading.Lock())

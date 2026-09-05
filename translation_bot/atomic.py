"""One correct atomic file write, shared by everything the app persists.

Every JSON/text file here (state.json, glossary.json, project.json, source.json,
token.json) is written the same way: serialize to a temp file, then ``os.replace`` it
over the target. The rename is atomic, so a crash mid-write can't truncate the real file
and a concurrent reader only ever sees the old contents or the new ones.

That guarantee lives or dies on the temp file's NAME. Five hand-rolled copies of this
idiom each derived the name from the target path alone (``state.json.tmp``, or
``state.json.<pid>.tmp``) — one name shared by every thread in the process. The server
saves state.json from many threads at once (the translation worker via run_in_threadpool,
plus every request thread that edits, accepts, cleans or re-checks a chapter), so two
saves picked the SAME temp path: one was still writing it when the other tried to rename
it, which on Windows is

    PermissionError: [WinError 32] The process cannot access the file because it is
    being used by another process: 'state.json.12500.tmp' -> 'state.json'

and, when the timing fell the other way, a temp file holding two threads' output spliced
together — the truncated/garbled state.json that ``State.load`` has to defend against.

Naming the temp file uniquely per CALL is the fix, and it only works if there is one
implementation to get right, hence this module.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

# Riding out a locked DESTINATION. Unique temp names rule out our own threads fighting
# each other, but os.replace still fails if something else holds the target open for a
# moment — a reader between open and close, an AV scanner, a Dropbox-style syncer. These
# clear in milliseconds, so a short retry turns a crash back into a normal save.
_ATTEMPTS = 10
_BACKOFF = 0.05
_BACKOFF_MAX = 0.3


def _replace_with_retry(src: Path, dst: Path) -> None:
    delay = _BACKOFF
    for attempt in range(_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            # Windows raises this for a sharing violation (errno 13 / winerror 32).
            # POSIX effectively never gets here: rename over an open file is fine.
            if attempt == _ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, _BACKOFF_MAX)


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically, safe against concurrent writers.

    The temp file name carries a random suffix as well as the pid, so no two calls —
    across threads OR processes — can ever choose the same one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
            # Flush to the platter before the rename: without this a power cut can leave
            # the renamed file present but empty, which is the exact outcome the atomic
            # write exists to prevent.
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    finally:
        # Never leave litter. On success the temp file is gone already (it WAS the
        # rename's source), so this only bites on the failure path.
        try:
            tmp.unlink()
        except OSError:
            pass

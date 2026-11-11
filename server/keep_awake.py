"""Keep the computer from going to sleep while Night Reader is working for the phone.

A chapter photographed in the evening is read, built and translated over the next hour;
a computer that sleeps after twenty idle minutes would stop that halfway, and the photos
still on the phone would wait until morning. So while there is work — a job running or
waiting for the plan's limit, or a photo that arrived in the last half hour — this asks
Windows not to sleep, and lets go once there is none.

It only asks for the system to stay awake: the screen still turns off, and closing a
laptop's lid or choosing Sleep still sleeps. It can't wake a computer already asleep.

``SetThreadExecutionState`` is per thread, so the asking is done on a thread of its own,
which holds the request while it lives and drops it when it ends.
"""

from __future__ import annotations

import ctypes
import sys
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
CHECK_EVERY_S = 30.0


def windows_kernel32():
    if sys.platform != "win32":
        return None
    try:
        return ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None


class KeepAwake:
    """Holds the computer awake while ``busy()`` says there is work."""

    def __init__(self, busy, *, kernel32=None, every: float = CHECK_EVERY_S):
        self.busy = busy
        self.kernel32 = kernel32
        self.every = every
        self.holding = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _ask(self, awake: bool) -> None:
        if self.kernel32 is not None:
            self.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if awake else 0))
        self.holding = awake

    def tick(self) -> bool:
        """Check once, and hold or let go to match. Call it on the keeper's thread."""
        try:
            want = bool(self.busy())
        except Exception:  # noqa: BLE001 — a failed check lets go rather than hold forever
            want = False
        if want != self.holding:
            self._ask(want)
        return want

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.every)
        if self.holding:
            self._ask(False)

    def start(self) -> None:
        if self._thread is None and self.kernel32 is not None:
            self._thread = threading.Thread(target=self._run, name="keep-awake", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

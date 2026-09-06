"""Re-export of the shared file-lock registry.

The implementation moved to :mod:`translation_bot.locks` so the engine can use it
too — ``write_chapter_file`` now takes the lock itself, which is the only way a
chapter write can be guarded no matter which of its callers performs it, and the
engine must not import the web layer.

Kept as a module because ``server.pages``, ``server.variants`` and ``server.app``
already import ``file_lock`` from here. Importing it from either place returns the
same lock for the same path — there is one registry in the process.
"""

from translation_bot.locks import file_lock

__all__ = ["file_lock"]

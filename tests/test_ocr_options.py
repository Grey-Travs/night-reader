"""Tests for the per-call tool exception that OCR needs.

The translator deliberately blocks every tool so a chapter call is clean text-in/
text-out. OCR is the one exception: the Claude Agent SDK has no image content block,
so the only way to hand the agent a page photo is a path it opens with ``Read``.

What has to hold:
- ``Read`` stays blocked for every existing caller. A call that does not explicitly
  ask for a tool must be byte-identical to the behaviour before OCR existed.
- Opting into ``Read`` unblocks ``Read`` and nothing else — Bash/Write/Edit stay off.
- ``cwd``/``add_dirs`` scope that Read to one folder, so an OCR call cannot wander
  outside the project's ``pages/`` directory.
- The module-level ``_BLOCKED_TOOLS`` list is never mutated by building options.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_ocr_options.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translation_bot.config import AnthropicConfig, TranslationConfig  # noqa: E402
from translation_bot.translator import _BLOCKED_TOOLS, Translator  # noqa: E402


def _translator(**anthropic_fields) -> Translator:
    return Translator(AnthropicConfig(**anthropic_fields), TranslationConfig())


# ---- the default call is unchanged -------------------------------------------

def test_read_is_blocked_by_default():
    opts = _translator()._options("system")
    assert "Read" in opts.disallowed_tools, "Read must stay blocked for ordinary calls"
    assert "Read" not in opts.allowed_tools, "Read must never be allowed by default"


def test_default_blocks_every_tool():
    opts = _translator()._options("system")
    assert opts.allowed_tools == [], "a plain call opts into no tools at all"
    assert set(opts.disallowed_tools) == set(_BLOCKED_TOOLS), "the full block list applies"


def test_default_has_no_filesystem_scope():
    opts = _translator()._options("system")
    assert opts.cwd is None, "a text-only call must not set a working directory"
    assert opts.add_dirs == [], "a text-only call must not add readable directories"


def test_web_access_still_allows_only_websearch():
    """The pre-existing web_access behaviour must survive the refactor."""
    opts = _translator(web_access=True)._options("system")
    assert opts.allowed_tools == ["WebSearch"], "web access allows exactly WebSearch"
    assert "WebSearch" not in opts.disallowed_tools, "WebSearch cannot be both allowed and blocked"
    assert "Read" in opts.disallowed_tools, "web access must not unblock Read"


# ---- opting in ---------------------------------------------------------------

def test_read_can_be_opted_into_per_call():
    opts = _translator()._options("system", tools=["Read"])
    assert "Read" in opts.allowed_tools, "OCR must be able to opt into Read"
    assert "Read" not in opts.disallowed_tools, "Read cannot be both allowed and blocked"


def test_opting_into_read_unblocks_nothing_else():
    opts = _translator()._options("system", tools=["Read"])
    for tool in ("Bash", "Write", "Edit", "Glob", "Grep", "WebFetch", "Task"):
        assert tool in opts.disallowed_tools, f"{tool} must stay blocked during OCR"


def test_opting_in_composes_with_web_access():
    opts = _translator(web_access=True)._options("system", tools=["Read"])
    assert set(opts.allowed_tools) == {"Read", "WebSearch"}, "both opt-ins apply"
    assert "Read" not in opts.disallowed_tools
    assert "WebSearch" not in opts.disallowed_tools


def test_filesystem_scope_is_stringified():
    """ClaudeAgentOptions takes str|Path; we always hand it str so the CLI sees a
    plain path regardless of how the caller built it."""
    opts = _translator()._options(
        "system", tools=["Read"], cwd="/tmp/pages", add_dirs=["/tmp/pages"])
    assert opts.cwd == "/tmp/pages", "cwd scopes Read to the project's pages folder"
    assert opts.add_dirs == ["/tmp/pages"], "add_dirs keeps the image inside the sandbox"
    assert all(isinstance(d, str) for d in opts.add_dirs), "add_dirs entries must be strings"


# ---- the block list is shared module state -----------------------------------

def test_building_options_never_mutates_the_block_list():
    before = list(_BLOCKED_TOOLS)
    tr = _translator(web_access=True)
    tr._options("system", tools=["Read"])
    tr._options("system")
    assert _BLOCKED_TOOLS == before, "_BLOCKED_TOOLS must not be mutated by a call"


def test_a_later_plain_call_is_unaffected_by_an_earlier_ocr_call():
    """An OCR call must not leak its permission into the next chapter translation."""
    tr = _translator()
    tr._options("system", tools=["Read"], cwd="/tmp/pages")
    opts = tr._options("system")
    assert "Read" in opts.disallowed_tools, "Read must be blocked again on the next call"
    assert opts.cwd is None, "the OCR working directory must not persist"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

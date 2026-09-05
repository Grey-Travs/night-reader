"""Claude translation via the Claude Agent SDK (runs on a Claude subscription).

This talks to Claude through the Claude Agent SDK, which authenticates with the
user's logged-in Claude **subscription** (Max/Pro) — no API key, no separate API
billing. Usage counts against the plan's allotment; when the plan's window is
exhausted the SDK reports a rejected rate-limit and we raise :class:`RateLimitedError`
so the pipeline can stop that chapter cleanly and resume later.

Whole-chapter calls by default (preserves voice, pronoun, and honorific
consistency); paragraph-boundary chunking with explicit do-not-translate
continuity context only when a chapter exceeds the threshold. Low variance comes
from `effort` + adaptive thinking.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    query,
)

from .config import AnthropicConfig, TranslationConfig
from .docs_extract import Chapter
from .glossary import (
    VALID_TYPES,
    GlossaryEntry,
    format_injection,
    format_names,
    normalize_pronoun,
)
from .prompts import (
    META_SCAN_PROMPT,
    NAME_EXTRACTION_PROMPT,
    NEW_TERMS_DELIMITER,
    PRONOUN_DETECT_PROMPT,
    PRONOUN_FIX_PROMPT,
    TERM_CLASSIFY_PROMPT,
    build_system_prompt,
)
from .sanitize import remove_korean_echoes, strip_reasoning

_VALID_EFFORT = {"low", "medium", "high", "xhigh", "max"}

# Tools the agent must never reach for — we want clean text-in/text-out.
_BLOCKED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebSearch", "WebFetch", "NotebookEdit", "TodoWrite", "Task",
]

_RETRY_REMINDER = (
    "\n\nIMPORTANT: Your previous attempt failed an automated fidelity check. "
    "Translate the section COMPLETELY — omit nothing, condense nothing, add nothing. "
    "Match the source paragraph by paragraph."
)


class TranslatorError(RuntimeError):
    """A non-recoverable error from the agent (not a rate limit)."""


class TranslationAborted(RuntimeError):
    """The user stopped this chapter mid-flight. Not a failure — the caller must
    leave the chapter's existing status/output alone rather than marking it failed."""


# The SDK raises a BARE Exception when the Claude Code CLI is spawned but doesn't
# answer the `initialize` control request in time ("Control request timeout:
# initialize"). It's almost always a slow cold start (antivirus scanning the node
# process, a machine under load), so it retries successfully — but as a bare
# Exception it matched none of our except clauses and escaped as a 500.
_STARTUP_TIMEOUT_MARKERS = ("control request timeout", "initialize timeout")


def _is_startup_timeout(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _STARTUP_TIMEOUT_MARKERS)


def _emit(fn: Callable | None, *args) -> None:
    """Call a progress hook defensively. A broken/slow callback must never be able
    to fail a translation that otherwise succeeded."""
    if fn is None:
        return
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 — progress reporting is never worth failing over
        pass


@dataclass
class StreamHooks:
    """Live-progress callbacks for one chapter, invoked from the translator's thread.

    The web worker runs ``process_chapter`` in a threadpool, so implementations must
    be non-blocking and must marshal back to the event loop themselves (see
    ``server/app.py``). They must not raise; ``_emit`` swallows it if they do.

    Text arrives as an append-only stream, punctuated by two boundary signals:

    * ``on_chunk(i, n)`` — an oversized chapter is translated in ``n`` calls whose
      prose is CONCATENATED. Everything streamed so far is final; treat this as a
      commit point, not a clear.
    * ``on_reset(reason)`` — discard streamed text. ``"reconnect"``/``"restart"``
      abandon only the current chunk's partial output (earlier chunks stand);
      ``"retry"`` means the whole chapter is being redone from scratch, so drop
      everything.
    """

    on_source: Callable[[list[str]], None] | None = None    # the Korean the model sees
    on_text: Callable[[str], None] | None = None            # a chunk of English arrived
    on_reset: Callable[[str], None] | None = None           # discard shown text (retry/chunk)
    on_chunk: Callable[[int, int], None] | None = None      # chunk i of n starting
    abort: threading.Event | None = None                    # set -> raise TranslationAborted

    def aborted(self) -> bool:
        return self.abort is not None and self.abort.is_set()

    # Callers use these rather than the raw fields, so an unset hook and a hook that
    # raises are both handled in one place.
    def source(self, paragraphs: list[str]) -> None:
        _emit(self.on_source, paragraphs)

    def text(self, chunk: str) -> None:
        _emit(self.on_text, chunk)

    def reset(self, reason: str) -> None:
        _emit(self.on_reset, reason)

    def chunk(self, i: int, n: int) -> None:
        _emit(self.on_chunk, i, n)


class RateLimitedError(RuntimeError):
    """The subscription's usage window is exhausted; resume after it resets."""

    def __init__(self, info):
        self.info = info
        resets_at = getattr(info, "resets_at", None)
        when = ""
        if resets_at:
            when = " resets at " + time.strftime("%Y-%m-%d %H:%M", time.localtime(resets_at))
        super().__init__(
            "Claude subscription usage limit reached"
            f" ({getattr(info, 'rate_limit_type', 'usage')}{when}). "
            "Re-run later to continue — completed chapters are skipped."
        )


@dataclass
class TranslationResult:
    prose: str
    new_terms: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0  # plan-equivalent usage cost (covered by the subscription)
    n_chunks: int = 1
    warnings: list[str] = field(default_factory=list)


def _agent_model(model: str) -> str:
    """Map a full model id to the alias Claude Code expects (robust to id format)."""
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    # Fable/unknown ids pass through unchanged — the SDK accepts full model ids.
    return model or "opus"


def _accumulate(into: dict, src: dict) -> None:
    for k, v in src.items():
        into[k] = into.get(k, 0) + v


def _extract_usage(result: ResultMessage) -> dict:
    out = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    model_usage = getattr(result, "model_usage", None) or {}
    for u in model_usage.values():
        if not isinstance(u, dict):
            continue
        out["input_tokens"] += u.get("inputTokens", 0)
        out["output_tokens"] += u.get("outputTokens", 0)
        out["cache_read_input_tokens"] += u.get("cacheReadInputTokens", 0)
        out["cache_creation_input_tokens"] += u.get("cacheCreationInputTokens", 0)
    return out


def parse_response(text: str) -> tuple[str, list[dict], list[str]]:
    """Split the model output into prose and the new-terms JSON array.

    Leaked AI reasoning/meta ("Let me redo", glossary chatter, wrong-name drafts) is
    stripped from the prose here so it can never reach a chapter file.
    """
    warnings: list[str] = []
    if NEW_TERMS_DELIMITER not in text:
        prose, removed = strip_reasoning(text)
        prose, echoes = remove_korean_echoes(prose)
        if removed or echoes:
            warnings.append(f"stripped {len(removed)} reasoning + {echoes} Korean-echo block(s)")
        warnings.append("response had no ===NEW_TERMS=== block; treating all output as prose")
        return prose, [], warnings

    prose, _, tail = text.partition(NEW_TERMS_DELIMITER)
    prose, removed = strip_reasoning(prose)
    prose, echoes = remove_korean_echoes(prose)
    if removed or echoes:
        warnings.append(f"stripped {len(removed)} reasoning + {echoes} Korean-echo block(s) from output")

    start = tail.find("[")
    end = tail.rfind("]")
    new_terms: list[dict] = []
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(tail[start : end + 1])
            if isinstance(parsed, list):
                new_terms = [d for d in parsed if isinstance(d, dict)]
            else:
                warnings.append("new-terms block was not a JSON array")
        except json.JSONDecodeError:
            warnings.append("could not parse new-terms JSON block")
    else:
        warnings.append("new-terms block present but no JSON array found")
    return prose, new_terms, warnings


def _chunk_paragraphs(paragraphs: list[str], threshold: int) -> list[list[str]]:
    """Split paragraphs into chunks each under the char threshold."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for para in paragraphs:
        plen = len(re.sub(r"\s", "", para))
        if current and size + plen > threshold:
            chunks.append(current)
            current = []
            size = 0
        current.append(para)
        size += plen
    if current:
        chunks.append(current)
    return chunks


def _build_user_message(
    source: str, *, continuity: str | None = None, extra_instruction: str = ""
) -> str:
    parts: list[str] = []
    if continuity:
        parts.append(
            "The following preceding text is FOR CONTINUITY ONLY — do not "
            "re-translate it and do not include it in your output:\n\n"
            f"{continuity}\n\n---\n"
        )
        parts.append("Now translate THIS section completely into English:\n\n" + source)
    else:
        parts.append("Translate the following chapter completely into English:\n\n" + source)
    return "".join(parts) + extra_instruction


class Translator:
    def __init__(self, cfg: AnthropicConfig, tcfg: TranslationConfig,
                 canonical_names: list[GlossaryEntry] | None = None):
        self.cfg = cfg
        self.tcfg = tcfg
        # Established English spellings (incl. names learned from already-English
        # chapters) injected into every chapter so new translations match them.
        self.canonical_names = canonical_names or []

    def _options(self, system_text: str, max_turns: int = 1) -> ClaudeAgentOptions:
        web = self.cfg.web_access
        # Fable 5 has thinking always on; {"type": "disabled"} is rejected with a 400.
        fable = "fable" in (self.cfg.model or "").lower()
        return ClaudeAgentOptions(
            system_prompt=system_text,           # fully replaces the default agent prompt
            allowed_tools=(["WebSearch"] if web else []),
            disallowed_tools=([t for t in _BLOCKED_TOOLS if t != "WebSearch"] if web
                              else _BLOCKED_TOOLS),
            permission_mode="bypassPermissions",  # headless: never prompt for approval
            setting_sources=[],                    # ignore project .claude/ skills + config
            max_turns=max_turns,                   # 1 for translation; more for aux checks
            model=_agent_model(self.cfg.model),
            effort=(self.cfg.effort if self.cfg.effort in _VALID_EFFORT else "high"),
            thinking={"type": "adaptive"} if (self.cfg.thinking or fable) else {"type": "disabled"},
        )

    async def _aquery(self, system_text: str, user_text: str, max_turns: int = 1,
                      hooks: StreamHooks | None = None) -> tuple[str, dict, float]:
        texts: list[str] = []
        usage: dict = {}
        cost = 0.0
        rate_limited = None
        last_info = None  # latest rate-limit info seen, even non-rejected warnings
        got_result = False
        async for msg in query(prompt=user_text, options=self._options(system_text, max_turns)):
            # Cooperative stop: a threadpool thread can't be killed, so the only way
            # to end an in-flight chapter is to check between streamed messages and
            # break out — which closes the generator and tears the CLI subprocess down.
            if hooks is not None and hooks.aborted():
                raise TranslationAborted("stopped by the user")
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
                        if hooks is not None:
                            hooks.text(block.text)
            elif isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                last_info = info
                if getattr(info, "status", None) == "rejected":
                    rate_limited = info
            elif isinstance(msg, ResultMessage):
                got_result = True
                cost = msg.total_cost_usd or 0.0
                usage = _extract_usage(msg)
                if msg.is_error:
                    detail = msg.api_error_status or msg.errors or msg.subtype
                    # A 429 is a rate limit — treat it like the rejected RateLimitEvent
                    # so it pauses/resumes gracefully instead of surfacing a raw error.
                    # The SDK often emits a warning RateLimitEvent (with resets_at)
                    # before the hard 429, so reuse its reset time when we have one.
                    if str(getattr(msg, "api_error_status", "")) == "429" or "429" in str(detail) \
                            or "rate limit" in str(detail).lower():
                        raise RateLimitedError(SimpleNamespace(
                            rate_limit_type="rate_limit",
                            resets_at=getattr(last_info, "resets_at", None)))
                    raise TranslatorError(f"agent error: {detail}")
        if rate_limited is not None:
            raise RateLimitedError(rate_limited)
        # A stream that ends with no ResultMessage was cut off (process died / connection
        # dropped mid-output). Don't return the partial text as if it were a finished
        # chapter — raise so it's retried/failed, never silently written as truncated.
        if not got_result:
            raise TranslatorError("incomplete response: the model run ended before finishing")
        return "".join(texts).strip(), usage, cost

    def _call(self, system_text: str, user_text: str, max_turns: int = 1,
              hooks: StreamHooks | None = None) -> tuple[str, dict, float]:
        """One agent call -> (text, usage dict, plan-equivalent cost)."""
        last: Exception | None = None
        attempts = max(1, self.cfg.api_retry_count)
        for attempt in range(attempts):
            try:
                return asyncio.run(self._aquery(system_text, user_text, max_turns, hooks))
            except (RateLimitedError, TranslatorError, CLINotFoundError, TranslationAborted):
                raise  # don't retry hard limits / config errors / a deliberate stop
            except (CLIConnectionError, ProcessError) as exc:
                last = exc
                if attempt < attempts - 1:
                    if hooks is not None:
                        hooks.reset("reconnect")
                    time.sleep(min(2 ** attempt, 30))
            except Exception as exc:  # noqa: BLE001
                # A startup timeout is transient — retry it like a dropped connection.
                # Anything else genuinely unknown becomes a TranslatorError rather than
                # escaping bare: callers (and the HTTP layer) only handle our own types,
                # so a bare exception here surfaced as an opaque 500 + raw traceback.
                if not _is_startup_timeout(exc):
                    raise TranslatorError(f"{type(exc).__name__}: {exc}") from exc
                last = exc
                if attempt < attempts - 1:
                    if hooks is not None:
                        hooks.reset("restart")
                    time.sleep(min(2 ** attempt, 30))
        raise TranslatorError(f"agent connection failed after {attempts} attempts: {last}")

    def translate_chapter(
        self,
        chapter: Chapter,
        glossary_entries: list[GlossaryEntry],
        *,
        extra_instruction: str = "",
        retry_reminder: bool = False,
        hooks: StreamHooks | None = None,
    ) -> TranslationResult:
        system_text = build_system_prompt(
            format_injection(glossary_entries),
            web_access=self.cfg.web_access,
            honorific_note=self.tcfg.honorific_note,
            style_note=self.tcfg.style_note,
            names_block=format_names(self.canonical_names) if self.canonical_names else None,
        )
        reminder = (_RETRY_REMINDER if retry_reminder else "") + extra_instruction

        usage: dict = {}
        cost = 0.0
        warnings: list[str] = []

        if chapter.metrics.char_count <= self.tcfg.chunk_threshold:
            if hooks is not None:
                hooks.chunk(1, 1)
            user_text = _build_user_message(chapter.text, extra_instruction=reminder)
            text, u, c = self._call(system_text, user_text, hooks=hooks)
            _accumulate(usage, u)
            cost += c
            prose, new_terms, w = parse_response(text)
            warnings += w
            return TranslationResult(prose, new_terms, usage, cost, 1, warnings)

        # Oversized chapter: chunk at paragraph boundaries with continuity context.
        chunks = _chunk_paragraphs(chapter.paragraphs, self.tcfg.chunk_threshold)
        prose_parts: list[str] = []
        all_terms: list[dict] = []
        prev_source_paras: list[str] = []

        for i, chunk_paras in enumerate(chunks):
            continuity = None
            if i > 0 and self.tcfg.continuity_paragraphs > 0:
                continuity = "\n\n".join(prev_source_paras[-self.tcfg.continuity_paragraphs :])
            if hooks is not None:
                hooks.chunk(i + 1, len(chunks))
            user_text = _build_user_message(
                "\n\n".join(chunk_paras), continuity=continuity, extra_instruction=reminder
            )
            text, u, c = self._call(system_text, user_text, hooks=hooks)
            _accumulate(usage, u)
            cost += c
            prose, new_terms, w = parse_response(text)
            warnings += [f"chunk {i + 1}: {x}" for x in w]
            prose_parts.append(prose)
            all_terms.extend(new_terms)
            prev_source_paras = chunk_paras

        return TranslationResult(
            "\n\n".join(prose_parts), all_terms, usage, cost, len(chunks), warnings
        )

    def find_meta_leaks(self, text: str) -> list[str]:
        """Deep check: have Claude read the chapter and return verbatim snippets that
        are NOT story (preambles, notes, reasoning, untranslated source). Best-effort;
        never raises on bad output. Catches phrasings the regex can't anticipate."""
        if not text.strip():
            return []
        # Allow several turns: reviewing a long chapter with adaptive thinking can
        # take more than one turn, and capping at 1 makes the SDK abort.
        out, _u, _c = self._call(META_SCAN_PROMPT, "Chapter to review:\n\n" + text, max_turns=8)
        start, end = out.find("["), out.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        try:
            data = json.loads(out[start: end + 1])
        except json.JSONDecodeError:
            return []
        return [str(x).strip() for x in data if isinstance(x, str) and str(x).strip()]

    def extract_glossary(self, english_text: str) -> list[dict]:
        """Have Claude pull the cast/places/terms out of already-English chapters,
        so their established spellings can seed the glossary. Returns a list of
        ``{english, type, note, pronoun}`` dicts (best-effort; never raises on bad output)."""
        if not english_text.strip():
            return []
        user_text = "Extract the glossary from this novel text:\n\n" + english_text
        text, _u, _c = self._call(NAME_EXTRACTION_PROMPT, user_text, max_turns=8)
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
        out: list[dict] = []
        for d in data if isinstance(data, list) else []:
            if isinstance(d, dict) and str(d.get("english", "")).strip():
                out.append({
                    "english": str(d["english"]).strip(),
                    "type": str(d.get("type", "name")).strip().lower(),
                    "note": str(d.get("note", "")).strip(),
                    "pronoun": normalize_pronoun(d.get("pronoun", "")),
                })
        return out

    def classify_terms(self, terms: list[str]) -> dict[str, str]:
        """Classify user-supplied English subjects as name/place/skill/term/other.
        Returns ``{english_lowercased: type}``; items the model omits or mislabels
        simply won't appear (callers default those to "other")."""
        items = [t.strip() for t in terms if t.strip()]
        if not items:
            return {}
        text, _u, _c = self._call(TERM_CLASSIFY_PROMPT, "Classify these:\n\n" + "\n".join(items), max_turns=8)
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
        out: dict[str, str] = {}
        for d in data if isinstance(data, list) else []:
            if isinstance(d, dict) and str(d.get("english", "")).strip():
                typ = str(d.get("type", "")).strip().lower()
                out[str(d["english"]).strip().lower()] = typ if typ in VALID_TYPES else "other"
        return out

    def detect_pronouns(self, names: list[str], sample: str) -> dict[str, str]:
        """Determine each character's pronoun from the novel's own English text.
        Returns ``{english_lowercased: "he"|"she"|"they"}``; names the model omits
        or marks "unknown" are simply absent (callers leave those empty)."""
        items = [n.strip() for n in names if n.strip()]
        if not items or not sample.strip():
            return {}
        user_text = "Names:\n\n" + "\n".join(items) + "\n\nNovel text:\n\n" + sample
        text, _u, _c = self._call(PRONOUN_DETECT_PROMPT, user_text, max_turns=8)
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
        out: dict[str, str] = {}
        for d in data if isinstance(data, list) else []:
            if isinstance(d, dict) and str(d.get("english", "")).strip():
                pronoun = normalize_pronoun(d.get("pronoun", ""))
                if pronoun:
                    out[str(d["english"]).strip().lower()] = pronoun
        return out

    # Pronoun forms in the order the prompt should present them, so the model is told
    # the full paradigm rather than a single token.
    _PRONOUN_FORMS = {
        "he": "he/him/his/himself",
        "she": "she/her/hers/herself",
        "they": "they/them/their/theirs/themselves",
    }

    def fix_pronouns(
        self,
        prose: str,
        fixes: list[dict],
        hooks: StreamHooks | None = None,
    ) -> tuple[str, dict, float]:
        """Rewrite ONLY the pronouns of the named characters in an existing translation.

        This is deliberately not a re-translation: the chapter's English is already
        good apart from the gender the model guessed for a character whose glossary
        ``pronoun`` says otherwise. Sending the finished prose back with an explicit
        character -> pronoun list is far cheaper than re-translating from Korean, and
        it keeps the prose the user has already read and possibly edited.

        ``fixes`` is a list of ``{"name": str, "expected": "he"|"she"|"they"}``.
        Returns ``(corrected_prose, usage, cost_usd)``. The CALLER must verify that
        nothing but pronouns changed — see ``pipeline.pronouns_only_changed``.
        """
        wanted = [f for f in fixes if f.get("name") and f.get("expected") in self._PRONOUN_FORMS]
        if not wanted or not prose.strip():
            return prose, {}, 0.0

        lines = [f"- {f['name']} is {f['expected']} — use {self._PRONOUN_FORMS[f['expected']]}"
                 for f in wanted]
        user_text = ("Characters whose pronouns are wrong in this chapter:\n\n"
                     + "\n".join(lines)
                     + "\n\nChapter:\n\n" + prose)

        # One chunk: the whole chapter goes in a single call so the model can resolve
        # referents across the entire text, and the live console shows one clean pass.
        if hooks is not None:
            hooks.chunk(1, 1)
        text, usage, cost = self._call(PRONOUN_FIX_PROMPT, user_text, hooks=hooks)
        cleaned, _removed = strip_reasoning(text)
        return (cleaned.strip() or prose), usage, cost

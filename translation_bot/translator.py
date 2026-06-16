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
import time
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
from .glossary import GlossaryEntry, format_injection, format_names
from .prompts import META_SCAN_PROMPT, NAME_EXTRACTION_PROMPT, NEW_TERMS_DELIMITER, build_system_prompt
from .sanitize import strip_reasoning

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
        if removed:
            warnings.append(f"stripped {len(removed)} leaked reasoning block(s) from output")
        warnings.append("response had no ===NEW_TERMS=== block; treating all output as prose")
        return prose, [], warnings

    prose, _, tail = text.partition(NEW_TERMS_DELIMITER)
    prose, removed = strip_reasoning(prose)
    if removed:
        warnings.append(f"stripped {len(removed)} leaked reasoning block(s) from output")

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
            thinking={"type": "adaptive"} if self.cfg.thinking else {"type": "disabled"},
        )

    async def _aquery(self, system_text: str, user_text: str, max_turns: int = 1) -> tuple[str, dict, float]:
        texts: list[str] = []
        usage: dict = {}
        cost = 0.0
        rate_limited = None
        async for msg in query(prompt=user_text, options=self._options(system_text, max_turns)):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
            elif isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                if getattr(info, "status", None) == "rejected":
                    rate_limited = info
            elif isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0
                usage = _extract_usage(msg)
                if msg.is_error:
                    detail = msg.api_error_status or msg.errors or msg.subtype
                    # A 429 is a rate limit — treat it like the rejected RateLimitEvent
                    # so it pauses/resumes gracefully instead of surfacing a raw error.
                    if str(getattr(msg, "api_error_status", "")) == "429" or "429" in str(detail) \
                            or "rate limit" in str(detail).lower():
                        raise RateLimitedError(SimpleNamespace(rate_limit_type="rate_limit", resets_at=None))
                    raise TranslatorError(f"agent error: {detail}")
        if rate_limited is not None:
            raise RateLimitedError(rate_limited)
        return "".join(texts).strip(), usage, cost

    def _call(self, system_text: str, user_text: str, max_turns: int = 1) -> tuple[str, dict, float]:
        """One agent call -> (text, usage dict, plan-equivalent cost)."""
        last: Exception | None = None
        attempts = max(1, self.cfg.api_retry_count)
        for attempt in range(attempts):
            try:
                return asyncio.run(self._aquery(system_text, user_text, max_turns))
            except (RateLimitedError, TranslatorError, CLINotFoundError):
                raise  # don't retry hard limits / config errors
            except (CLIConnectionError, ProcessError) as exc:
                last = exc
                if attempt < attempts - 1:
                    time.sleep(min(2 ** attempt, 30))
        raise TranslatorError(f"agent connection failed after {attempts} attempts: {last}")

    def translate_chapter(
        self,
        chapter: Chapter,
        glossary_entries: list[GlossaryEntry],
        *,
        extra_instruction: str = "",
        retry_reminder: bool = False,
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
            user_text = _build_user_message(chapter.text, extra_instruction=reminder)
            text, u, c = self._call(system_text, user_text)
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
            user_text = _build_user_message(
                "\n\n".join(chunk_paras), continuity=continuity, extra_instruction=reminder
            )
            text, u, c = self._call(system_text, user_text)
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
        ``{english, type, note}`` dicts (best-effort; never raises on bad output)."""
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
                })
        return out

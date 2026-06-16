"""Typed configuration loaded from a TOML file.

Every tunable mentioned in the build spec is exposed here so behaviour can be
changed without touching code. Read with the 3.11+ stdlib ``tomllib``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class AnthropicConfig(BaseModel):
    model: str = "claude-opus-4-8"
    effort: str = "high"
    thinking: bool = True
    # Opus 4.8/4.7 reject `temperature`. Keep it optional; the translator only
    # forwards it when set AND the model is known to accept it.
    temperature: float | None = None
    max_output_tokens: int = 32000
    web_access: bool = False
    api_retry_count: int = 4


class GoogleConfig(BaseModel):
    source_doc_id: str = "REPLACE_WITH_GOOGLE_DOC_ID"
    credentials_file: Path = Path("client_secret.json")
    token_file: Path = Path("token.json")
    flatten_child_tabs: bool = True


class PathsConfig(BaseModel):
    output_dir: Path = Path("chapters")
    glossary_json: Path = Path("glossary.json")
    glossary_md: Path = Path("glossary.md")
    glossary_pending: Path = Path("glossary_pending.json")
    state_file: Path = Path("state.json")
    audit_dir: Path = Path("audit")


class TranslationConfig(BaseModel):
    chunk_threshold: int = 12000
    continuity_paragraphs: int = 3
    include_prev_translation: bool = False
    romanization: str = "Revised Romanization (RR)"
    # Per-novel framing for the system prompt (genre/tone/audience). Empty = a neutral
    # "Korean web novel". Set per project so a fantasy serial isn't framed as romance.
    style_note: str = ""
    # Per-novel free-form instructions appended to every chapter's user message
    # (e.g. "render sound effects in italics", "the protagonist speaks formally").
    extra_instruction: str = ""
    honorific_note: str = (
        "Keep -hyung and similar relational honorifics attached to names; "
        "localize or drop the address particles -ssi, -ya, -ah, -nim into natural English."
    )
    # Automatic AI deep-check after translation: off | flagged | always.
    deep_check: str = "flagged"
    # The source doc mixes Korean tabs with already-translated English tabs. Skip
    # tabs that are already English (below this Hangul fraction) instead of wastefully
    # "translating" English -> English.
    skip_non_korean: bool = True
    min_hangul_fraction: float = 0.15


class ValidationConfig(BaseModel):
    # Korean -> English expands by character count (Korean is denser), so the band
    # is wider than a same-script pair. ~2.0-2.5x is typical and faithful.
    length_ratio_min: float = 1.6
    length_ratio_max: float = 3.2
    paragraph_tolerance: int = 2          # absolute floor
    paragraph_tolerance_pct: float = 0.05  # OR this fraction of source paragraphs, whichever is larger
    dialogue_tolerance: int = 3


class Config(BaseModel):
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found: {path}. Copy config.example.toml to {path} and edit it."
            )
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return cls.model_validate(data)

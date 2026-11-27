#!/usr/bin/env python3
"""Shared TTS pronunciation lexicon helper for NFL Game Scout Shorts renderers."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re

DEFAULT_LEXICON_PATH = Path(__file__).resolve().parent / "tts_pronunciations.json"


@lru_cache(maxsize=4)
def _load_cached_lexicon(resolved_path: Path) -> dict[str, str]:
    raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in pronunciation lexicon: {resolved_path}")
    return {str(k): str(v) for k, v in raw.items()}


def load_pronunciations(path: Path = DEFAULT_LEXICON_PATH) -> dict[str, str]:
    """Load the written-to-phonetic TTS pronunciation lexicon from JSON."""
    return dict(_load_cached_lexicon(path.resolve()))


def _compile_lexicon(lexicon: dict[str, str]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    if not lexicon:
        return None, {}
    # Longest terms first so multi-word or longer substrings match before shorter ones.
    sorted_terms = sorted(lexicon.keys(), key=len, reverse=True)
    escaped = "|".join(re.escape(term) for term in sorted_terms)
    pattern = re.compile(rf"(?<!\w)(?:{escaped})(?!\w)", re.IGNORECASE)
    lower_map = {term.lower(): replacement for term, replacement in lexicon.items()}
    return pattern, lower_map


def apply_tts_pronunciations(text: str, lexicon: dict[str, str] | None = None) -> str:
    """Rewrite known tricky proper nouns to phonetic spellings for spoken TTS only."""
    if not text:
        return text
    active_lexicon = load_pronunciations() if lexicon is None else lexicon
    pattern, lower_map = _compile_lexicon(active_lexicon)
    if pattern is None:
        return text
    return pattern.sub(lambda match: lower_map[match.group(0).lower()], text)

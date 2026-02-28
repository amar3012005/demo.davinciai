"""
Lightweight text normalization helpers for more natural TTS prosody.

Goals:
- Keep semantics intact (no lexical rewriting)
- Improve pause placement and sentence boundaries
- Support mixed Indic + English text
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

_END_PUNCT = {".", "!", "?", "।", "…"}


def _contains_indic(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        # Devanagari + common Indic blocks
        if (
            0x0900 <= code <= 0x097F or  # Devanagari
            0x0980 <= code <= 0x09FF or  # Bengali
            0x0A00 <= code <= 0x0A7F or  # Gurmukhi
            0x0A80 <= code <= 0x0AFF or  # Gujarati
            0x0B00 <= code <= 0x0B7F or  # Oriya/Tamil/Telugu/Kannada/Malayalam split below
            0x0B80 <= code <= 0x0BFF or
            0x0C00 <= code <= 0x0C7F or
            0x0C80 <= code <= 0x0CFF or
            0x0D00 <= code <= 0x0D7F
        ):
            return True
    return False


def _terminal_punct(language: Optional[str], text: str) -> str:
    if language == "en-IN":
        return "."
    return "।" if _contains_indic(text) else "."


def normalize_text(text: str) -> str:
    """Conservative cleanup for better TTS readability."""
    if not text:
        return ""

    t = unicodedata.normalize("NFC", text)

    # Normalize explicit ASCII ellipsis to unicode ellipsis
    t = t.replace("...", "…")

    # Normalize spaces while preserving paragraph breaks
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)

    # Remove space before punctuation, ensure space after inline punctuation where needed
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,.;:!?])(?!\s|$)", r"\1 ", t)

    # Collapse excessive repeated terminal punctuation
    t = re.sub(r"([!?]){2,}", r"\1", t)
    t = re.sub(r"\.{2,}", "…", t)

    return t.strip()


def prepare_segment(text: str, language: Optional[str], is_final: bool) -> str:
    """
    Normalize a segment and make pause intent explicit.

    - Mid-stream segments without terminal punctuation get a comma pause.
    - Final segments get language-appropriate sentence punctuation if missing.
    """
    t = normalize_text(text)
    if not t:
        return ""

    if t[-1] not in _END_PUNCT:
        if is_final:
            t = f"{t}{_terminal_punct(language, t)}"
        else:
            t = f"{t},"

    return t

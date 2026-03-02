"""
Sarvam AI Text Translation Module

Uses Sarvam's dedicated Translation API (POST /translate) instead of LLM chat.
~10x faster than sarvam-m chat completion — single REST call, no streaming overhead.

API: https://docs.sarvam.ai/api-reference-docs/text/translate-text
Model: mayura:v1 (max 1000 chars) — supports all major Indian languages.
Mode: modern-colloquial — casual, code-mixed output ideal for voice assistants.
"""

import os
import json
import asyncio
import httpx
import logging
import re
from pathlib import Path
from typing import AsyncGenerator, Optional, List

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency at runtime
    load_dotenv = None

logger = logging.getLogger(__name__)

# ── Language name → BCP-47 code mapping ──────────────────────────────────────
_LANG_TO_BCP47 = {
    "telugu":    "te-IN",
    "te":        "te-IN",
    "hindi":     "hi-IN",
    "hi":        "hi-IN",
    "tamil":     "ta-IN",
    "ta":        "ta-IN",
    "kannada":   "kn-IN",
    "kn":        "kn-IN",
    "malayalam": "ml-IN",
    "ml":        "ml-IN",
    "bengali":   "bn-IN",
    "bn":        "bn-IN",
    "marathi":   "mr-IN",
    "mr":        "mr-IN",
    "gujarati":  "gu-IN",
    "gu":        "gu-IN",
    "odia":      "od-IN",
    "od":        "od-IN",
    "punjabi":   "pa-IN",
    "pa":        "pa-IN",
}

# Max chars per API call for mayura:v1
_MAX_CHUNK_CHARS = 950  # Leave headroom below 1000 limit


def _resolve_language_code(target_language: str) -> Optional[str]:
    """Resolve a language name/code to BCP-47 format for Sarvam Translate API."""
    return _LANG_TO_BCP47.get(target_language.lower().strip())


def _split_text_for_translation(text: str, max_chars: int = _MAX_CHUNK_CHARS) -> List[str]:
    """
    Split text into chunks ≤ max_chars at sentence boundaries.
    Preserves sentence integrity for better translation quality.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?।])\s+', text)
    current_chunk = ""

    for sentence in sentences:
        if len(current_chunk) + len(sentence) + 1 > max_chars:
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            # If a single sentence exceeds max_chars, split it further
            if len(sentence) > max_chars:
                words = sentence.split()
                current_chunk = ""
                for word in words:
                    if len(current_chunk) + len(word) + 1 > max_chars:
                        if current_chunk.strip():
                            chunks.append(current_chunk.strip())
                        current_chunk = word
                    else:
                        current_chunk += (" " + word if current_chunk else word)
            else:
                current_chunk = sentence
        else:
            current_chunk += (" " + sentence if current_chunk else sentence)

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks or [text]


async def _translate_chunk(
    client: httpx.AsyncClient,
    text: str,
    target_language_code: str,
    api_key: str,
) -> str:
    """Translate a single chunk via Sarvam Translation API."""
    url = "https://api.sarvam.ai/translate"
    headers = {
        "api-subscription-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "input": text,
        "source_language_code": "en-IN",
        "target_language_code": target_language_code,
        "model": "mayura:v1",
        "speaker_gender": "Female",
        "mode": "modern-colloquial",
        "output_script": "spoken-form-in-native",
        "numerals_format": "international",
    }

    response = await client.post(url, headers=headers, json=payload, timeout=12.0)

    if response.status_code != 200:
        error_body = response.text
        logger.error(f"❌ Sarvam Translate API error {response.status_code}: {error_body[:200]}")
        return text  # Fallback to original English

    result = response.json()
    translated = result.get("translated_text", "")
    if not translated:
        logger.warning("⚠️ Sarvam Translate returned empty translated_text, using original")
        return text

    return translated


async def stream_translate_to_native(text: str, target_language: str = "Telugu") -> AsyncGenerator[str, None]:
    """
    Translate English text to an Indian language using Sarvam's Translation API.

    Uses POST /translate (mayura:v1) — dedicated translation model, ~10x faster
    than LLM chat completion. Returns modern-colloquial output with natural
    code-mixing, ideal for TTS voice output.

    Yields translated text in chunks for streaming compatibility with the
    existing RAG pipeline.

    Supported languages: Telugu, Hindi, Tamil, Kannada, Malayalam, Bengali,
    Marathi, Gujarati, Odia, Punjabi.
    """
    # ── Resolve API key ──────────────────────────────────────────
    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key and load_dotenv is not None:
        env_path = Path(__file__).resolve().parents[1] / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        logger.warning("SARVAM_API_KEY is not set. Yielding original text.")
        yield text
        return

    # ── Resolve target language code ─────────────────────────────
    target_code = _resolve_language_code(target_language)
    if not target_code:
        logger.warning(
            f"⚠️ Language '{target_language}' not supported by Sarvam Translate API. "
            f"Yielding original English text."
        )
        yield text
        return

    # ── Split text into translatable chunks ──────────────────────
    chunks = _split_text_for_translation(text)
    logger.info(
        f"🌐 Translating to {target_language} ({target_code}) via Sarvam Translate API "
        f"| {len(chunks)} chunk(s), {len(text)} chars"
    )

    # ── Translate each chunk and yield immediately ───────────────
    try:
        async with httpx.AsyncClient() as client:
            for i, chunk in enumerate(chunks):
                translated = await _translate_chunk(client, chunk, target_code, api_key)
                logger.debug(f"   Chunk {i+1}/{len(chunks)}: '{chunk[:40]}...' → '{translated[:40]}...'")
                yield translated
                # Small space between chunks for natural TTS pacing
                if i < len(chunks) - 1:
                    yield " "

        logger.info(f"✅ Translation to {target_language} completed ({len(chunks)} chunks)")

    except httpx.TimeoutException:
        logger.error(f"⏱️ Sarvam Translate API timeout for {target_language}")
        yield text  # Fallback to original English
    except Exception as e:
        logger.error(f"❌ Sarvam Translate API error: {e}")
        yield text  # Fallback to original English


# ── Language detection helper (unchanged) ────────────────────────────────────

def detect_user_language(query: str) -> str:
    """
    Simple heuristic to detect user's language from their query.
    Returns language name for translation.
    """
    if not query:
        return "Telugu"  # Default

    # Telugu script detection (Unicode range: 0C00-0C7F)
    telugu_chars = sum(1 for c in query if '\u0C00' <= c <= '\u0C7F')
    # Hindi/Devanagari script detection (Unicode range: 0900-097F)
    hindi_chars = sum(1 for c in query if '\u0900' <= c <= '\u097F')
    # Tamil script detection (Unicode range: 0B80-0BFF)
    tamil_chars = sum(1 for c in query if '\u0B80' <= c <= '\u0BFF')
    # Kannada script detection (Unicode range: 0C80-0CFF)
    kannada_chars = sum(1 for c in query if '\u0C80' <= c <= '\u0CFF')
    # Malayalam script detection (Unicode range: 0D00-0D7F)
    malayalam_chars = sum(1 for c in query if '\u0D00' <= c <= '\u0D7F')

    total = len(query.strip())
    if total == 0:
        return "Telugu"

    # If more than 20% is in a specific script, use that language
    if telugu_chars / total > 0.2:
        return "Telugu"
    if hindi_chars / total > 0.2:
        return "Hindi"
    if tamil_chars / total > 0.2:
        return "Tamil"
    if kannada_chars / total > 0.2:
        return "Kannada"
    if malayalam_chars / total > 0.2:
        return "Malayalam"

    # Default to Telugu if no clear script detected
    return "Telugu"

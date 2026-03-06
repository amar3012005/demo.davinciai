"""
visual_copilot/api/analyse_page.py

One-shot page narration — part of the Visual Co-pilot feature set.

Called by the Orchestrator's ws_handler when the user clicks DOM or Vision
in the Analyse Page strip. Returns a natural-language narration of what TARA
sees on the current page, using:
  - DOM mode:    Groq LLM text model (llama-4-scout) with live graph snapshot
  - Vision mode: Groq Vision model (llama-4-scout-17b multimodal) with screenshot

The calling orchestrator then streams the returned text through TTS.
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger("vc.api.analyse_page")

# ── Groq model to use ─────────────────────────────────────────
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

NARRATE_SYSTEM = (
    "You are TARA, an intelligent web co-pilot. "
    "A user has asked you to look at the current page and narrate what you see "
    "in a warm, natural, conversational voice — like a thoughtful assistant "
    "describing the screen to a colleague. "
    "Be specific: mention the main headings, key interactive elements "
    "(buttons, forms, menus), any alerts or important content, and the overall "
    "purpose of the page. "
    "Keep it under 120 words. Do NOT use bullet points or markdown. Speak directly."
)

QA_SYSTEM = (
    "You are TARA, an intelligent web co-pilot. "
    "The user is looking at a web page and has asked a specific question about it. "
    "Answer their question directly using the page content provided. "
    "Be specific and accurate — reference headings, text, links, buttons, or "
    "other elements visible on the page that are relevant to the question. "
    "Keep it under 120 words. Do NOT use bullet points or markdown. Speak directly "
    "in a warm, natural, conversational voice."
)


def _get_api_key() -> str:
    """Resolve Groq API key from environment (same key used by LLM provider)."""
    key = (
        os.environ.get("GROQ_API_KEY")
        or os.environ.get("LLM_API_KEY")
    )
    if not key:
        raise ValueError(
            "No Groq API key found. Set GROQ_API_KEY or LLM_API_KEY env var."
        )
    return key


async def analyse_page_dom(
    *,
    dom_context: list,
    current_url: str,
    page_title: str,
    session_id: str,
    user_question: Optional[str] = None,
) -> str:
    """
    Narrate the page using the live DOM graph (text-only Groq call).

    Args:
        dom_context: List of DOM element dicts from Scanner.scanPageBlueprint()
        current_url: Current page URL
        page_title: Page <title>
        session_id: For logging

    Returns:
        Narration string (≤120 words)
    """
    api_key = _get_api_key()

    # Build compact DOM summary — type, visible text, id for each element
    lines = []
    for el in (dom_context or [])[:120]:
        el_type = el.get("type", "")
        el_text = (el.get("text") or el.get("label") or "").strip()[:80]
        el_id = el.get("id", "")
        if el_text:
            lines.append(f"[{el_type}] {el_text}" + (f" (id={el_id})" if el_id else ""))

    dom_summary = "\n".join(lines[:80]) if lines else "(no readable elements found)"

    if user_question:
        system_prompt = QA_SYSTEM
        user_content = (
            f"Page URL: {current_url}\n"
            f"Page title: {page_title}\n\n"
            f"DOM elements (live graph snapshot):\n{dom_summary}\n\n"
            f"User question: {user_question}"
        )
    else:
        system_prompt = NARRATE_SYSTEM
        user_content = (
            f"Page URL: {current_url}\n"
            f"Page title: {page_title}\n\n"
            f"DOM elements (live graph snapshot):\n{dom_summary}\n\n"
            "Based on the above, narrate what the agent sees on this page."
        )

    payload = {
        "model": GROQ_TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 250,
        "temperature": 0.4,
    }

    logger.info(
        f"[{session_id}] 🧠 Analyse DOM: {len(lines)} elements, "
        f"url={current_url[:60]}"
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            GROQ_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        narration = resp.json()["choices"][0]["message"]["content"].strip()

    logger.info(f"[{session_id}] 🗣️ DOM narration ({len(narration)} chars): {narration[:60]}...")
    return narration


async def analyse_page_vision(
    *,
    screenshot_b64: str,
    current_url: str,
    page_title: str,
    session_id: str,
    dom_context: Optional[list] = None,
    user_question: Optional[str] = None,
) -> str:
    """
    Narrate the page using a screenshot (multimodal Groq Vision call).

    If Groq Vision fails for any reason, falls back to DOM narration
    if dom_context is provided.

    Args:
        screenshot_b64: Base64-encoded JPEG (no data: prefix)
        current_url: Current page URL
        page_title: Page <title>
        session_id: For logging
        dom_context: Optional DOM fallback if vision fails

    Returns:
        Narration string (≤120 words)
    """
    api_key = _get_api_key()

    logger.info(
        f"[{session_id}] 📸 Analyse Vision: screenshot={len(screenshot_b64) // 1024}KB, "
        f"url={current_url[:60]}"
    )

    try:
        if user_question:
            system_prompt = QA_SYSTEM
            text_prompt = (
                f"This is a screenshot of the current page.\n"
                f"URL: {current_url}\n"
                f"Title: {page_title}\n\n"
                f"User question: {user_question}"
            )
        else:
            system_prompt = NARRATE_SYSTEM
            text_prompt = (
                f"This is a screenshot of the current page.\n"
                f"URL: {current_url}\n"
                f"Title: {page_title}\n\n"
                "Please narrate what you see on this page."
            )

        payload = {
            "model": GROQ_VISION_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": text_prompt,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{screenshot_b64}"
                            },
                        },
                    ],
                },
            ],
            "max_tokens": 250,
            "temperature": 0.4,
        }

        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                GROQ_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            narration = resp.json()["choices"][0]["message"]["content"].strip()

        logger.info(
            f"[{session_id}] 🗣️ Vision narration ({len(narration)} chars): {narration[:60]}..."
        )
        return narration

    except Exception as e:
        status_code = None
        if isinstance(e, httpx.HTTPStatusError):
            status_code = e.response.status_code
        # Auth failures from vision provider should never break endpoint UX.
        # Force a graceful DOM fallback (or safe message) instead of bubbling 500.
        if status_code == 401:
            logger.warning(
                f"[{session_id}] ⚠️ Vision narration failed with 401: {e} — "
                f"{'falling back to DOM' if dom_context else 'returning safe fallback narration'}"
            )
            if dom_context:
                return await analyse_page_dom(
                    dom_context=dom_context,
                    current_url=current_url,
                    page_title=page_title,
                    session_id=session_id,
                    user_question=user_question,
                )
            if user_question:
                return "I can’t access vision analysis right now, and no DOM context was provided to answer your question."
            return "I can’t access vision analysis right now. Please try DOM analysis or reconnect the vision API key."

        logger.warning(
            f"[{session_id}] ⚠️ Vision narration failed: {e} — "
            f"{'falling back to DOM' if dom_context else 'no DOM fallback'}"
        )

        if dom_context:
            return await analyse_page_dom(
                dom_context=dom_context,
                current_url=current_url,
                page_title=page_title,
                session_id=session_id,
                user_question=user_question,
            )
        raise


async def analyse_page(
    *,
    analysis_mode: str,
    current_url: str,
    page_title: str,
    session_id: str,
    screenshot_b64: Optional[str] = None,
    dom_context: Optional[list] = None,
    user_question: Optional[str] = None,
) -> str:
    """
    Unified entrypoint for page narration.

    Args:
        analysis_mode: 'dom' or 'vision'
        current_url: Current page URL
        page_title: Page <title>
        session_id: For logging / tracing
        screenshot_b64: Base64 JPEG for vision mode (optional)
        dom_context: DOM element list for dom mode (optional)

    Returns:
        Narration string to be streamed via TTS
    """
    if analysis_mode == "vision" and screenshot_b64:
        return await analyse_page_vision(
            screenshot_b64=screenshot_b64,
            current_url=current_url,
            page_title=page_title,
            session_id=session_id,
            dom_context=dom_context,   # fallback if vision fails
            user_question=user_question,
        )
    else:
        return await analyse_page_dom(
            dom_context=dom_context or [],
            current_url=current_url,
            page_title=page_title,
            session_id=session_id,
            user_question=user_question,
        )

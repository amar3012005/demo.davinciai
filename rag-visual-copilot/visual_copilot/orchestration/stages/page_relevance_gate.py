"""
page_relevance_gate.py

PURPOSE: Synchronous gate that compares the user's intended domain (target_domain
         from MindReader) against the current URL. If different, short-circuits
         to cross_domain_navigate immediately — skipping Hive + DOM entirely.

PLACEMENT: After schema parsing/caching, BEFORE normalize_schema_domain()
           (which overwrites schema.domain with current host).

NO LLM CALLS — pure string comparison.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("vc.stage.page_relevance_gate")


_KNOWN_SITE_MAP = {
    "youtube": "youtube.com",
    "amazon": "amazon.com",
    "google": "google.com",
    "github": "github.com",
    "openai": "openai.com",
    "groq": "groq.com",
    "cartesia": "cartesia.ai",
    "docs.cartesia": "docs.cartesia.ai",
    "myntra": "myntra.com",
    "flipkart": "flipkart.com",
    "reddit": "reddit.com",
}


def _extract_root_domain(host: str) -> str:
    """Extract root domain: 'm.youtube.com' → 'youtube.com'"""
    host = host.lower().strip().rstrip("/")
    # Strip protocol if accidentally included
    if "://" in host:
        try:
            host = urlparse(host).netloc or host
        except Exception:
            pass
    host = host.replace("www.", "")
    parts = host.split(".")
    # Return last two parts (e.g. youtube.com)
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _infer_target_domain_from_goal(goal: str) -> Optional[str]:
    """Best-effort domain extraction when MindReader omits target_domain."""
    g = (goal or "").lower().strip()
    if not g:
        return None

    # Explicit domain mention: "go to docs.cartesia.ai", "open github.com"
    m = re.search(
        r"\b(?:go\s+to|open|navigate\s+to|take\s+me\s+to|switch\s+to|on)\s+([a-z0-9.-]+\.[a-z]{2,})\b",
        g,
    )
    if m:
        return m.group(1).strip(".")

    # Bare domain mention anywhere in utterance
    m_any = re.search(r"\b([a-z0-9.-]+\.[a-z]{2,})\b", g)
    if m_any:
        return m_any.group(1).strip(".")

    # Known product/site names
    for k, d in _KNOWN_SITE_MAP.items():
        if k in g:
            return d
    return None


def run_page_relevance_gate(
    *,
    schema: Any,
    current_url: str,
    goal: str,
    logger: Any = logger,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Compare target_domain (from MindReader) vs current URL host.

    Returns:
        (cross_domain_response, preserved_target_domain)
        - If cross_domain_response is not None → short-circuit the pipeline
        - preserved_target_domain passed through for downstream use
    """
    target_domain = getattr(schema, "target_domain", None)
    inferred_target = None

    # ── No target → continue on current page ──
    if not target_domain or str(target_domain).lower() in ("null", "none", "current", ""):
        inferred_target = _infer_target_domain_from_goal(goal)
        if inferred_target:
            target_domain = inferred_target
            logger.debug(
                f"PAGE_RELEVANCE_GATE: target_domain missing; inferred from goal='{inferred_target}'."
            )
        else:
            logger.debug("PAGE_RELEVANCE_GATE: No target_domain -- continuing on current page.")
            return None, None

    # Normalize: ensure TLD
    target_domain = str(target_domain).lower().strip()
    if "." not in target_domain:
        target_domain += ".com"

    # Extract current host
    current_host = ""
    if current_url:
        try:
            parsed = urlparse(current_url if "://" in current_url else f"http://{current_url}")
            current_host = (parsed.netloc or "").replace("www.", "").lower()
        except Exception:
            current_host = ""

    if not current_host:
        logger.debug("PAGE_RELEVANCE_GATE: No current host, can't compare. Continuing.")
        return None, target_domain

    current_root = _extract_root_domain(current_host)
    target_root = _extract_root_domain(target_domain)

    # ── CASE 1: Already on target domain → continue to DOM interaction ──
    if current_root == target_root:
        logger.debug(
            f"PAGE_RELEVANCE_GATE: Already on target domain "
            f"(current={current_root}, target={target_root}). Continuing."
        )
        return None, target_domain

    # ── CASE 2: Different domain → short-circuit navigate ──
    logger.debug(
        f"PAGE_RELEVANCE_GATE: SHORTCUT! "
        f"current={current_root} != target={target_root}. "
        f"Short-circuiting to cross_domain_navigate."
    )

    target_entity = getattr(schema, "target_entity", target_domain)

    return {
        "success": True,
        "action": {
            "type": "cross_domain_navigate",
            "target_domain": target_domain,
            "target_entity": target_entity,
            "hints": [],
            "speech": f"Taking you to {target_domain} now.",
        },
        "pipeline": "ultimate_tara_page_relevance_gate",
        "confidence": 0.95,
    }, target_domain

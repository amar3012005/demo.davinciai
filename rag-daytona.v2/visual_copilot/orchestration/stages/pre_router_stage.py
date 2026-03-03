import json
import os
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

from visual_copilot.constants import PRE_ROUTER_VISION_MODEL


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


def _root_domain(host_or_url: str) -> str:
    raw = (host_or_url or "").strip().lower()
    if "://" in raw:
        try:
            raw = urlparse(raw).netloc
        except Exception:
            pass
    raw = raw.replace("www.", "").split("/")[0]
    parts = [p for p in raw.split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return raw


def _get_api_key() -> Optional[str]:
    return os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")


def _goal_mentions_target_domain(goal: str, target_domain: str) -> bool:
    g = (goal or "").lower()
    root = _root_domain(target_domain or "")
    if not root:
        return False
    tokens = [t for t in root.replace(".", " ").split() if len(t) > 2]
    return any(t in g for t in tokens)


def _has_explicit_domain_intent(goal: str) -> bool:
    """
    Returns True only when user explicitly asks for a specific site/domain.
    Examples: 'go to docs.cartesia.ai', 'open wikipedia.org', full URL present.
    """
    g = (goal or "").lower().strip()
    if not g:
        return False

    if "http://" in g or "https://" in g:
        return True

    # Explicit domain-like token in utterance
    if re.search(r"\b[a-z0-9-]+\.[a-z]{2,}(?:/[^\s]*)?\b", g):
        return True

    explicit_site_verbs = ("go to", "open", "visit", "navigate to")
    if any(v in g for v in explicit_site_verbs):
        # Only count as explicit if a known site marker/domain token is present too.
        if re.search(r"\b(docs|console|dashboard|wikipedia|reddit|stackoverflow|github|groq|cartesia)\b", g):
            return True

    return False


def _is_generic_external_domain(target_domain: str) -> bool:
    root = _root_domain(target_domain or "")
    generic = {
        "wikipedia.org",
        "stackoverflow.com",
        "stackexchange.com",
        "medium.com",
        "reddit.com",
        "quora.com",
    }
    return root in generic


def _safe_json_parse(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


async def run_pre_router_gate(
    *,
    goal: str,
    current_url: str,
    screenshot_b64: str,
    session_id: str,
    logger: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Vision-based pre-router that runs before MindReader.

    Returns:
        (early_response, decision)
        - early_response: if non-None, pipeline should return immediately.
        - decision: routing hint for downstream stages.
    """
    if not screenshot_b64:
        return None, None

    api_key = _get_api_key()
    if not api_key:
        logger.info("PRE_ROUTER_VISION: skipped (missing API key)")
        return None, None

    # Keep this aligned with analyse_page multimodal style but tuned for routing decisions.
    system_prompt = (
        "You are TARA's visual routing controller.\n"
        "Your job is ONLY to provide tie-breaker routing signals from screenshot + goal + current URL.\n"
        "First reason about these three questions:\n"
        "1) Is goal evidence already visible on this page?\n"
        "2) Is there an obvious next UI control to reach the goal?\n"
        "3) Is this page clearly unrelated to the goal?\n"
        "Return ONLY valid JSON with this exact schema:\n"
        "{\n"
        '  "route": "cross_domain_navigate | current_domain_hive | current_domain_last_mile",\n'
        '  "target_domain": "https://domain/path or null",\n'
        '  "confidence": 0.0,\n'
        '  "reason": "short reason",\n'
        '  "goal_evidence_visible": true,\n'
        '  "obvious_next_control": true,\n'
        '  "page_clearly_unrelated": false\n'
        "}\n"
        "Routing rules (strict):\n"
        "1) Prefer IN-DOMAIN routing by default.\n"
        "2) Use current_domain_last_mile when goal evidence is visible OR obvious next local control exists.\n"
        "3) Use current_domain_hive when location is unclear and strategic navigation hints are needed.\n"
        "4) Use cross_domain_navigate ONLY when the user explicitly requests another website/domain.\n"
        "5) Do NOT route to generic knowledge sites (wikipedia/reddit/stackoverflow/medium/etc.) unless user explicitly asks for that site.\n"
        "6) If uncertain, choose current_domain_hive.\n"
        "Never invent domains. Use null target_domain unless route=cross_domain_navigate.\n"
        "Never output cross_domain_navigate just because the page seems unrelated."
    )
    user_prompt = (
        f"User goal: {goal}\n"
        f"Current URL: {current_url}\n"
        "Inspect the screenshot and decide the route now."
    )
    payload = {
        "model": PRE_ROUTER_VISION_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                    },
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 220,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=18.0) as client:
            resp = await client.post(
                GROQ_ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        data = _safe_json_parse(content)
    except Exception as e:
        logger.warning(f"PRE_ROUTER_VISION failed: {e}")
        return None, None

    route = str(data.get("route", "")).strip().lower()
    allowed_routes = {"cross_domain_navigate", "current_domain_hive", "current_domain_last_mile"}
    if route not in allowed_routes:
        route = "current_domain_hive"
    target_domain = data.get("target_domain")
    confidence = float(data.get("confidence") or 0.0)
    reason = str(data.get("reason") or "").strip()[:180]
    goal_evidence_visible = bool(data.get("goal_evidence_visible", False))
    obvious_next_control = bool(data.get("obvious_next_control", False))
    page_clearly_unrelated = bool(data.get("page_clearly_unrelated", False))

    decision = {
        "route": route,
        "target_domain": target_domain,
        "confidence": confidence,
        "reason": reason,
        "goal_evidence_visible": goal_evidence_visible,
        "obvious_next_control": obvious_next_control,
        "page_clearly_unrelated": page_clearly_unrelated,
    }

    # Normalize noisy LLM output: cross-domain prediction inside same root domain
    # should stay in current-domain routing to avoid confusing handoff behavior/logs.
    if route == "cross_domain_navigate" and target_domain:
        current_root = _root_domain(current_url)
        target_root = _root_domain(str(target_domain))
        if current_root and target_root and current_root == target_root:
            decision["route"] = "current_domain_hive"
            decision["target_domain"] = None
            decision["reason"] = (
                f"normalized_same_root_domain({current_root}): {reason or 'keep in-domain routing'}"
            )[:180]
            logger.info(
                f"PRE_ROUTER_VISION_NORMALIZED session={session_id} "
                f"from=cross_domain_navigate to=current_domain_hive root={current_root}"
            )
            route = decision["route"]
            target_domain = decision["target_domain"]
            reason = decision["reason"]

    # Hard rule: cross-domain only on explicit-domain intent in user goal.
    if route == "cross_domain_navigate" and not _has_explicit_domain_intent(goal):
        decision["route"] = "current_domain_hive"
        decision["target_domain"] = None
        decision["reason"] = (
            f"normalized_cross_domain_requires_explicit_domain_intent: {reason or 'stay in-domain'}"
        )[:180]
        logger.info(
            f"PRE_ROUTER_VISION_NORMALIZED session={session_id} "
            "from=cross_domain_navigate to=current_domain_hive reason=no_explicit_domain_intent"
        )
        route = decision["route"]
        target_domain = decision["target_domain"]
        reason = decision["reason"]

    # Normalize noisy LLM output: cross-domain target not explicitly requested in goal.
    if route == "cross_domain_navigate" and target_domain:
        if not _goal_mentions_target_domain(goal, str(target_domain)):
            decision["route"] = "current_domain_hive"
            decision["target_domain"] = None
            decision["reason"] = (
                f"normalized_no_explicit_domain_in_goal: {reason or 'stay in-domain'}"
            )[:180]
            logger.info(
                f"PRE_ROUTER_VISION_NORMALIZED session={session_id} "
                "from=cross_domain_navigate to=current_domain_hive reason=no_explicit_domain"
            )
            route = decision["route"]
            target_domain = decision["target_domain"]
            reason = decision["reason"]

    # Normalize noisy LLM output: generic external knowledge domains without explicit user request.
    if route == "cross_domain_navigate" and target_domain and _is_generic_external_domain(str(target_domain)):
        if not _goal_mentions_target_domain(goal, str(target_domain)):
            decision["route"] = "current_domain_hive"
            decision["target_domain"] = None
            decision["reason"] = (
                f"normalized_generic_external_domain: {reason or 'stay in-domain'}"
            )[:180]
            logger.info(
                f"PRE_ROUTER_VISION_NORMALIZED session={session_id} "
                f"from=cross_domain_navigate to=current_domain_hive reason=generic_external({_root_domain(str(target_domain))})"
            )
            route = decision["route"]
            target_domain = decision["target_domain"]
            reason = decision["reason"]
    logger.info(
        f"PRE_ROUTER_VISION_DECISION session={session_id} route={route or 'none'} "
        f"target={target_domain or 'none'} conf={confidence:.2f} "
        f"visible={goal_evidence_visible} next_control={obvious_next_control} "
        f"unrelated={page_clearly_unrelated} reason={reason or 'n/a'}"
    )

    if route == "cross_domain_navigate" and target_domain:
        current_root = _root_domain(current_url)
        target_root = _root_domain(str(target_domain))
        if target_root and current_root and target_root != current_root:
            return {
                "success": True,
                "action": {
                    "type": "cross_domain_navigate",
                    "target_domain": str(target_domain),
                    "target_entity": goal,
                    "hints": [],
                    "speech": f"Taking you to {target_domain} now.",
                },
                "pipeline": "ultimate_tara_pre_router_vision",
                "confidence": confidence,
            }, decision

    return None, decision

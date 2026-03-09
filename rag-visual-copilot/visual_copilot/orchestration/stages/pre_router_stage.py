import json
import os
from typing import Any, Dict, Optional, Tuple

import httpx

from visual_copilot.constants import PRE_ROUTER_VISION_MODEL


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


def _get_api_key() -> Optional[str]:
    return os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")


def _safe_json_parse(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _parse_confidence(raw: Any) -> float:
    """
    Parse model confidence robustly.
    Accepts floats, numeric strings, and percentage-like strings.
    Returns clamped [0.0, 1.0].
    """
    if raw is None:
        return 0.0
    try:
        if isinstance(raw, str):
            s = raw.strip().replace("%", "")
            if not s:
                return 0.0
            val = float(s)
            # If model returns 80 for 80%, normalize.
            if val > 1.0:
                val = val / 100.0
        else:
            val = float(raw)
        return max(0.0, min(1.0, val))
    except Exception:
        return 0.0


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
        "You are TARA's visual routing controller. You translate visual patterns and human intent into routing signals.\n"
        "Humans use free language (e.g., 'show me my spend', 'where's my keys', 'is the model up?').\n"
        "Your job is to look at the screenshot, current URL, and human intent to decide the next big step.\n"
        "First, reason about these questions:\n"
        "1) Is the human's goal (or evidence of it) already visible on this page?\n"
        "2) Is there a clear local button/label that matches the soul of the human request?\n"
        "3) Is this page a dead end for this specific human intent?\n"
        "Return ONLY valid JSON with this exact schema:\n"
        "{\n"
        '  "route": "current_domain_hive | current_domain_last_mile",\n'
        '  "confidence": 0.0,\n'
        '  "reason": "short reason (explain the human intent match)",\n'
        '  "goal_evidence_visible": true,\n'
        '  "obvious_next_control": true,\n'
        '  "page_clearly_unrelated": false\n'
        "}\n"
        "Routing rules (Strict & Human-First):\n"
        "1) Use current_domain_last_mile if the human's target is on screen or nearby.\n"
        "2) Use current_domain_hive if the human is asking for something deep and you need a strategic map.\n"
        "Never output external-domain routing."
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
    allowed_routes = {"current_domain_hive", "current_domain_last_mile"}
    if route not in allowed_routes:
        route = "current_domain_hive"
    target_domain = None
    confidence = _parse_confidence(data.get("confidence"))
    reason = str(data.get("reason") or "").strip()[:180]
    goal_evidence_visible = bool(data.get("goal_evidence_visible", False))
    obvious_next_control = bool(data.get("obvious_next_control", False))
    page_clearly_unrelated = bool(data.get("page_clearly_unrelated", False))

    decision = {
        "route": route,
        "target_domain": None,
        "confidence": confidence,
        "reason": reason,
        "goal_evidence_visible": goal_evidence_visible,
        "obvious_next_control": obvious_next_control,
        "page_clearly_unrelated": page_clearly_unrelated,
    }

    # If model provides route/reason but leaves confidence at 0, calibrate a sensible default
    # so downstream gates/logs reflect an actionable signal instead of a misleading zero.
    if confidence <= 0.0:
        if goal_evidence_visible or obvious_next_control:
            confidence = 0.85
        elif route == "current_domain_hive" and reason:
            confidence = 0.70
        elif page_clearly_unrelated:
            confidence = 0.65
        else:
            confidence = 0.55
        decision["confidence"] = confidence

    # Tie-breaker only: keep current-domain routes only.
    decision["route"] = route
    decision["target_domain"] = None

    logger.info(
        f"PRE_ROUTER_VISION_DECISION session={session_id} route={route or 'none'} "
        f"target=none conf={confidence:.2f} "
        f"visible={goal_evidence_visible} next_control={obvious_next_control} "
        f"unrelated={page_clearly_unrelated} reason={reason or 'n/a'}"
    )

    return None, decision

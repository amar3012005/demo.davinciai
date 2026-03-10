"""
screenshot_broker.py

Session-scoped screenshot request broker.

When the compound last-mile agent calls `request_vision`, instead of relying
on a screenshot that was captured before the loop started (which would be stale),
this broker:
  1. Registers a pending asyncio.Future keyed by (session_id, request_id)
  2. Retrieves the session's WebSocket from app.state.active_websockets
  3. Sends a `request_screenshot` message to the browser
  4. The browser captures the page and responds with `screenshot_response`
  5. The WebSocket handler resolves the Future with the base64 image
  6. The tool_executor awaits the Future and gets a fresh screenshot

WebSocket Registration:
  Call `register_session_websocket(app, session_id, ws)` on WS connect.
  Call `unregister_session_websocket(app, session_id)` on WS disconnect.
"""

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Global registry: (session_id, request_id) -> asyncio.Future[str | None]
_pending: Dict[Tuple[str, str], "asyncio.Future[Optional[str]]"] = {}

# Timeout for screenshot response from browser (seconds)
# Increased from 10.0 to 25.0 to accommodate Groq Vision API latency (10-15s for large images)
SCREENSHOT_TIMEOUT = 25.0


# ── WebSocket session registry ────────────────────────────────────────────────

def register_session_websocket(app: Any, session_id: str, websocket: Any) -> None:
    """Register a live WebSocket for a session. Called on WS connect."""
    if not hasattr(app.state, "active_websockets"):
        app.state.active_websockets = {}
    app.state.active_websockets[session_id] = websocket
    logger.debug(f"📸 WS registered for session={session_id}")


def unregister_session_websocket(app: Any, session_id: str) -> None:
    """Remove session WebSocket on disconnect."""
    ws_map = getattr(getattr(app, "state", None), "active_websockets", None) or {}
    ws_map.pop(session_id, None)
    logger.debug(f"📸 WS unregistered for session={session_id}")


def get_session_websocket(app: Any, session_id: str) -> Optional[Any]:
    """Look up the active WebSocket for a session."""
    ws_map = getattr(getattr(app, "state", None), "active_websockets", None) or {}
    return ws_map.get(session_id)


# ── Screenshot request / resolve ─────────────────────────────────────────────

def _make_key(session_id: str, request_id: str) -> Tuple[str, str]:
    return (session_id, request_id)


async def request_screenshot(
    app: Any,
    session_id: str,
    reason: str = "visual analysis",
) -> Optional[str]:
    """
    Request a live screenshot from the browser for a session.

    Primary path (WebSocket registered):
      - Sends a `request_screenshot` message to the browser via WS
      - Awaits the browser's `screenshot_response` future

    Fallback path (no WebSocket — typical for RAG-side calls):
      - Reads from app.state.latest_screenshots, which is kept fresh
        by the Orchestrator calling POST /api/v1/push_screenshot every
        time it receives a new screenshot from the browser.

    Returns:
        Base64 JPEG string, or None on timeout/failure.
    """
    websocket = get_session_websocket(app, session_id)

    if websocket:
        # ── Primary: live WebSocket path ────────────────────────────────────
        request_id = str(uuid.uuid4())[:8]
        key = _make_key(session_id, request_id)

        loop = asyncio.get_event_loop()
        future: asyncio.Future[Optional[str]] = loop.create_future()
        _pending[key] = future

        try:
            await websocket.send_json({
                "type": "request_screenshot",
                "request_id": request_id,
                "reason": reason,
            })
            logger.info(f"📸 SCREENSHOT_REQUEST sent session={session_id} rid={request_id} reason='{reason}'")

            result = await asyncio.wait_for(future, timeout=SCREENSHOT_TIMEOUT)
            if result:
                logger.info(f"📸 SCREENSHOT_RECEIVED session={session_id} rid={request_id} size={len(result) // 1024}KB")
                # Also update the cache so any parallel readers see the fresh image
                cache = getattr(getattr(app, "state", None), "latest_screenshots", None)
                if cache is not None:
                    cache[session_id] = result
            else:
                logger.warning(f"📸 SCREENSHOT_EMPTY session={session_id} rid={request_id}")
            return result

        except asyncio.TimeoutError:
            logger.warning(f"⏰ SCREENSHOT_TIMEOUT session={session_id} rid={request_id} after {SCREENSHOT_TIMEOUT}s")
            return None
        except Exception as e:
            logger.error(f"❌ SCREENSHOT_ERROR session={session_id} rid={request_id}: {e}")
            return None
        finally:
            _pending.pop(key, None)
    else:
        # ── Fallback: read from Orchestrator-pushed cache ────────────────────
        cache = getattr(getattr(app, "state", None), "latest_screenshots", None)
        if cache and session_id in cache:
            cached = cache[session_id]
            size_kb = len(cached) // 1024 if cached else 0
            logger.info(
                f"📸 SCREENSHOT_CACHE_HIT session={session_id} size={size_kb}KB "
                f"(no WebSocket — using Orchestrator-pushed screenshot)"
            )
            return cached
        logger.warning(
            f"📸 SCREENSHOT_UNAVAILABLE session={session_id} — "
            f"no WebSocket and no cached screenshot. "
            f"Ensure Orchestrator is calling POST /api/v1/push_screenshot."
        )
        return None



def resolve_screenshot(session_id: str, request_id: str, image_b64: Optional[str]) -> bool:
    """
    Called by the WebSocket handler when a `screenshot_response` arrives.
    Resolves the pending Future so the compound loop can continue.

    Returns True if a matching pending request was found and resolved.
    """
    key = _make_key(session_id, request_id)
    future = _pending.get(key)
    if future and not future.done():
        future.set_result(image_b64)
        return True
    logger.warning(f"📸 SCREENSHOT_RESOLVE_MISS session={session_id} rid={request_id} (no pending future)")
    return False


# ═══════════════════════════════════════════════════════════════════════
# Mission Context Packet for Vision Fallback
# ═══════════════════════════════════════════════════════════════════════

def build_vision_context_packet(
    *,
    current_goal: str = "",
    failed_evidence_summary: str = "",
    last_attempted_actions: Optional[list] = None,
    evidence_miss_streak: int = 0,
    semantic_repeat_streak: int = 0,
) -> Dict[str, Any]:
    """
    Build a concise mission context packet to accompany a vision request.
    This helps the vision model understand WHY it was called and what to look for.
    """
    actions_str = ""
    if last_attempted_actions:
        actions_str = "; ".join(str(a)[:80] for a in last_attempted_actions[-5:])

    return {
        "current_goal": (current_goal or "")[:200],
        "failed_evidence_summary": (failed_evidence_summary or "no evidence found in DOM text")[:300],
        "last_attempted_actions": actions_str[:300],
        "evidence_miss_streak": evidence_miss_streak,
        "semantic_repeat_streak": semantic_repeat_streak,
        "trigger_reason": (
            "policy_triggered" if evidence_miss_streak >= 3 or semantic_repeat_streak >= 2
            else "manual_request"
        ),
    }


def parse_vision_response(raw_response: str) -> Dict[str, Any]:
    """
    Parse the raw vision model response into structured hints.
    Handles both reasoning-based narrative and structured formats.
    Returns a dict with answer_visible, best_target_id, recommended_tool.
    """
    response_lower = raw_response.lower()

    # Preferred: parse JSON contract if present
    json_payload: Dict[str, Any] = {}
    try:
        import json
        start = raw_response.find("{")
        end = raw_response.rfind("}")
        if start != -1 and end != -1 and end > start:
            json_payload = json.loads(raw_response[start : end + 1])
    except Exception:
        json_payload = {}

    # Detect if answer is visible - look for reasoning indicators
    answer_visible = False
    if isinstance(json_payload, dict) and "answer_visible" in json_payload:
        answer_visible = bool(json_payload.get("answer_visible"))
    else:
        # Parse from reasoning-based narrative
        # Look for positive indicators
        positive_indicators = [
            "answer is visible", "can see the answer", "information is shown",
            "data is displayed", "content shows", "i can see", "answer visible now: yes",
            "the answer appears to be visible", "information is already visible",
            "the data is present", "i can see the", "visible on the screen",
        ]
        # Look for negative indicators
        negative_indicators = [
            "answer is not visible", "cannot see the answer", "information is not shown",
            "data is not displayed", "answer visible now: no", "not visible",
            "the answer is not present", "i cannot see", "not shown on the screen",
            "missing evidence", "still need to", "need to find", "need to navigate",
        ]

        positive_count = sum(1 for ind in positive_indicators if ind in response_lower)
        negative_count = sum(1 for ind in negative_indicators if ind in response_lower)

        # Default to not visible if uncertain
        answer_visible = positive_count > negative_count

    # Try to extract a suggested target ID (IDs are alphanumeric, e.g. t-s0t1lp, t-123, t-abc4)
    import re
    best_target_id = ""
    if isinstance(json_payload, dict):
        best_target_id = str(json_payload.get("best_target_id") or "").strip()
    if not best_target_id:
        # Look for DOM IDs in the text: (t-123), id: t-123, or just t-123
        id_match = re.search(r"\(?(t-[a-z0-9]+)\)?", raw_response, re.IGNORECASE)
        best_target_id = id_match.group(1) if id_match else ""

    # Extract page mode/type from reasoning
    page_mode = ""
    if isinstance(json_payload, dict):
        page_mode = str(json_payload.get("page_mode") or "").strip()[:80]
    if not page_mode:
        # Try to infer from reasoning text
        mode_patterns = [
            r"page[\s/]*(type|mode)[\s:]*(\w+)",
            r"(dashboard|menu|tab|page|screen)[\s:]*(\w+)",
            r"appears to be (?:a|an) (\w+)",
            r"this is (?:a|an) (\w+)",
        ]
        for pattern in mode_patterns:
            m = re.search(pattern, response_lower)
            if m:
                page_mode = m.group(2) if m.lastindex > 1 else m.group(1)
                break

    # Recommend a probe based on what vision found
    recommended_tool = ""
    if isinstance(json_payload, dict):
        recommended_tool = str(json_payload.get("recommended_tool") or "").strip()

    if not recommended_tool:
        # Parse from reasoning text
        tool_patterns = [
            (r"(?:should|could|recommend|suggest).*?(?:click|clicking).*?(?:on|the)", "click_element"),
            (r"(?:should|could|recommend|suggest).*?(?:type|typing|enter|input)", "type_text"),
            (r"(?:should|could|recommend|suggest).*?(?:wait|waiting|pause|delay)", "wait_for_ui"),
            (r"(?:should|could|recommend|suggest).*?(?:scroll|scrolling)", "scroll_page"),
            (r"(?:should|could|recommend|suggest).*?(?:read|re-read|examine|check).*?(?:content|text|page)", "read_page_content"),
            (r"(?:should|could|recommend|suggest).*?(?:complete|finish|done|answer)", "complete_mission"),
        ]

        for pattern, tool in tool_patterns:
            if re.search(pattern, response_lower):
                recommended_tool = tool
                break

    # SECOND PASS: If we found a target ID but the tool isn't 'click_element', double check if the text implies a click
    if best_target_id and recommended_tool != "click_element":
        if any(kw in response_lower for kw in ["click", "press", "select", "interact", "choose"]):
            recommended_tool = "click_element"

    # Fallback logic if still no recommendation
    if not recommended_tool:
        if answer_visible:
            recommended_tool = "complete_mission"
        elif best_target_id:
            recommended_tool = "click_element"
        elif any(kw in response_lower for kw in ["loading", "spinner", "fetching", "wait"]):
            recommended_tool = "wait_for_ui"
        elif any(kw in response_lower for kw in ["scroll", "below", "more content", "down"]):
            recommended_tool = "scroll_page"
        else:
            recommended_tool = "read_page_content"

    # Extract evidence and reasoning summaries
    evidence_summary = ""
    blocking_reason = ""
    best_target_label = ""
    uncertainty_summary = ""
    candidate_targets = []
    actions = []

    if isinstance(json_payload, dict):
        evidence_summary = str(json_payload.get("evidence_summary") or "").strip()[:260]
        blocking_reason = str(json_payload.get("blocking_reason") or "").strip()[:260]
        best_target_label = str(json_payload.get("best_target_label") or "").strip()[:120]
        uncertainty_summary = str(json_payload.get("uncertainty_summary") or "").strip()[:180]
        raw_targets = json_payload.get("candidate_targets") or []
        if isinstance(raw_targets, list):
            for t in raw_targets[:4]:
                if not isinstance(t, dict):
                    continue
                candidate_targets.append(
                    {
                        "target_id": str(t.get("target_id") or "").strip(),
                        "label": str(t.get("label") or "").strip()[:120],
                        "priority": int(t.get("priority", 99) or 99),
                        "why": str(t.get("why") or "").strip()[:180],
                    }
                )

    # Plain-text parsing from reasoning narrative
    if not evidence_summary:
        # Look for "visible" or "can see" statements
        m = re.search(r"(?:i can see|visible|observed|the page shows)[\s:]*([^\.\n]+)", response_lower)
        if m:
            evidence_summary = m.group(1).strip()[:260]

    if not blocking_reason:
        # Look for what's missing or needed
        m = re.search(r"(?:missing|need to|should|could|next step|logical next)[\s:]*([^\.\n]+)", response_lower)
        if m:
            blocking_reason = m.group(1).strip()[:260]

    if not uncertainty_summary:
        # Look for uncertainty expressions
        m = re.search(r"(?:uncertain|unclear|not sure|ambiguous|difficult to|hard to)[\s:]*([^\.\n]+)", response_lower)
        if m:
            uncertainty_summary = m.group(1).strip()[:180]

    if not best_target_label and best_target_id:
        # Try to find label near the ID
        m = re.search(rf"{re.escape(best_target_id)}[^\.\n]*?(?:is|appears|looks|seems)?[^\.\n]*?(\w+(?:\s+\w+){0,3})", raw_response, re.IGNORECASE)
        if m:
            best_target_label = m.group(1).strip()[:120]

    # PARSE ACTION PLAN: [Step 1], [Step 2]
    action_plan_match = re.search(r"action plan[\s:]*(.+)", response_lower)
    if action_plan_match:
        plan_text = action_plan_match.group(1)
        # Split by comma or semicolon
        steps = re.split(r"[,;]", plan_text)
        for step in steps:
            step = step.strip()
            if not step: continue
            
            # Identify tool and ID
            step_tool = "click_element" # default
            if any(kw in step for kw in ["type", "input", "enter"]): step_tool = "type_text"
            elif any(kw in step for kw in ["wait", "pause"]): step_tool = "wait_for_ui"
            elif any(kw in step for kw in ["scroll"]): step_tool = "scroll_page"
            elif any(kw in step for kw in ["read", "examine"]): step_tool = "read_page_content"
            
            step_id_match = re.search(r"(t-[a-z0-9]+)", step, re.IGNORECASE)
            step_id = step_id_match.group(1) if step_id_match else ""
            
            if step_id or step_tool in {"wait_for_ui", "scroll_page", "read_page_content"}:
                actions.append({
                    "tool": step_tool,
                    "target_id": step_id,
                    "target_label": "", # resolved later
                    "text": "", # text extraction would need more regex but this is a good start
                    "press_enter": "enter" in step.lower(),
                    "seconds": 2,
                    "force_click": True,
                    "why": f"Action Plan step: {step[:80]}"
                })

    # Build actions from reasoning
    if not actions:
        if answer_visible:
            actions.append(
                {
                    "tool": "complete_mission",
                    "target_id": "",
                    "target_label": "",
                    "text": "",
                    "press_enter": False,
                    "seconds": 2,
                    "force_click": False,
                    "why": "Answer appears visible based on visual analysis.",
                }
            )
        elif best_target_id:
            # Extract reasoning for why this target
            why_text = "Probe the strongest visible control."
            m = re.search(rf"{re.escape(best_target_id)}[^\.\n]*?(?:because|as|since|to|might|could|would)[^\.\n]*", raw_response, re.IGNORECASE)
            if m:
                why_text = m.group(0).strip()[:220]

            actions.append(
                {
                    "tool": "click_element",
                    "target_id": best_target_id,
                    "target_label": best_target_label[:120],
                    "text": "",
                    "press_enter": False,
                    "seconds": 2,
                    "force_click": True,
                    "why": why_text,
                }
            )
        elif recommended_tool in {"read_page_content", "wait_for_ui", "scroll_page"}:
            actions.append(
                {
                    "tool": recommended_tool,
                    "target_id": "",
                    "target_label": "",
                    "text": "",
                    "press_enter": False,
                    "seconds": 2,
                    "force_click": False,
                    "why": "Recommended based on visual observation and reasoning.",
                }
            )

    return {
        "answer_visible": answer_visible,
        "best_target_id": best_target_id,
        "best_target_label": best_target_label,
        "recommended_tool": recommended_tool,
        "candidate_targets": candidate_targets,
        "recommended_actions": actions,
        "evidence_summary": evidence_summary,
        "blocking_reason": blocking_reason,
        "page_mode": page_mode,
        "uncertainty_summary": uncertainty_summary,
        "raw_summary": raw_response[:500],
    }

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

    # Detect if answer is visible
    answer_visible = bool(json_payload.get("answer_visible")) if isinstance(json_payload, dict) and "answer_visible" in json_payload else any(kw in response_lower for kw in [
        "answer is visible", "can see the answer", "information is shown",
        "data is displayed", "content shows", "i can see", "answer visible now: yes",
    ])

    # Try to extract a suggested target ID (IDs are alphanumeric, e.g. t-s0t1lp, t-123, t-abc4)
    import re
    best_target_id = ""
    if isinstance(json_payload, dict):
        best_target_id = str(json_payload.get("best_target_id") or "").strip()
    if not best_target_id:
        id_match = re.search(r"\b(t-[a-z0-9]+)\b", raw_response, re.IGNORECASE)
        best_target_id = id_match.group(1) if id_match else ""

    # Recommend a probe based on what vision found. Prefer safe observation over forced action.
    recommended_tool = ""
    if isinstance(json_payload, dict):
        recommended_tool = str(json_payload.get("recommended_tool") or "").strip()
    if not recommended_tool:
        tool_match = re.search(
            r"(?:primary tool|recommended tool|safest probe)\s*:\s*(click_element|type_text|wait_for_ui|scroll_page|read_page_content|complete_mission)",
            response_lower,
        )
        if tool_match:
            recommended_tool = tool_match.group(1)
    if not recommended_tool:
        if answer_visible:
            recommended_tool = "complete_mission"
        elif best_target_id:
            recommended_tool = "click_element"
        elif any(kw in response_lower for kw in ["loading", "spinner", "fetching"]):
            recommended_tool = "wait_for_ui"
        elif any(kw in response_lower for kw in ["scroll", "below", "more content"]):
            recommended_tool = "scroll_page"
        else:
            recommended_tool = "read_page_content"

    evidence_summary = ""
    blocking_reason = ""
    best_target_label = ""
    page_mode = ""
    uncertainty_summary = ""
    candidate_targets = []
    actions = []
    if isinstance(json_payload, dict):
        evidence_summary = str(json_payload.get("evidence_summary") or "").strip()[:260]
        blocking_reason = str(json_payload.get("blocking_reason") or "").strip()[:260]
        best_target_label = str(json_payload.get("best_target_label") or "").strip()[:120]
        page_mode = str(json_payload.get("page_mode") or "").strip()[:80]
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
        raw_controls = json_payload.get("candidate_controls") or []
        if isinstance(raw_controls, list) and not candidate_targets:
            for t in raw_controls[:4]:
                if not isinstance(t, dict):
                    continue
                candidate_targets.append(
                    {
                        "target_id": str(t.get("target_id") or "").strip(),
                        "label": str(t.get("label") or "").strip()[:120],
                        "priority": int(t.get("priority", 99) or 99),
                        "why": str(t.get("why") or t.get("reason") or "").strip()[:180],
                    }
                )
        raw_actions = json_payload.get("recommended_actions") or []
        if isinstance(raw_actions, list):
            for a in raw_actions[:4]:
                if not isinstance(a, dict):
                    continue
                tool = str(a.get("tool") or "").strip()
                if tool not in {"click_element", "type_text", "wait_for_ui", "scroll_page", "read_page_content", "complete_mission"}:
                    continue
                actions.append(
                    {
                        "tool": tool,
                        "target_id": str(a.get("target_id") or "").strip(),
                        "target_label": str(a.get("target_label") or "").strip()[:120],
                        "text": str(a.get("text") or "").strip(),
                        "press_enter": bool(a.get("press_enter", False)),
                        "seconds": int(a.get("seconds", 2) or 2),
                        "force_click": bool(a.get("force_click", False)),
                        "why": str(a.get("why") or "").strip()[:220],
                    }
                )

    # Plain-text strategic brief parsing (non-JSON mode)
    if not evidence_summary:
        m = re.search(r"visible evidence\s*:\s*(.+)", raw_response, re.IGNORECASE)
        if m:
            evidence_summary = m.group(1).strip()[:260]
    if not blocking_reason:
        m = re.search(r"missing evidence\s*:\s*(.+)", raw_response, re.IGNORECASE)
        if m:
            blocking_reason = m.group(1).strip()[:260]
    if not page_mode:
        m = re.search(r"page mode\s*:\s*(.+)", raw_response, re.IGNORECASE)
        if m:
            page_mode = m.group(1).strip()[:80]
    if not uncertainty_summary:
        m = re.search(r"uncertainty\s*:\s*(.+)", raw_response, re.IGNORECASE)
        if m:
            uncertainty_summary = m.group(1).strip()[:180]
    if not best_target_label:
        m = re.search(r"best (?:target|visible control)\s*:\s*(.+?)(?:\s*\((?:id=)?[a-zA-Z0-9_\-]+\)|$)", raw_response, re.IGNORECASE)
        if m:
            best_target_label = m.group(1).strip()[:120]
    if not best_target_id:
        m = re.search(r"best (?:target|visible control)\s*:\s*.+?\((?:id=)?([a-zA-Z0-9_\-]+)\)", raw_response, re.IGNORECASE)
        if m:
            best_target_id = m.group(1)

    if not candidate_targets:
        for line in raw_response.splitlines():
            m = re.search(
                r"^\s*\d+\)\s*(.+?)(?:\s*\((?:id=)?([a-zA-Z0-9_\-]+)\))?\s*-\s*(?:confidence=(?:high|medium|low)\s*-\s*)?(.+)$",
                line.strip(),
                re.IGNORECASE,
            )
            if m:
                candidate_targets.append(
                    {
                        "target_id": str(m.group(2) or "").strip(),
                        "label": m.group(1).strip()[:120],
                        "priority": len(candidate_targets) + 1,
                        "why": m.group(3).strip()[:180],
                    }
                )
                if len(candidate_targets) >= 4:
                    break

    if not actions:
        # Derive safe advisory actions from candidate controls when the vision model only observed.
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
                    "why": "Answer appears visible in the screenshot.",
                }
            )
        elif candidate_targets:
            first = candidate_targets[0]
            if first.get("target_id"):
                actions.append(
                    {
                        "tool": "click_element",
                        "target_id": str(first.get("target_id") or "").strip(),
                        "target_label": str(first.get("label") or "").strip()[:120],
                        "text": "",
                        "press_enter": False,
                        "seconds": 2,
                        "force_click": True,
                        "why": str(first.get("why") or "Probe the strongest visible control.").strip()[:220],
                    }
                )
            else:
                actions.append(
                    {
                        "tool": "read_page_content",
                        "target_id": "",
                        "target_label": "",
                        "text": "",
                        "press_enter": False,
                        "seconds": 2,
                        "force_click": False,
                        "why": "Vision found possible controls but no grounded DOM id; re-read visible content first.",
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
                    "why": "Safest probe derived from visual observation.",
                }
            )

    if not actions:
        for line in raw_response.splitlines():
            m = re.search(
                r"^\s*\d+\)\s*(click_element|type_text|wait_for_ui|scroll_page|read_page_content|complete_mission)\s*->\s*(.+?)(?:\s*\|\s*why:\s*(.+))?$",
                line.strip(),
                re.IGNORECASE,
            )
            if not m:
                continue
            tool = m.group(1).strip()
            target_part = (m.group(2) or "").strip()
            why = (m.group(3) or "").strip()
            tid = ""
            tlbl = ""
            idm = re.search(r"\(id=(t-[a-z0-9]+)\)", target_part, re.IGNORECASE)
            if idm:
                tid = idm.group(1)
                tlbl = re.sub(r"\s*\(id=t-[a-z0-9]+\)", "", target_part, flags=re.IGNORECASE).strip()
            else:
                inline_id = re.search(r"\b(t-[a-z0-9]+)\b", target_part, re.IGNORECASE)
                if inline_id:
                    tid = inline_id.group(1)
                    tlbl = target_part.replace(tid, "").strip(" -()")
                else:
                    tlbl = target_part if target_part.lower() != "n/a" else ""
            actions.append(
                {
                    "tool": tool,
                    "target_id": tid,
                    "target_label": tlbl[:120],
                    "text": "",
                    "press_enter": False,
                    "seconds": 2,
                    "force_click": tool == "click_element",
                    "why": why[:220],
                }
            )
            if len(actions) >= 4:
                break

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

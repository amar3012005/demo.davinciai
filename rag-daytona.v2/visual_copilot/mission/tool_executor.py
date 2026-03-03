"""
Tool Executor for the TARA Compound Last-Mile Agent.

Bridges the gap between the LLM's tool calls and TARA's live
DOM validation + Groq Vision multimodal pipeline.

Vision pipeline:
  LLM calls request_vision
    -> broker looks up WebSocket from app.state.active_websockets
    -> sends request_screenshot WS message to browser
    -> browser captures page via html2canvas
    -> browser sends screenshot_response WS message
    -> screenshot_broker resolves the Future
    -> executor calls Groq llama-4-scout-17b-16e-instruct with the base64 image
    -> vision result fed back into the LLM conversation as a tool result
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _is_valid_id(target_id: str, nodes: List[Any]) -> bool:
    """Check if the target_id exists in the current LiveGraph."""
    if not target_id:
        return False
    if target_id.lower() in {"?", "unknown", "none", "id", "missing"} or "t-?" in target_id:
        return False
    for n in nodes:
        if str(getattr(n, "id", "")) == str(target_id):
            return True
    return False


async def _call_groq_vision(
    image_b64: str,
    reason: str,
    nodes: List[Any],
    app: Any = None,
) -> str:
    """
    Call Groq llama-4-scout-17b-16e-instruct with a base64 screenshot.
    Returns a text description grounded against known DOM IDs.
    """
    llm = None
    if app and hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
        llm = app.state.mind_reader.llm

    if not llm or not hasattr(llm, "generate_vision"):
        return "System: Vision model (generate_vision) not available on this LLM provider."

    # Build grounded prompt — include DOM IDs so vision model can map visuals to known elements
    interactive_ids = []
    for n in nodes[:80]:
        if getattr(n, "interactive", False):
            text = (getattr(n, "text", "") or "")[:60]
            tag = getattr(n, "tag", "")
            nid = getattr(n, "id", "")
            interactive_ids.append(f"[{nid}] {tag}: '{text}'")
    id_context = "\n".join(interactive_ids) if interactive_ids else "(no interactive elements)"

    vision_prompt = (
        f"You are TARA's visual analysis system. The agent is looking for: {reason}\n\n"
        f"Describe what you see on this page screenshot. Focus specifically on:\n"
        f"1. Interactive elements visible (buttons, links, dropdowns, tabs, inputs)\n"
        f"2. Any data/content relevant to: {reason}\n"
        f"3. Loading states, empty panels, or errors visible\n"
        f"4. Charts, graphs, or visual data that cannot be read from text DOM\n\n"
        f"KNOWN DOM element IDs for reference (match visual elements to these IDs):\n{id_context}\n\n"
        f"Be specific. If you see a button or element matching the goal, mention its likely DOM ID."
    )

    try:
        result = await llm.generate_vision(
            text_prompt=vision_prompt,
            image_b64=image_b64,
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            max_tokens=512,
            temperature=0.2,
        )
        logger.info(f"👁️ VISION RESULT: {result[:200]}")
        return f"Vision Analysis: {result}"
    except Exception as e:
        logger.error(f"👁️ VISION CALL FAILED: {e}")
        return f"System: Groq Vision call failed ({e}). Rely on the provided DOM text."


async def execute_internal_tool(
    tool_name: str,
    args: Dict[str, Any],
    nodes: List[Any],
    screenshot_b64: Optional[str] = None,
    app: Any = None,
    session_id: str = "",
    excluded_ids: set = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Executes the LLM's chosen tool.

    Args:
        tool_name: Name of the tool the LLM called.
        args: Parsed tool arguments.
        nodes: Current LiveGraph nodes for ID validation.
        screenshot_b64: Pre-captured screenshot (optional, may be None/stale).
        app: FastAPI app — used for LLM access and WebSocket registry lookup.
        session_id: Current session ID for screenshot broker routing.

    Returns:
        is_terminal (bool): If True, break the internal loop and return to frontend.
        tool_result_str (str): Text to feed back to LLM if non-terminal.
        frontend_action (dict | None): Payload to return to frontend if terminal.
    """
    logger.info(f"⚙️ EXECUTING TOOL: {tool_name} | Args: {args}")

    if tool_name == "click_element":
        target_id = args.get("target_id", "")
        if not _is_valid_id(target_id, nodes):
            logger.warning(f"🛡️ GUARDRAIL: Hallucinated ID -> {target_id}")
            return (
                False,
                f"Error: ID '{target_id}' does not exist in the current DOM. "
                f"Please review the provided elements and use a valid ID, "
                f"or call request_vision to see the page visually.",
                None,
            )
        if excluded_ids and target_id in excluded_ids:
            logger.warning(f"🛡️ GUARDRAIL: Re-click blocked -> {target_id} (already clicked)")
            return (
                False,
                f"Error: ID '{target_id}' was already clicked in a previous step. "
                f"Do NOT click it again. Choose a different element or use type_text to search instead.",
                None,
            )
        return True, "", {
            "type": "click",
            "target_id": target_id,
            "text": "",
            "speech": args.get("why", "Clicking element."),
        }

    elif tool_name == "type_text":
        target_id = args.get("target_id", "")
        text = args.get("text", "")
        press_enter = args.get("press_enter", True)
        if not _is_valid_id(target_id, nodes):
            logger.warning(f"🛡️ GUARDRAIL: Hallucinated ID -> {target_id}")
            return (
                False,
                f"Error: ID '{target_id}' does not exist in the current DOM. "
                f"Please review the provided elements.",
                None,
            )
        return True, "", {
            "type": "type_text",
            "target_id": target_id,
            "text": text,
            "press_enter": press_enter,
            "speech": f"Typing '{text}'.",
        }

    elif tool_name == "scroll_page":
        direction = args.get("direction", "down")
        return True, "", {"type": "scroll", "direction": direction, "speech": "Scrolling to reveal more content."}

    elif tool_name == "wait_for_ui":
        seconds = args.get("seconds", 2)
        return True, "", {"type": "wait", "seconds": seconds, "speech": "Waiting briefly for the page to update."}

    elif tool_name == "request_vision":
        # ═══════════════════════════════════════════════════════════════════
        # 👁️ REAL VISION — Groq llama-4-scout-17b-16e-instruct
        #
        # Flow:
        #   1. Try to get a fresh screenshot via the broker (WS round-trip)
        #   2. Fall back to pre-captured screenshot_b64 if broker fails
        #   3. Call Groq Vision API with the image
        #   4. Return result as a tool observation (non-terminal → LLM re-reasons)
        # ═══════════════════════════════════════════════════════════════════
        reason = args.get("reason", "general page analysis")
        logger.info(f"👁️ VISION REQUESTED: {reason}")

        image_b64 = None

        # Prefer a fresh on-demand screenshot via WebSocket broker
        if app and session_id:
            try:
                from visual_copilot.mission.screenshot_broker import request_screenshot
                image_b64 = await request_screenshot(
                    app=app,
                    session_id=session_id,
                    reason=reason,
                )
            except Exception as e:
                logger.error(f"👁️ Screenshot broker error: {e}")

        # Fall back to any pre-captured screenshot from earlier in the request
        if not image_b64 and screenshot_b64:
            logger.info("👁️ Using pre-captured screenshot_b64 as fallback")
            image_b64 = screenshot_b64

        if image_b64:
            vision_result = await _call_groq_vision(
                image_b64=image_b64,
                reason=reason,
                nodes=nodes,
                app=app,
            )
            return False, vision_result, None
        else:
            return (
                False,
                "System: No screenshot could be captured from the browser (WebSocket unavailable or timeout). "
                "Please rely ONLY on the LiveGraph DOM text provided in the mission brief.",
                None,
            )

    elif tool_name == "complete_mission":
        status = args.get("status", "success")
        response = args.get("response", "Task completed.")
        return True, "", {
            "type": "answer",
            "speech": response,
            "text": response,
            "status": status,
        }

    else:
        return False, f"Error: Unknown tool '{tool_name}'.", None

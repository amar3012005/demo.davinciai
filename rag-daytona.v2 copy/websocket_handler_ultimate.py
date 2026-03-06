"""
websocket_handler_ultimate.py

PURPOSE: WebSocket handler updates for Ultimate TARA architecture.
         Adds support for:
         - dom_delta messages from tara_sensor.js
         - New architecture execution flow
         - Mission status queries

DEPENDENCIES:
    - visual_orchestrator_ultimate.py
    - Existing WebSocket infrastructure

MIGRATION STATUS: [INTEGRATION] - Add to existing WebSocket handlers

USAGE:
    # In your existing WebSocket handler, add:
    from websocket_handler_ultimate import handle_ultimate_messages
    
    @websocket("/ws")
    async def websocket_handler(websocket: WebSocket, session_id: str):
        await websocket.accept()
        
        orchestrator = app.state.ultimate_orchestrator
        
        while True:
            message = await websocket.receive_json()
            
            # Check for ultimate messages
            handled = await handle_ultimate_messages(
                websocket, session_id, message, orchestrator
            )
            
            if not handled:
                # Existing message handling...
"""

import json
import logging
import time
from typing import Any, Dict, Optional

from visual_copilot.mission.screenshot_broker import resolve_screenshot

logger = logging.getLogger(__name__)


async def handle_ultimate_messages(
    websocket: Any,
    session_id: str,
    message: Dict[str, Any],
    orchestrator: Any
) -> bool:
    """
    Handle Ultimate TARA specific WebSocket messages.
    
    Args:
        websocket: WebSocket connection
        session_id: Session identifier
        message: Received message dict
        orchestrator: UltimateVisualOrchestrator instance
    
    Returns:
        True if message was handled, False to continue with legacy handling
    """
    msg_type = message.get("type", "")
    
    # Handle dom_delta from tara_sensor.js
    if msg_type == "dom_delta":
        return await handle_dom_delta(websocket, session_id, message, orchestrator)
    
    # Handle mission status query
    elif msg_type == "get_mission_status":
        return await handle_mission_status(websocket, session_id, message, orchestrator)
    
    # Handle constraint update (e.g., user selected size)
    elif msg_type == "update_constraint":
        return await handle_constraint_update(websocket, session_id, message, orchestrator)
    
    # Handle user input with new architecture
    elif msg_type == "user_input" and orchestrator and orchestrator.config.use_new_detective:
        return await handle_user_input_ultimate(websocket, session_id, message, orchestrator)

    # ── NEW: Pre-Flight node validation before any click action ──
    elif msg_type == "preflight_check":
        return await handle_preflight_check(websocket, session_id, message, orchestrator)

    # ── NEW: Cross-domain bridge trigger ──
    elif msg_type == "cross_domain_trigger":
        return await handle_cross_domain_trigger(websocket, session_id, message, orchestrator)

    # ── Vision: Screenshot response from browser ──
    elif msg_type == "screenshot_response":
        return await handle_screenshot_response(websocket, session_id, message, orchestrator)

    return False


async def handle_screenshot_response(
    websocket: Any,
    session_id: str,
    message: Dict[str, Any],
    orchestrator: Any,
) -> bool:
    """
    Handle screenshot_response from the browser widget.

    Message format:
    {
        "type": "screenshot_response",
        "request_id": "abc12345",
        "image_b64": "<base64 JPEG>",   # null on capture failure
        "image_mime": "image/jpeg",
        "error": null,
        "url": "https://...",
        "timestamp": 1234567890
    }
    """
    request_id = message.get("request_id") or ""
    image_b64 = message.get("image_b64") or None
    error = message.get("error")

    if error:
        logger.warning(f"📸 Screenshot capture error from browser: {error} rid={request_id}")

    resolved = resolve_screenshot(session_id, request_id, image_b64)
    if resolved:
        logger.info(f"📸 SCREENSHOT resolved for session={session_id} rid={request_id}")
    else:
        logger.warning(f"📸 No pending screenshot future for rid={request_id} — ignoring")

    return True


async def handle_dom_delta(
    websocket: Any,
    session_id: str,
    message: Dict[str, Any],
    orchestrator: Any
) -> bool:
    """
    Handle DOM delta message from tara_sensor.js.
    
    Message format:
    {
        "type": "dom_delta",
        "delta_type": "full_scan" | "update" | "add" | "remove",
        "nodes": [...],  # For full_scan/add/update
        "changes": [...], # For update
        "removed_ids": [],
        "url": "https://example.com",
        "timestamp": 1234567890
    }
    
    Args:
        websocket: WebSocket connection
        session_id: Session identifier
        message: Delta message
        orchestrator: UltimateVisualOrchestrator instance
    
    Returns:
        True if handled successfully
    """
    if not orchestrator or not orchestrator.live_graph:
        logger.debug("LiveGraph not available, skipping dom_delta")
        return True
    
    try:
        success = await orchestrator.ingest_dom_delta(
            session_id=session_id,
            delta=message
        )
        
        if success:
            logger.debug(f"✅ Ingested DOM delta for {session_id}")
        else:
            logger.warning(f"⚠️ Failed to ingest DOM delta for {session_id}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ DOM delta handling failed: {e}")
        return True


async def handle_user_input_ultimate(
    websocket: Any,
    session_id: str,
    message: Dict[str, Any],
    orchestrator: Any
) -> bool:
    """
    Handle user input with new Ultimate architecture.
    
    Message format:
    {
        "type": "user_input",
        "text": "Export my API usage",
        "url": "https://groq.com/dashboard",
        "mode": "visual-copilot"
    }
    
    Args:
        websocket: WebSocket connection
        session_id: Session identifier
        message: User input message
        orchestrator: UltimateVisualOrchestrator instance
    
    Returns:
        True if handled (regardless of success, as we sent response)
    """
    if not orchestrator:
        return False
    
    try:
        user_input = message.get("text", "")
        current_url = message.get("url", "")
        
        # Send acknowledgment
        await websocket.send_json({
            "type": "processing",
            "status": "analyzing_intent",
            "message": "Understanding your request..."
        })
        
        # Execute with new architecture
        result = await orchestrator.execute_with_new_architecture(
            session_id=session_id,
            user_input=user_input,
            current_url=current_url
        )
        
        # Send result
        await websocket.send_json({
            "type": "action_result",
            **result
        })
        
        logger.info(f"✅ Ultimate architecture executed: {user_input}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ultimate execution failed: {e}")
        
        # Send error to client
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"Execution failed: {str(e)}"
            })
        except:
            pass
        
        return True


async def handle_preflight_check(
    websocket: Any,
    session_id: str,
    message: Dict[str, Any],
    orchestrator: Any
) -> bool:
    """
    Pre-Flight node validation — called right before the executor fires a click.

    The browser extension calls this with the target_id it’s about to click.
    If live_graph says the node is gone/hidden/non-interactive, we tell the
    browser to pause and request a fresh DOM scan instead.

    Message format:
    {
        "type": "preflight_check",
        "target_id": "tara-123"
    }
    """
    if not orchestrator or not getattr(orchestrator, 'live_graph', None):
        # No live graph — allow the click and hope for the best
        await websocket.send_json({"type": "preflight_result", "valid": True, "reason": "no_live_graph"})
        return True

    target_id = message.get("target_id", "")
    if not target_id:
        await websocket.send_json({"type": "preflight_result", "valid": False, "reason": "no_target_id"})
        return True

    try:
        result = await orchestrator.live_graph.preflight_check(session_id, target_id)

        if result["valid"]:
            logger.info(f"✅ Pre-Flight PASSED for '{target_id}' — sending click clearance")
            await websocket.send_json({
                "type": "preflight_result",
                "valid": True,
                "reason": "ok",
                "target_id": target_id
            })
        else:
            logger.warning(
                f"⚠️ Pre-Flight BLOCKED '{target_id}': {result['reason']} — "
                f"requesting fresh DOM scan from browser"
            )
            await websocket.send_json({
                "type": "preflight_result",
                "valid": False,
                "reason": result["reason"],
                "target_id": target_id,
                # Tell the browser's tara_sensor.js to fire a new full_scan immediately
                "request_rescan": True
            })

    except Exception as e:
        logger.error(f"❌ preflight_check WS handler failed: {e}")
        # Fail-safe: allow click if we can't verify (better than hanging)
        await websocket.send_json({"type": "preflight_result", "valid": True, "reason": f"error:{e}"})

    return True


async def handle_cross_domain_trigger(
    websocket: Any,
    session_id: str,
    message: Dict[str, Any],
    orchestrator: Any
) -> bool:
    """
    Cross-Domain Bridge Trigger.

    When hive_interface.retrieve() returns 0 visual hints for the current domain,
    ultimate_api.py calls hive_interface.retrieve_cross_domain() and—if a bridge
    target is found—sends this message so we can forward the navigation instruction
    to the browser.

    Message format:
    {
        "type": "cross_domain_trigger",
        "target_domain": "console.groq.com",
        "target_entity": "Kilo Code",
        "hints": [ ... VisualHint dicts ... ]
    }
    """
    target_domain = message.get("target_domain", "")
    target_entity = message.get("target_entity", "")
    hints = message.get("hints", [])

    if not target_domain:
        logger.warning("⚠️ cross_domain_trigger received without target_domain")
        return True

    logger.info(
        f"🌐 Cross-Domain Bridge: routing '{target_entity}' → '{target_domain}' "
        f"with {len(hints)} pre-loaded visual hints"
    )

    try:
        # Forward the bridge navigation command to the browser overlay
        await websocket.send_json({
            "type": "cross_domain_navigate",
            "target_domain": target_domain,
            "target_entity": target_entity,
            "hints": hints,
            # Tell the browser to navigate to the best matching URL on the target domain
            "action": "navigate",
            "speech": f"I found '{target_entity}' on {target_domain}. Navigating there now."
        })
        logger.info(f"✅ Cross-Domain Bridge sent to browser for {target_domain}")
    except Exception as e:
        logger.error(f"❌ cross_domain_trigger WS handler failed: {e}")

    return True


async def handle_mission_status(
    websocket: Any,
    session_id: str,
    message: Dict[str, Any],
    orchestrator: Any
) -> bool:
    """
    Handle mission status query.
    
    Message format:
    {
        "type": "get_mission_status",
        "mission_id": "mission-abc123"
    }
    
    Args:
        websocket: WebSocket connection
        session_id: Session identifier
        message: Status query message
        orchestrator: UltimateVisualOrchestrator instance
    
    Returns:
        True if handled
    """
    if not orchestrator or not orchestrator.mission_brain:
        return False
    
    try:
        mission_id = message.get("mission_id")
        if not mission_id:
            await websocket.send_json({
                "type": "error",
                "message": "mission_id required"
            })
            return True
        
        status = await orchestrator.get_mission_status(mission_id)
        
        if status:
            await websocket.send_json({
                "type": "mission_status",
                **status
            })
        else:
            await websocket.send_json({
                "type": "error",
                "message": "Mission not found"
            })
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Mission status query failed: {e}")
        return True


async def handle_constraint_update(
    websocket: Any,
    session_id: str,
    message: Dict[str, Any],
    orchestrator: Any
) -> bool:
    """
    Handle constraint update (e.g., user selected a size).
    
    Message format:
    {
        "type": "update_constraint",
        "mission_id": "mission-abc123",
        "constraint_name": "size",
        "value": "medium"
    }
    
    Args:
        websocket: WebSocket connection
        session_id: Session identifier
        message: Constraint update message
        orchestrator: UltimateVisualOrchestrator instance
    
    Returns:
        True if handled
    """
    if not orchestrator or not orchestrator.mission_brain:
        return False
    
    try:
        mission_id = message.get("mission_id")
        constraint_name = message.get("constraint_name")
        value = message.get("value")
        
        if not all([mission_id, constraint_name, value]):
            await websocket.send_json({
                "type": "error",
                "message": "mission_id, constraint_name, and value required"
            })
            return True
        
        success = await orchestrator.update_constraint(
            mission_id=mission_id,
            constraint_name=constraint_name,
            value=value
        )
        
        if success:
            # Get updated mission status
            status = await orchestrator.get_mission_status(mission_id)
            
            await websocket.send_json({
                "type": "constraint_updated",
                "constraint_name": constraint_name,
                "value": value,
                "mission_status": status
            })
            
            logger.info(f"✅ Constraint updated: {constraint_name} = {value}")
        else:
            await websocket.send_json({
                "type": "error",
                "message": "Failed to update constraint"
            })
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Constraint update failed: {e}")
        return True


# ═══════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY WRAPPERS
# ═══════════════════════════════════════════════════════════════

async def process_dom_update_legacy(
    session_id: str,
    dom_elements: list,
    orchestrator: Any
) -> None:
    """
    Convert legacy dom_update to dom_delta format.
    For backward compatibility during migration.
    
    Args:
        session_id: Session identifier
        dom_elements: List of DOM elements from legacy widget
        orchestrator: UltimateVisualOrchestrator instance
    """
    if not orchestrator or not orchestrator.live_graph:
        return
    
    # Convert to delta format
    delta = {
        "delta_type": "full_scan",
        "nodes": dom_elements,
        "url": "",
        "timestamp": time.time()
    }
    
    await orchestrator.ingest_dom_delta(session_id, delta)

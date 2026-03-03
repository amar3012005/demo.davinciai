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
SCREENSHOT_TIMEOUT = 10.0


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

    Looks up the WebSocket from app.state.active_websockets, sends a
    `request_screenshot` message, and awaits the browser's response.

    Args:
        app: FastAPI application (for websocket registry lookup).
        session_id: Current session identifier.
        reason: Description of what visual context is needed.

    Returns:
        Base64 JPEG string, or None on timeout/failure.
    """
    websocket = get_session_websocket(app, session_id)
    if not websocket:
        logger.warning(f"📸 No active WebSocket for session={session_id}, cannot request screenshot")
        return None

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

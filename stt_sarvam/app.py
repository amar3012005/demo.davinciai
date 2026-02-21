"""
Sarvam AI Speech-to-Text-Translate Microservice

A drop-in replacement for stt_groq_whisper that uses Sarvam's native
WebSocket streaming API. Key advantages:
  - True streaming (no micro-chunking workaround)
  - Server-side VAD with flush for instant finals
  - Lower end-to-end latency

Endpoints:
  WS  /api/v1/transcribe/stream  — Real-time STT streaming
  GET /                           — Service info
  GET /health                     — Health check (with ?deep=true)
  GET /metrics                    — Runtime metrics
  GET /client                     — Browser test client
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketState

from config import SarvamSTTConfig
from session_manager import SarvamSTTSession

# ── Logging ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sarvam_stt")

# ── Globals ─────────────────────────────────────────────────────

config: SarvamSTTConfig = None  # type: ignore
active_sessions: Dict[str, SarvamSTTSession] = {}
start_time: float = 0.0
total_connections: int = 0


# ── App lifecycle ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, start_time
    start_time = time.time()

    config = SarvamSTTConfig.from_env()
    config.log_config()

    if not config.api_key:
        logger.error("SARVAM_API_KEY is required — set it as an environment variable")

    logger.info("Sarvam STT service ready")
    yield

    # Shutdown: close all sessions
    logger.info(f"Shutting down — closing {len(active_sessions)} session(s)")
    close_tasks = [session.stop() for session in active_sessions.values()]
    if close_tasks:
        await asyncio.gather(*close_tasks, return_exceptions=True)
    active_sessions.clear()
    logger.info("Sarvam STT service stopped")


app = FastAPI(
    title="Sarvam STT Service",
    version="1.0.0",
    lifespan=lifespan,
)


# ── WebSocket endpoint ──────────────────────────────────────────

@app.websocket("/api/v1/transcribe/stream")
async def transcribe_stream(
    websocket: WebSocket,
    session_id: str = Query(default=""),
):
    global total_connections

    await websocket.accept()
    total_connections += 1

    # Generate session ID if not provided
    if not session_id:
        session_id = f"sarvam_{int(time.time())}_{id(websocket) % 10000}"

    logger.info(f"[{session_id}] Client connected")

    # Create and start session
    session = SarvamSTTSession(
        session_id=session_id,
        config=config,
        client_ws=websocket,
    )
    active_sessions[session_id] = session

    started = await session.start()

    # Send connection confirmation (matches groq_whisper contract)
    await websocket.send_json({
        "type": "connected",
        "session_id": session_id,
        "timestamp": time.time(),
        "message": "Sarvam STT streaming ready",
        "config": {
            "model": config.model,
            "sample_rate": config.sample_rate,
            "codec": config.input_audio_codec,
            "vad_signals": config.enable_vad_signals,
        },
    })

    if not started:
        await websocket.send_json({
            "type": "error",
            "message": "Failed to connect to Sarvam API. Check SARVAM_API_KEY.",
        })
        await _cleanup_session(session_id)
        await websocket.close()
        return

    # ── Main receive loop ───────────────────────────────────────

    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                # Keepalive
                if websocket.client_state == WebSocketState.CONNECTED:
                    try:
                        await websocket.send_json({
                            "type": "ping",
                            "timestamp": time.time(),
                        })
                    except Exception:
                        break
                continue

            if "bytes" in message and message["bytes"]:
                # Raw PCM audio
                await session.process_audio(message["bytes"])

            elif "text" in message and message["text"]:
                # JSON control message
                try:
                    msg = json.loads(message["text"])
                    msg_type = msg.get("type", "")

                    if msg_type == "ping":
                        await websocket.send_json({
                            "type": "pong",
                            "timestamp": time.time(),
                        })
                    elif msg_type == "flush":
                        # Client-initiated flush
                        await session.flush_remaining_audio()
                        if session.sarvam:
                            session.flush_pending = True
                            await session.sarvam.send_flush()
                    elif msg_type == "config":
                        # Update context prompt mid-session
                        prompt = msg.get("prompt", "")
                        if prompt and session.sarvam and session.sarvam.is_connected:
                            await session.sarvam._send_config(prompt)
                    else:
                        logger.debug(f"[{session_id}] Unknown message type: {msg_type}")

                except json.JSONDecodeError:
                    logger.warning(f"[{session_id}] Invalid JSON from client")

            elif message.get("type") == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] Client disconnected")
    except Exception as e:
        logger.error(f"[{session_id}] WebSocket error: {e}")
    finally:
        await _cleanup_session(session_id)
        logger.info(f"[{session_id}] Session cleaned up")


async def _cleanup_session(session_id: str):
    """Remove and stop a session."""
    session = active_sessions.pop(session_id, None)
    if session:
        await session.stop()


# ── REST endpoints ──────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "sarvam-stt",
        "version": "1.0.0",
        "model": config.model if config else "loading",
        "endpoints": {
            "ws_stream": "/api/v1/transcribe/stream",
            "health": "/health",
            "metrics": "/metrics",
            "client": "/client",
        },
    }


@app.get("/health")
async def health(deep: bool = False):
    uptime = time.time() - start_time
    status = "healthy"

    result = {
        "status": status,
        "service": "sarvam-stt",
        "uptime_seconds": round(uptime, 1),
        "config": {
            "model": config.model,
            "sample_rate": config.sample_rate,
            "codec": config.input_audio_codec,
            "vad_signals": config.enable_vad_signals,
        },
        "sessions": len(active_sessions),
    }

    if deep:
        # Test Sarvam connectivity
        from sarvam_client import SarvamWebSocketClient

        test_client = SarvamWebSocketClient(config=config)
        try:
            connected = await test_client.connect()
            if connected:
                result["upstream"] = {"sarvam": "reachable"}
                await test_client.disconnect()
            else:
                result["upstream"] = {"sarvam": "unreachable"}
                result["status"] = "degraded"
        except Exception as e:
            result["upstream"] = {"sarvam": f"error: {e}"}
            result["status"] = "degraded"

    return JSONResponse(result)


@app.get("/metrics")
async def metrics():
    session_metrics = {
        sid: s.get_metrics() for sid, s in active_sessions.items()
    }
    return {
        "service": "sarvam-stt",
        "uptime_seconds": round(time.time() - start_time, 1),
        "total_connections": total_connections,
        "active_sessions": len(active_sessions),
        "sessions": session_metrics,
        "config": {
            "model": config.model,
            "sample_rate": config.sample_rate,
            "forward_interval_ms": config.forward_interval_ms,
            "vad_signals": config.enable_vad_signals,
            "flush_signal": config.enable_flush_signal,
        },
    }


@app.get("/client")
async def client_page():
    client_path = Path(__file__).parent / "static" / "client.html"
    if client_path.exists():
        return HTMLResponse(client_path.read_text())
    return HTMLResponse("<h1>Client not found</h1>", status_code=404)


# ── Entry point ─────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    _config = SarvamSTTConfig.from_env()
    uvicorn.run(
        "app:app",
        host=_config.host,
        port=_config.port,
        workers=1,  # Single worker — shared session state
        log_level=_config.log_level.lower(),
    )

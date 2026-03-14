"""
TTS-Cartesia Streaming Microservice FastAPI Application

WebSocket-based TTS streaming service using Cartesia AI's sonic model.
Optimized for ultra-low latency (~40ms first audio chunk).

Endpoints:
- GET /health - Health check
- GET /metrics - Service metrics
- GET /client - Testing UI
- WS /api/v1/stream - Main streaming endpoint
- POST /api/v1/synthesize - HTTP synthesis
"""

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import CartesiaConfig
from cartesia_manager import CartesiaManager

# Import rate limiter
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
from rate_limiter import RateLimitMiddleware, WebSocketRateLimiter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# Reduce noise from libraries
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# Global state
config: Optional[CartesiaConfig] = None
manager: Optional[CartesiaManager] = None


# =============================================================================
# Session Management
# =============================================================================

class SessionState:
    """Track state for a WebSocket session"""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.context_id = str(uuid.uuid4())  # For prosody continuity
        self.is_streaming = False
        self.audio_queue: asyncio.Queue = asyncio.Queue()
        self.turn_done_event = asyncio.Event()
        self.created_at = time.time()
        self.chunks_sent = 0
        self.total_audio_bytes = 0
        self.last_activity = time.time()
        
        # Context for streaming turn
        self.current_voice: Optional[str] = None
        self.current_model: Optional[str] = None
        self.current_language: Optional[str] = None

active_sessions: Dict[str, SessionState] = {}


# =============================================================================
# Application Lifecycle
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler for application startup/shutdown"""
    global config, manager
    
    logger.info("=" * 70)
    logger.info("🚀 Starting TTS-Cartesia Microservice")
    logger.info("=" * 70)
    
    try:
        # Load configuration
        config = CartesiaConfig.from_env()
        logger.info("✅ Configuration loaded")
        
        # Initialize Cartesia manager
        manager = CartesiaManager(config)
        
        # Pre-warm connection pool
        await manager.warmup()
        logger.info("✅ Connection pool warmed")
        
        logger.info("=" * 70)
        logger.info("✅ TTS-Cartesia microservice ready")
        logger.info(f"   Model: {config.model}")
        logger.info(f"   Voice: {config.voice_id}")
        logger.info(f"   Sample rate: {config.sample_rate}Hz")
        logger.info(f"   Output format: {config.output_format}")
        logger.info("=" * 70)
        
        app.state.start_time = time.time()
        yield
        
    except Exception as e:
        logger.error(f"❌ Startup error: {e}")
        raise
    finally:
        # Cleanup
        logger.info("Shutting down TTS-Cartesia...")
        if manager:
            await manager.close()
        
        # Close active sessions
        for session_id, session in list(active_sessions.items()):
            logger.info(f"Closing session {session_id}")
        active_sessions.clear()
        
        logger.info("TTS-Cartesia shutdown complete")


app = FastAPI(
    title="TTS-Cartesia",
    description="Ultra-low latency Text-to-Speech streaming using Cartesia AI",
    version="1.0.0",
    lifespan=lifespan
)

# Rate limiting middleware - 100 requests/minute per IP
app.add_middleware(
    RateLimitMiddleware,
    redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
    default_requests=100,
    default_window=60,
    exempt_paths=["/health", "/metrics", "/", "/client"]
)

# CORS middleware - RESTRICTED to known origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "https://demo.davinciai.eu,https://enterprise.davinciai.eu").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# =============================================================================
# Static Files
# =============================================================================

static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


# =============================================================================
# HTTP Endpoints
# =============================================================================

@app.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": "TTS-Cartesia Streaming Service",
        "version": "1.0.0",
        "model": config.model if config else None,
        "voice_id": config.voice_id if config else None,
        "endpoints": {
            "websocket": "/api/v1/stream",
            "synthesize": "/api/v1/synthesize",
            "health": "/health",
            "metrics": "/metrics",
            "client": "/client"
        }
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    if not manager:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": "Manager not initialized"}
        )
    
    stats = manager.get_stats()
    available = stats.get("available_connections", 0)
    
    if available == 0 and stats.get("is_warmed"):
        return JSONResponse(
            status_code=200,
            content={"status": "degraded", "error": "No available connections"}
        )
    
    return {
        "status": "healthy",
        "model": config.model if config else None,
        "pool_size": stats.get("pool_size", 0),
        "available_connections": available,
        "active_sessions": len(active_sessions),
    }


@app.get("/metrics")
async def metrics():
    """Get service metrics"""
    if not manager:
        return {"error": "Manager not initialized"}
    
    stats = manager.get_stats()
    
    return {
        "service": "tts-cartesia",
        "uptime_seconds": time.time() - app.state.start_time if hasattr(app.state, 'start_time') else 0,
        "model": config.model if config else None,
        "sample_rate": config.sample_rate if config else None,
        "pool": stats,
        "active_sessions": len(active_sessions),
        "session_details": [
            {
                "session_id": s.session_id[:8] + "...",
                "chunks_sent": s.chunks_sent,
                "total_audio_bytes": s.total_audio_bytes,
                "age_seconds": time.time() - s.created_at,
            }
            for s in active_sessions.values()
        ]
    }


@app.get("/client", response_class=HTMLResponse)
async def get_client():
    """Serve the client testing interface"""
    client_path = os.path.join(static_dir, "client.html")
    if os.path.exists(client_path):
        with open(client_path, "r") as f:
            return f.read()
    return """
    <html>
    <body>
        <h1>TTS-Cartesia Test Client</h1>
        <p>client.html not found. Please create it in the static directory.</p>
    </body>
    </html>
    """


# =============================================================================
# HTTP Synthesis Endpoint
# =============================================================================

class SynthesizeRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None
    language: Optional[str] = None
    pronunciation_dict_id: Optional[str] = None


@app.post("/api/v1/synthesize")
async def synthesize(request: SynthesizeRequest):
    """
    Synthesize text to audio (HTTP endpoint).
    
    Returns base64-encoded audio data.
    """
    if not manager:
        raise HTTPException(status_code=503, detail="Service not ready")
    
    start_time = time.time()
    
    audio_bytes = await manager.synthesize(
        text=request.text,
        voice_id=request.voice_id,
        language=request.language,
        pronunciation_dict_id=request.pronunciation_dict_id,
    )
    
    if not audio_bytes:
        raise HTTPException(status_code=500, detail="Synthesis failed")
    
    latency_ms = (time.time() - start_time) * 1000
    
    return {
        "audio": base64.b64encode(audio_bytes).decode("utf-8"),
        "sample_rate": config.sample_rate,
        "format": config.output_format,
        "size_bytes": len(audio_bytes),
        "latency_ms": latency_ms
    }


# =============================================================================
# WebSocket Streaming Endpoint
# =============================================================================

# Global WebSocket rate limiter
ws_rate_limiter = WebSocketRateLimiter(
    max_connections_per_ip=10,
    max_messages_per_minute=60
)

@app.websocket("/api/v1/stream")
async def websocket_stream(
    websocket: WebSocket,
    session_id: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for real-time TTS streaming.

    Protocol:
    - Client sends: {"type": "prewarm"} - Optional warmup
    - Client sends: {"type": "synthesize", "text": "...", "voice": "...", "language": "..."}
    - Client sends: {"type": "stream_chunk", "text": "...", "emotion": "..."}
    - Client sends: {"type": "stream_end"}
    - Server sends: {"type": "audio", "data": "<base64>", "sample_rate": 24000}
    - Server sends: {"type": "complete"}
    """
    # Rate limit: Check connection limit per IP
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not ws_rate_limiter.can_connect(client_ip):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Connection limit exceeded")
        logger.warning(f"WebSocket connection rejected for {client_ip}: too many connections")
        return

    await websocket.accept()

    # Generate session ID if not provided
    session_id = session_id or f"ws_{uuid.uuid4().hex[:16]}"
    tenant_id = os.getenv("TENANT_ID", "tenant")
    
    # Create session state
    session = SessionState(session_id)
    active_sessions[session_id] = session
    
    logger.info("============================================================")
    logger.info("============================================================")
    logger.info(f"🔌 TTS SESSION START | TENANT_ID={tenant_id} | SESSION_ID={session_id}")
    logger.info("============================================================")
    logger.info("============================================================")
    
    # Send connection confirmation
    await websocket.send_json({
        "type": "connected",
        "session_id": session_id,
        "context_id": session.context_id,
        "model": config.model if config else None,
        "voice_id": config.voice_id if config else None,
        "sample_rate": config.sample_rate if config else 44100,
        "format": config.output_format if config else "pcm_f32le",
    })

    def sanitize_config(incoming_voice: Optional[str], incoming_model: Optional[str]):
        """
        Sanitize incoming voice and model IDs.
        If they look like ElevenLabs IDs or are empty, fall back to Cartesia defaults.
        """
        final_voice = incoming_voice
        final_model = config.model

        # 1. Sanitize Voice ID
        # Cartesia uses UUIDs (e.g. "a0e99841-438c-4a64-b679-ae501e7d6091")
        # ElevenLabs uses ~20 char alphanumeric (e.g. "AnvlJBAqSLDzEevYr9Ap")
        if not final_voice or len(final_voice.strip()) == 0:
            final_voice = config.voice_id
        elif len(final_voice) < 15 or "-" not in final_voice:
            # Simple heuristic: if no hyphens and not a UUID, it's likely an ElevenLabs ID or generic
            logger.warning(f"[{session_id}] Detected likely incompatible voice ID: '{final_voice}'. Falling back to default.")
            final_voice = config.voice_id
        
        # 2. Sanitize Model ID
        # Cartesia models: "sonic-2", "sonic-3"
        # ElevenLabs models: "eleven_turbo_v2_5", etc.
        if incoming_model and "sonic" not in incoming_model.lower():
            logger.warning(f"[{session_id}] Detected likely incompatible model ID: '{incoming_model}'. Falling back to default.")
            final_model = config.model
        elif incoming_model:
            final_model = incoming_model

        return final_voice, final_model
    
    # Text accumulator for streaming
    text_buffer: list[str] = []
    stream_task: Optional[asyncio.Task] = None
    
    async def audio_sender():
        """Background task to send audio from queue to client"""
        while True:
            try:
                audio_data = await asyncio.wait_for(session.audio_queue.get(), timeout=1.0)
                if audio_data is None:  # Sentinel for turn end
                    session.turn_done_event.set()
                    session.audio_queue.task_done()
                    continue
                
                audio_bytes, metadata = audio_data
                session.chunks_sent += 1
                session.total_audio_bytes += len(audio_bytes)
                session.last_activity = time.time()
                
                # Send raw binary audio chunk for maximum efficiency
                await websocket.send_bytes(audio_bytes)
                session.audio_queue.task_done()
                
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"[{session_id}] Audio sender error: {e}")
                break
    
    # Start audio sender task
    sender_task = asyncio.create_task(audio_sender())
    
    try:
        while True:
            try:
                # Receive message with timeout
                raw_message = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)

                # Rate limit: Check message rate per session
                if not await ws_rate_limiter.check_message_rate(session_id):
                    await websocket.send_json({
                        "type": "error",
                        "message": "Message rate limit exceeded. Max 60 messages per minute."
                    })
                    continue

                message = json.loads(raw_message)
                msg_type = message.get("type")
                
                session.last_activity = time.time()
                
                if msg_type == "prewarm":
                    # Prewarm is a no-op since we pre-warm the pool
                    logger.info(f"[{session_id}] Prewarm request")
                    await websocket.send_json({"type": "prewarm_done"})
                
                elif msg_type == "synthesize":
                    # Full synthesis request
                    text = message.get("text", "")
                    raw_voice = message.get("voice")
                    raw_model = message.get("model")
                    language = message.get("language")
                    pron_dict = message.get("pronunciation_dict_id")
                    
                    # Sanitize inputs
                    voice_id, model_id = sanitize_config(raw_voice, raw_model)
                    
                    if not text:
                        await websocket.send_json({"type": "error", "message": "No text provided"})
                        continue
                    
                    logger.info(f"[{session_id}] Synthesize (voice={voice_id}, model={model_id}): '{text[:50]}...'")
                    session.is_streaming = True
                    
                    # Audio callback to queue audio for sender
                    def audio_callback(audio_bytes: bytes, sample_rate: int, metadata: Dict[str, Any]):
                        session.audio_queue.put_nowait((audio_bytes, metadata))
                    
                    # Renew Context ID for every single synthesis request since Cartesia closes them instantly upon completion
                    turn_ctx_id = f"{session.context_id}-{uuid.uuid4().hex[:6]}"
                    
                    # Run synthesis
                    stats = await manager.stream_text_to_audio(
                        text,
                        audio_callback,
                        context_id=turn_ctx_id,
                        voice_id=voice_id,
                        model_id=model_id,
                        language=language,
                        pronunciation_dict_id=pron_dict,
                    )
                    
                    session.is_streaming = False
                    
                    # Signal turn end and WAIT for audio_sender to finish
                    session.turn_done_event.clear()
                    await session.audio_queue.put(None)
                    await session.turn_done_event.wait()
                    
                    # Send completion
                    await websocket.send_json({
                        "type": "complete",
                        "chunks": stats.get("chunks_received", 0),
                        "total_bytes": stats.get("total_audio_bytes", 0),
                        "first_chunk_latency_ms": stats.get("first_chunk_latency_ms"),
                        "total_time_ms": stats.get("total_time_ms"),
                    })
                    
                    if stats.get("error"):
                        await websocket.send_json({
                            "type": "error",
                            "message": stats["error"]
                        })
                
                elif msg_type == "stream_chunk":
                    # Streaming text chunk
                    text_chunk = message.get("text", "")
                    
                    # Store config for stream_end (capture from first chunk)
                    if message.get("voice"):
                        session.current_voice = message.get("voice")
                    if message.get("model"):
                        session.current_model = message.get("model")
                    if message.get("language"):
                        session.current_language = message.get("language")
                    if message.get("pronunciation_dict_id"):
                        session.current_dict = message.get("pronunciation_dict_id")
                        
                    if text_chunk:
                        text_buffer.append(text_chunk)
                        logger.debug(f"[{session_id}] Chunk buffered: '{text_chunk[:30]}...'")
                
                elif msg_type == "stream_end":
                    # End of streaming - synthesize buffered text
                    if text_buffer:
                        full_text = "".join(text_buffer)
                        text_buffer.clear()
                        
                        logger.info(f"[{session_id}] Stream end, synthesizing {len(full_text)} chars")
                        session.is_streaming = True
                        
                        def audio_callback(audio_bytes: bytes, sample_rate: int, metadata: Dict[str, Any]):
                            session.audio_queue.put_nowait((audio_bytes, metadata))
                        
                        # Renew context
                        turn_ctx_id = f"{session.context_id}-{uuid.uuid4().hex[:6]}"
                        
                        stats = await manager.stream_text_to_audio(
                            full_text,
                            audio_callback,
                            context_id=turn_ctx_id,
                            voice_id=session.current_voice,
                            model_id=session.current_model or config.model,
                            language=session.current_language,
                            pronunciation_dict_id=getattr(session, "current_dict", None),
                        )

                        session.is_streaming = False

                        # Signal turn end and WAIT for audio_sender
                        session.turn_done_event.clear()
                        await session.audio_queue.put(None)
                        await session.turn_done_event.wait()

                        # Log and propagate errors from Cartesia
                        if stats.get("error"):
                            logger.error(f"[{session_id}] Cartesia stream error: {stats['error']}")
                            await websocket.send_json({
                                "type": "error",
                                "message": f"TTS synthesis failed: {stats['error']}"
                            })

                        await websocket.send_json({
                            "type": "complete",
                            "chunks": stats.get("chunks_received", 0),
                            "total_bytes": stats.get("total_audio_bytes", 0),
                            "first_chunk_latency_ms": stats.get("first_chunk_latency_ms"),
                        })
                    else:
                        await websocket.send_json({"type": "complete", "chunks": 0})
                
                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                
                else:
                    logger.warning(f"[{session_id}] Unknown message type: {msg_type}")
                
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
            except json.JSONDecodeError as e:
                logger.warning(f"[{session_id}] Invalid JSON: {e}")
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                
    except WebSocketDisconnect:
        logger.info(f"[{session_id}] WebSocket disconnected")
    except Exception as e:
        logger.error(f"[{session_id}] WebSocket error: {e}")
    finally:
        # Cleanup
        session.is_streaming = False
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass

        # Rate limit: Decrement connection count
        ws_rate_limiter.disconnect(client_ip)

        # Remove session
        if session_id in active_sessions:
            del active_sessions[session_id]
        
        duration = time.time() - session.created_at
        logger.info("============================================================")
        logger.info("============================================================")
        logger.info(
            f"🔌 TTS SESSION END | TENANT_ID={tenant_id} | SESSION_ID={session_id} | "
            f"(duration: {duration:.1f}s, chunks: {session.chunks_sent}, "
            f"bytes: {session.total_audio_bytes})"
        )
        logger.info("============================================================")
        logger.info("============================================================")
        logger.info(
            f"🔌 [{session_id}] Session ended "
            f"(duration: {duration:.1f}s, chunks: {session.chunks_sent}, "
            f"bytes: {session.total_audio_bytes})"
        )


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("TTS_CARTESIA_HOST", "0.0.0.0")
    port = int(os.getenv("TTS_CARTESIA_PORT", "8000"))
    
    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        reload=False,
        log_level="info"
    )

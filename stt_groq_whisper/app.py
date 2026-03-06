"""
Groq Whisper STT Microservice FastAPI Application

Provides WebSocket-based speech-to-text streaming using Groq's ultra-fast Whisper API.
"""

import asyncio
import time
import logging
import json
from contextlib import asynccontextmanager
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import GroqWhisperConfig
from session_manager import GroqWhisperSession

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Global state
config: Optional[GroqWhisperConfig] = None
active_sessions: Dict[str, GroqWhisperSession] = {}
app_start_time: float = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler for application startup/shutdown"""
    global config
    
    logger.info("=" * 70)
    logger.info("🚀 Starting Groq Whisper STT Microservice")
    logger.info("=" * 70)
    
    # Load configuration
    try:
        config = GroqWhisperConfig.from_env()
        logger.info("✅ Configuration loaded")
        logger.info(f"   Model: {config.model}")
        logger.info(f"   Chunk duration: {config.chunk_duration_ms}ms")
        logger.info(f"   Overlap: {config.overlap_duration_ms}ms")
        logger.info(f"   Language: {config.language}")
    except Exception as e:
        logger.error(f"❌ Failed to load configuration: {e}")
        raise
    
    logger.info("✅ Groq Whisper STT microservice ready")
    logger.info("=" * 70)
    
    yield
    
    # Shutdown
    logger.info("🛑 Shutting down Groq Whisper STT microservice")
    
    # Close all active sessions
    for session_id, session in list(active_sessions.items()):
        try:
            await session.stop()
        except Exception as e:
            logger.warning(f"⚠️ Error stopping session {session_id}: {e}")
    
    logger.info("✅ Groq Whisper STT microservice stopped")


# Initialize FastAPI app
app = FastAPI(
    title="Groq Whisper STT Streaming Service",
    description="WebSocket-based STT streaming with Groq's ultra-fast Whisper API",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": "Groq Whisper STT Streaming Service",
        "version": "1.0.0",
        "description": "WebSocket-based speech-to-text with Groq's Whisper API",
        "model": config.model if config else None,
        "endpoints": {
            "websocket": "/api/v1/transcribe/stream",
            "health": "/health",
            "metrics": "/metrics"
        }
    }





# Static files
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import os

static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/client", response_class=HTMLResponse)
async def get_client():
    """Serve the client testing interface"""
    client_path = os.path.join(static_dir, "client.html")
    if os.path.exists(client_path):
        with open(client_path, "r") as f:
            return f.read()
    return "client.html not found. Please create it in the static directory."


@app.get("/health")
async def health_check(deep: bool = False):
    """
    Health check endpoint.
    
    Args:
        deep: If true, performs connectivity check to Groq API
    """
    uptime_seconds = time.time() - app_start_time
    
    health_status = {
        "status": "healthy",
        "service": "groq-whisper-stt",
        "uptime_seconds": uptime_seconds,
        "config": {
            "model": config.model if config else None,
            "chunk_duration_ms": config.chunk_duration_ms if config else None,
            "language": config.language if config else None,
        },
        "sessions": {
            "active": len(active_sessions)
        }
    }
    
    if deep and config:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Simple ping to Groq API (will get auth error but confirms connectivity)
                response = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {config.api_key}"}
                )
                health_status["upstream"] = {
                    "groq_api": "reachable" if response.status_code in [200, 401] else "error",
                    "status_code": response.status_code,
                    "timestamp": time.time()
                }
                if response.status_code != 200:
                    health_status["status"] = "degraded"
        except Exception as e:
            health_status["status"] = "unhealthy"
            health_status["upstream"] = {"error": str(e)}
    
    return health_status


@app.get("/metrics")
async def get_metrics():
    """Get real-time metrics and performance statistics"""
    metrics = {
        "sessions": {
            "active": len(active_sessions),
        },
        "uptime_seconds": time.time() - app_start_time,
    }
    
    # Aggregate session stats
    total_chunks = 0
    total_processed = 0
    total_transcripts = 0
    total_latency = 0.0
    latency_count = 0
    
    for session in active_sessions.values():
        stats = session.get_stats()
        total_chunks += stats.get("chunks_received", 0)
        total_processed += stats.get("chunks_processed", 0)
        total_transcripts += stats.get("transcripts_emitted", 0)
        
        if "groq_client" in stats:
            client_stats = stats["groq_client"]
            if client_stats.get("avg_latency_ms", 0) > 0:
                total_latency += client_stats["avg_latency_ms"]
                latency_count += 1
    
    metrics["transcription"] = {
        "total_chunks_received": total_chunks,
        "total_chunks_processed": total_processed,
        "total_transcripts_emitted": total_transcripts,
        "avg_groq_latency_ms": total_latency / latency_count if latency_count > 0 else 0,
    }
    
    # Add config info
    if config:
        metrics["config"] = {
            "model": config.model,
            "chunk_duration_ms": config.chunk_duration_ms,
            "overlap_duration_ms": config.overlap_duration_ms,
            "language": config.language,
        }
    
    return metrics


@app.websocket("/api/v1/transcribe/stream")
async def transcribe_stream(websocket: WebSocket, session_id: str = None):
    """
    WebSocket endpoint for real-time speech-to-text streaming.
    
    Message protocol:
    Client -> Server: Raw PCM audio chunks (16kHz, 16-bit mono) OR JSON messages
    Server -> Client:
        - {"type": "data", "data": {"transcript": "text", "is_final": false, ...}}
        - {"type": "data", "data": {"transcript": "text", "is_final": true, ...}}
        - {"type": "events", "data": {"event_type": "vad_event", "signal_type": "SPEECH_START/END"}}
    """
    if not config:
        await websocket.close(code=1013, reason="Service not initialized")
        return
    
    if not session_id:
        session_id = f"groq_{int(time.time())}_{id(websocket)}"
    tenant_id = os.getenv("TENANT_ID", "tenant")
    
    # Check if session already exists
    if session_id in active_sessions:
        logger.warning(f"⚠️ Session {session_id} already exists - cleaning up")
        try:
            await active_sessions[session_id].stop()
            del active_sessions[session_id]
        except Exception as e:
            logger.warning(f"⚠️ Error cleaning up existing session: {e}")
    
    logger.info("============================================================")
    logger.info("============================================================")
    logger.info(f"🔌 STT SESSION START | TENANT_ID={tenant_id} | SESSION_ID={session_id}")
    logger.info("============================================================")
    logger.info("============================================================")
    
    await websocket.accept()
    
    # Track connection state
    connection_open = True
    audio_chunks_received = 0
    
    # Send connection confirmation
    try:
        await websocket.send_json({
            "type": "connected",
            "session_id": session_id,
            "timestamp": time.time(),
            "message": "Groq Whisper STT streaming ready",
            "config": {
                "model": config.model,
                "chunk_duration_ms": config.chunk_duration_ms
            }
        })
    except WebSocketDisconnect:
        logger.info(f"🔌 WebSocket disconnected during handshake: {session_id}")
        return
    except Exception as e:
        logger.error(f"❌ Error sending handshake: {e}")
        return
    
    # Create transcript callback
    async def transcript_callback(message: dict):
        """Callback to send transcripts back to WebSocket client"""
        nonlocal connection_open
        if not connection_open:
            return
        
        try:
            await websocket.send_json(message)
        except WebSocketDisconnect:
            logger.info(f"[{session_id}] WebSocket client disconnected during callback")
            connection_open = False
        except Exception as e:
            # Only log if connection is supposed to be open
            if connection_open:
                logger.error(f"❌ [{session_id}] WebSocket send error: {e}")
                connection_open = False
    
    # Create session
    session = GroqWhisperSession(
        session_id,
        config,
        transcript_callback,
        orchestrator_url=config.orchestrator_ws_url
    )
    
    # Start session
    started = await session.start()
    if not started:
        try:
            await websocket.send_json({
                "type": "error",
                "message": "Failed to initialize Groq Whisper session. Check API key."
            })
            await websocket.close()
        except Exception:
            pass
        logger.error(f"❌ Session {session_id} failed to start")
        return
    
    active_sessions[session_id] = session
    logger.info(f"📝 Session {session_id} registered and ready")
    
    try:
        # Audio processing loop
        while connection_open:
            try:
                message = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=30.0
                )
                
                if message["type"] == "websocket.receive":
                    if "bytes" in message and message["bytes"]:
                        audio_data = message["bytes"]
                        audio_chunks_received += 1
                        
                        if len(audio_data) < 100:
                            continue
                        
                        # Log every 50th chunk
                        if audio_chunks_received % 50 == 1:
                            logger.info(f"📥 [{session_id}] Received audio chunk #{audio_chunks_received} ({len(audio_data)} bytes)")
                        
                        # Process audio through session
                        await session.process_audio_chunk(audio_data)
                        
                    elif "text" in message and message["text"]:
                        try:
                            data = json.loads(message["text"])
                            msg_type = data.get("type", "")
                            
                            if msg_type == "ping":
                                await websocket.send_json({
                                    "type": "pong",
                                    "timestamp": time.time()
                                })
                            else:
                                logger.debug(f"📩 [{session_id}] Received JSON: {msg_type}")
                        except json.JSONDecodeError:
                            logger.warning(f"⚠️ [{session_id}] Invalid JSON received")
                
                elif message["type"] == "websocket.disconnect":
                    logger.info(f"🔌 WebSocket disconnected: {session_id}")
                    connection_open = False
                    break
                    
            except asyncio.TimeoutError:
                if connection_open:
                    try:
                        await websocket.send_json({
                            "type": "ping",
                            "timestamp": time.time()
                        })
                    except Exception:
                        connection_open = False
                        break
                        
            except WebSocketDisconnect:
                logger.info(f"🔌 WebSocket disconnected: {session_id}")
                connection_open = False
                break
                
            except Exception as e:
                error_str = str(e).lower()
                if "disconnect" in error_str or "closed" in error_str:
                    connection_open = False
                    break
                else:
                    logger.error(f"❌ Error processing message: {e}", exc_info=True)
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Processing error: {str(e)}"
                        })
                    except Exception:
                        connection_open = False
                        break
                        
    except Exception as e:
        logger.error(f"❌ Session error for {session_id}: {e}")
        
    finally:
        connection_open = False
        
        logger.info(f"📊 [{session_id}] Session summary: {audio_chunks_received} audio chunks received")
        
        # Cleanup session only if it's still ours
        if active_sessions.get(session_id) is session:
            try:
                await session.stop()
                del active_sessions[session_id]
            except Exception as e:
                logger.warning(f"⚠️ Error stopping session: {e}")
        else:
            logger.debug(f"[{session_id}] Session was already replaced or removed, skipping cleanup")
        
        # Close WebSocket
        try:
            await websocket.close()
        except Exception:
            pass
        
        logger.info("============================================================")
        logger.info("============================================================")
        logger.info(f"🔌 STT SESSION END | TENANT_ID={tenant_id} | SESSION_ID={session_id}")
        logger.info("============================================================")
        logger.info("============================================================")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)

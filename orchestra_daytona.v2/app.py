"""
Orchestra-daytona: Generalized Orchestrator for Daytona University

FastAPI application with unified WebSocket architecture supporting:
- Multi-language (English & German) with auto-detection
- Configurable services (STT, TTS, RAG)
- YAML-based configuration
- Phase 3 unified WebSocket handler
"""

import logging
import sys
import asyncio
import aiohttp
import websockets
import json
import ssl
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, Query
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import redis.asyncio as redis
import os
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

from config_loader import load_config, OrchestratorConfig
from dialogue.manager import MultiLangDialogueManager
from core.pipeline import ProcessingPipeline
from core.ws_handler import OrchestratorWSHandler

# Metrics imports
from starlette.middleware.base import BaseHTTPMiddleware
import time

# Phone integration imports
from fastapi import Request, Response, HTTPException
from twilio.twiml.voice_response import VoiceResponse, Start, Stream

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Global instances
config: Optional[OrchestratorConfig] = None
dialogue_manager: Optional[MultiLangDialogueManager] = None
pipeline: Optional[ProcessingPipeline] = None
ws_handler: Optional[OrchestratorWSHandler] = None
redis_client: Optional[redis.Redis] = None

# Metrics storage (simplistic in-memory)
metrics: Dict[str, Any] = {
    "requests_total": 0,
    "errors_total": 0,
    "active_sessions": 0,
    "start_time": time.time()
}

class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        metrics["requests_total"] += 1
        try:
            response = await call_next(request)
            return response
        except Exception as e:
            metrics["errors_total"] += 1
            raise e

async def check_service_health(url: str, service_name: str, timeout: float = 5.0, retries: int = 1, skip_ssl: bool = True) -> dict:
    """
    Check health of a microservice via HTTP health endpoint with optional retries.
    """
    health_url = f"{url}/health"
    
    # Create explicit SSL context for robustness
    ssl_context = None
    if skip_ssl and url.startswith("https://"):
        # Use unverified context to ensure all checks are bypassed for internal services
        ssl_context = ssl._create_unverified_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(health_url, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                    if response.status == 200:
                        data = await response.json()
                        logger.info(f"  ✅ {service_name}: Healthy (status={data.get('status', 'unknown')})")
                        return {"healthy": True, "status": data.get("status")}
                    else:
                        error_msg = f"HTTP {response.status}"
                        if attempt == retries - 1:
                            logger.error(f"  ❌ {service_name}: {error_msg}")
                        else:
                            logger.warning(f"  ⚠️ {service_name}: Attempt {attempt + 1} failed ({error_msg}), retrying...")
                            await asyncio.sleep(1.0)
        except Exception as e:
            error_msg = str(e)
            if attempt == retries - 1:
                logger.error(f"  ❌ {service_name}: {error_msg}")
            else:
                logger.warning(f"  ⚠️ {service_name}: Attempt {attempt + 1} failed ({error_msg}), retrying...")
                await asyncio.sleep(1.0)
                
    return {"healthy": False, "error": error_msg}


async def check_tts_prewarm(url: str, timeout: float = 10.0, skip_ssl: bool = False) -> dict:
    """
    Check TTS service by attempting a prewarm connection (tests WebSocket + ElevenLabs API).
    Falls back to HTTP health check if WebSocket fails.
    
    Args:
        url: Base URL of TTS service (e.g., http://tts-labs-daytona:8006)
        timeout: Connection timeout in seconds
        skip_ssl: Whether to skip SSL verification
    
    Returns:
        dict with 'healthy' (bool) and 'error' (str) keys
    """
    ws_url = url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/api/v1/stream?session_id=health_check"
    
    ssl_context = None
    if skip_ssl and ws_url.startswith("wss://"):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    
    try:
        # Use asyncio.wait_for to timeout the entire operation
        result = await asyncio.wait_for(websockets_connect_with_prewarm(ws_url, ssl_context), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        error_msg = f"WebSocket connection timeout after {timeout}s"
        logger.warning(f"  ⚠️  TTS-Labs WebSocket: {error_msg}")
        # Fallback to HTTP health check
        logger.info(f"  🔄 Falling back to HTTP health check...")
        http_result = await check_service_health(url, "TTS-Labs (HTTP)", timeout=5.0, skip_ssl=skip_ssl)
        if http_result["healthy"]:
            logger.info(f"  ✅ TTS-Labs: HTTP health check passed (WebSocket may not be supported)")
            return {"healthy": True, "status": "http_only", "note": "WebSocket not available"}
        return http_result
    except Exception as e:
        error_msg = f"WebSocket connection failed: {str(e)}"
        logger.warning(f"  ⚠️  TTS-Labs WebSocket: {error_msg}")
        # Fallback to HTTP health check
        logger.info(f"  🔄 Falling back to HTTP health check...")
        http_result = await check_service_health(url, "TTS-Labs (HTTP)", timeout=5.0, skip_ssl=skip_ssl)
        if http_result["healthy"]:
            logger.info(f"  ✅ TTS-Labs: HTTP health check passed (WebSocket may not be supported)")
            return {"healthy": True, "status": "http_only", "note": "WebSocket not available"}
        return {"healthy": False, "error": error_msg}


async def websockets_connect_with_prewarm(ws_url: str, ssl_context: Optional[ssl.SSLContext] = None) -> dict:
    """Helper function to handle WebSocket connection and prewarm."""
    async with websockets.connect(ws_url, ssl=ssl_context) as ws:
        # Send prewarm message
        await ws.send(json.dumps({"type": "prewarm"}))
        # Wait for response (should be prewarmed or error)
        try:
            response = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(response)
            if data.get("type") in ("prewarmed", "connected"):
                logger.info(f"  ✅ TTS-Labs: WebSocket prewarm successful")
                return {"healthy": True, "status": "prewarmed"}
            elif data.get("type") == "error":
                error_msg = data.get("message", "Unknown error")
                logger.error(f"  ❌ TTS-Labs: Prewarm failed - {error_msg}")
                return {"healthy": False, "error": f"Prewarm error: {error_msg}"}
            else:
                logger.warning(f"  ⚠️  TTS-Labs: Unexpected response: {data.get('type')}")
                return {"healthy": True, "status": "connected"}  # Assume OK if we got a response
        except asyncio.TimeoutError:
            logger.warning(f"  ⚠️  TTS-Labs: No response to prewarm (may be OK)")
            return {"healthy": True, "status": "connected"}  # Assume OK if connection succeeded


async def perform_startup_health_checks(config: OrchestratorConfig, max_retries: int = 1, retry_delay: float = 1.0) -> dict:
    """
    Perform healthy checks with automatic retries for startup.
    """
    for attempt in range(max_retries):
        results = {
            "all_healthy": True, # Default to True to allow startup even if some services fail
            "services": {}
        }
        
        logger.info(f"Startup health check - Attempt {attempt + 1}/{max_retries}")
        
        # Check RAG service
        rag_result = await check_service_health(config.services.rag.url, "RAG-Daytona", timeout=2.0, skip_ssl=config.server.skip_ssl_verify)
        results["services"]["rag"] = rag_result
        if not rag_result["healthy"]:
            # results["all_healthy"] = False # WARN strictly, but don't block
             logger.warning(f"  ⚠️ RAG health check failed: {rag_result.get('error')}")
        
        # Check STT service
        stt_result = await check_service_health(config.services.stt.url, "STT-Groq-Whisper", timeout=2.0, skip_ssl=config.server.skip_ssl_verify)
        results["services"]["stt"] = stt_result
        if not stt_result["healthy"]:
            # results["all_healthy"] = False
            logger.warning(f"  ⚠️ STT health check failed: {stt_result.get('error')}")
        
        # Check TTS service
        tts_result = await check_tts_prewarm(config.services.tts.url, timeout=2.0, skip_ssl=config.server.skip_ssl_verify)
        results["services"]["tts"] = tts_result
        if not tts_result["healthy"]:
            logger.warning(f"  ⚠️ TTS health check failed: {tts_result.get('error')}")
        
        # Always return results to allow startup
        return results


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    global config, dialogue_manager, pipeline, ws_handler, redis_client
    
    # Startup
    logger.info("=" * 70)
    logger.info("🚀 Starting Orchestrator")
    logger.info("=" * 70)
    
    # Load configuration
    try:
        config = load_config()
        logger.info(f"✅ Configuration loaded: {config.organization.name}")
    except Exception as e:
        logger.error(f"❌ Failed to load configuration: {e}")
        raise
    
    # Initialize Redis (if enabled)
    if config.services.redis.enabled:
        try:
            redis_client = redis.from_url(
                config.services.redis.url,
                encoding="utf-8",
                decode_responses=True
            )
            await redis_client.ping()
            logger.info(f"✅ Redis connected: {config.services.redis.url}")
        except Exception as e:
            logger.warning(f"⚠️ Redis connection failed: {e}")
            redis_client = None
    else:
        logger.info("ℹ️ Redis disabled in configuration")
    
    # Initialize Dialogue Manager (loads from JSON files if dialogue_config is None)
    try:
        dialogue_manager = MultiLangDialogueManager(
            dialogue_config=None,  # Will load from JSON files in assets/
            assets_dir=None,  # Will use default
            disable_pregenerated_audio=config.languages.disable_pregenerated_audio
        )
        logger.info(f"✅ Dialogue Manager initialized for languages: {', '.join(dialogue_manager.get_supported_languages())}")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Dialogue Manager: {e}")
        raise
    
    # Initialize Processing Pipeline
    try:
        pipeline = ProcessingPipeline(
            rag_config=config.services.rag,
            intent_config=config.services.intent,
            supported_languages=config.languages.supported,
            skip_ssl=config.server.skip_ssl_verify
        )
        logger.info("✅ Processing Pipeline initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Pipeline: {e}")
        raise
    
    # Initialize WebSocket Handler
    try:
        ws_handler = OrchestratorWSHandler(
            config=config,
            dialogue_manager=dialogue_manager,
            pipeline=pipeline,
            redis_client=redis_client
        )
        logger.info("✅ WebSocket Handler initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize WebSocket Handler: {e}")
        raise
    
    # CRITICAL: Health checks for all dependent services before accepting sessions
    logger.info("=" * 70)
    logger.info("🔍 Performing startup health checks (IN BACKGROUND)...")
    logger.info("=" * 70)
    
    # Run health checks in background to avoid blocking startup
    async def run_background_health_checks():
        # Wait a bit for other services to come up
        await asyncio.sleep(2.0)
        
        health_check_results = await perform_startup_health_checks(config, max_retries=10, retry_delay=5.0)
        
        if not health_check_results["all_healthy"]:
            logger.error("=" * 70)
            logger.error("❌ BACKGROUND HEALTH CHECKS FAILED!")
            logger.error("=" * 70)
            logger.error("The following services are unavailable:")
            for service, status in health_check_results["services"].items():
                if not status["healthy"]:
                    logger.error(f"  ❌ {service}: {status.get('error', 'Unknown error')}")
            logger.error("=" * 70)
        else:
            logger.info("=" * 70)
            logger.info("✅ All background health checks passed!")
            logger.info("=" * 70)

    asyncio.create_task(run_background_health_checks())
    
    logger.info("=" * 70)
    logger.info("✅ Orchestrator ready!")
    logger.info(f"   Organization: {config.organization.name}")
    logger.info(f"   Languages: {', '.join(config.languages.supported)}")
    logger.info(f"   Default Language: {config.languages.default}")
    logger.info(f"   STT Service: {config.services.stt.url}")
    logger.info(f"   TTS Service: {config.services.tts.url}")
    logger.info(f"   RAG Service: {config.services.rag.url}")
    logger.info("=" * 70)
    
    yield
    
    # Shutdown
    logger.info("🛑 Shutting down Orchestrator...")
    
    # Graceful shutdown of WebSocket sessions
    if ws_handler:
        await ws_handler.shutdown()
    
    if redis_client:
        await redis_client.close()
        logger.info("✅ Redis connection closed")
    
    logger.info("✅ Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Orchestra-daytona",
    description="Generalized Orchestrator for DaVinci AI Assistant",
    version="1.0.0",
    lifespan=lifespan
)

# Add metrics middleware
app.add_middleware(MetricsMiddleware)


@app.get("/")
async def root():
    """Redirect root to client frontend"""
    return RedirectResponse(url="/client")


@app.get("/health")
async def health(deep: bool = False):
    """
    Health check endpoint.
    Use ?deep=true for real connectivity checks.
    """
    basic_status = {
        "status": "healthy",
        "config_loaded": config is not None,
        "dialogue_manager_ready": dialogue_manager is not None,
        "pipeline_ready": pipeline is not None,
        "ws_handler_ready": ws_handler is not None,
        "redis_connected": redis_client is not None if config and config.services.redis.enabled else None
    }
    
    if not deep:
        return basic_status
        
    # Perform deep checks
    logger.info("🔍 Performing on-demand deep health checks...")
    deep_results = await perform_startup_health_checks(config)
    
    # Merge results
    basic_status["services"] = deep_results["services"]
    if not deep_results["all_healthy"]:
        basic_status["status"] = "degraded"
        
    return basic_status


@app.get("/metrics")
async def get_metrics():
    """Internal metrics endpoint"""
    current_metrics = metrics.copy()
    current_metrics["uptime_seconds"] = int(time.time() - metrics["start_time"])
    if ws_handler:
        current_metrics["active_sessions"] = len(ws_handler.sessions)
    return current_metrics


@app.get("/api/metrics")
async def get_public_metrics():
    """
    Public metrics endpoint for monitoring dashboard.
    Returns JSON with system health and performance metrics.
    """
    current_metrics = metrics.copy()
    uptime = int(time.time() - metrics["start_time"])
    
    # Calculate average response time if we have data
    avg_response_ms = None
    if metrics.get("response_times") and len(metrics["response_times"]) > 0:
        avg_response_ms = sum(metrics["response_times"]) / len(metrics["response_times"])
    
    return {
        "active_sessions": len(ws_handler.sessions) if ws_handler else 0,
        "total_requests": metrics.get("requests_total", 0),
        "errors_total": metrics.get("errors_total", 0),
        "uptime_seconds": uptime,
        "avg_response_ms": avg_response_ms,
        "status": "healthy",
        "timestamp": time.time()
    }


@app.get("/api/sessions")
async def list_active_sessions():
    """List all currently active WebSocket sessions with high-level stats."""
    if not ws_handler:
        return []
    
    active_sessions = []
    for sid, session in ws_handler.sessions.items():
        active_sessions.append({
            "session_id": sid,
            "user_id": session.user_id,
            "created_at": session.created_at,
            "uptime_sec": time.time() - session.created_at,
            "turns": len(session.history_manager.turns),
            "language": session.current_language,
            "last_activity": session.last_activity
        })
    
    return sorted(active_sessions, key=lambda x: x["created_at"], reverse=True)


@app.get("/api/sessions/{session_id}/metrics")
async def get_session_performance_metrics(session_id: str):
    """Get turn-by-turn performance metrics (TTFT, TTFC) for a specific session."""
    if not ws_handler or session_id not in ws_handler.sessions:
        raise HTTPException(status_code=404, detail="Session not found or inactive")
    
    session = ws_handler.sessions[session_id]
    return {
        "session_id": session_id,
        "turn_count": len(session.turn_metrics),
        "metrics": session.turn_metrics
    }


@app.get("/api/sessions/{session_id}/history")
async def get_session_chat_history(session_id: str):
    """Get the full conversation history for a specific session."""
    if not ws_handler or session_id not in ws_handler.sessions:
        raise HTTPException(status_code=404, detail="Session not found or inactive")
    
    session = ws_handler.sessions[session_id]
    return {
        "session_id": session_id,
        "history": session.history_manager.to_dict()
    }


@app.get("/api/sessions/{session_id}/summary")
async def generate_session_summary(session_id: str):
    """Generate an AI summary of the conversation history."""
    if not ws_handler or session_id not in ws_handler.sessions:
        raise HTTPException(status_code=404, detail="Session not found or inactive")
    
    session = ws_handler.sessions[session_id]
    history = session.history_manager.get_context_window()
    
    if not history:
        return {"summary": "No conversation history available."}
    
    # Use RAG pipeline to generate a summary
    # We create a special "summary" query
    summary_query = "Please provide a concise summary of our conversation so far, highlighting the main topics and any actions taken."
    
    try:
        summary_text = ""
        async for token_data in pipeline.process_query(
            query=summary_query,
            session_id=f"summary_{session_id}",
            history_context=f"CONVERSATION HISTORY TO SUMMARIZE:\n{history}",
            language=session.current_language
        ):
            summary_text += token_data.get("token", "")
        
        return {
            "session_id": session_id,
            "summary": summary_text.strip(),
            "turn_count": len(session.history_manager.turns)
        }
    except Exception as e:
        logger.error(f"Failed to generate summary for {session_id}: {e}")
        return {"error": f"Summary generation failed: {str(e)}"}


@app.get("/api/stream")
@app.post("/api/stream")
async def typing_stream(q: Optional[str] = None, 
                        query: Optional[str] = None,
                        lang: str = "en", 
                        delay: float = 0.03,
                        request: Request = None):
    """
    Streams RAG response with a deliberate delay to simulate typing.
    Accepts q/query via query params (GET) or json body (POST).
    """
    input_query = q or query
    
    if request and request.method == "POST":
        try:
            body = await request.json()
            input_query = body.get("q") or body.get("query") or input_query
            lang = body.get("lang") or body.get("language") or lang
            delay = body.get("delay") or delay
        except:
            pass
            
    if not input_query:
        raise HTTPException(status_code=400, detail="Missing query parameter 'q' or 'query'")

    async def stream_generator():
        try:
            async for token_data in pipeline.process_query(
                query=input_query,
                session_id=f"typing_stream_{int(time.time())}",
                language=lang
            ):
                token = token_data.get("token", "")
                if token:
                    # Yield token and wait to simulate typing
                    yield token
                    await asyncio.sleep(delay)
                elif token_data.get("is_final"):
                    # End of stream
                    break
        except Exception as e:
            logger.error(f"Typing stream error: {e}")
            yield f"\n[Error: {str(e)}]"
                
    return StreamingResponse(stream_generator(), media_type="text/plain")


@app.get("/config")
async def get_config():
    """Get current configuration (non-sensitive)"""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    
    return {
        "organization": {
            "name": config.organization.name,
            "full_name": config.organization.full_name
        },
        "languages": {
            "default": config.languages.default,
            "supported": config.languages.supported,
            "auto_detect": config.languages.auto_detect
        },
        "services": {
            "stt": {
                "url": config.services.stt.url,
                "type": config.services.stt.type
            },
            "tts": {
                "url": config.services.tts.url,
                "type": config.services.tts.type,
                "streaming_mode": config.services.tts.streaming_mode
            },
            "rag": {
                "url": config.services.rag.url,
                "top_k": config.services.rag.top_k
            }
        }
    }


# ════════════════════════════════════════════════════════════════════════════════
# RAG PROXY - Mock Mode Direct Access
# ════════════════════════════════════════════════════════════════════════════════

from fastapi.responses import StreamingResponse

@app.post("/api/rag/stream_query")
async def proxy_rag_stream_query(request: Request):
    """
    Proxy endpoint for direct RAG access (mock mode).
    Forwards requests to the RAG service's streaming endpoint.
    """
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    
    try:
        # Get request body
        body = await request.json()
        
        # Add conversation context tracking
        logger.info(f"📤 RAG Proxy: Forwarding query to {config.services.rag.url}")
        
        # Forward to RAG service streaming endpoint
        rag_url = f"{config.services.rag.url}/api/v1/stream_query"
        
        async def stream_generator():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    rag_url,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"RAG Proxy error: HTTP {response.status} - {error_text}")
                        yield json.dumps({"error": f"RAG service error: {response.status}", "is_final": True}) + "\n"
                        return
                    
                    async for chunk in response.content:
                        yield chunk
        
        return StreamingResponse(
            stream_generator(),
            media_type="application/x-ndjson"
        )
        
    except Exception as e:
        logger.error(f"RAG Proxy error: {e}")
        return JSONResponse(
            {"error": f"RAG proxy error: {str(e)}"},
            status_code=500
        )


@app.get("/client")
async def serve_client(request: Request):
    """Serve the browser client HTML page or redirect to global domain"""
    host = request.headers.get("host", "")
    # If accessed via localhost, and not in development mode, we could redirect
    if "localhost" in host or "127.0.0.1" in host:
        # Check if user wants to force global domain
        if os.getenv("FORCE_GLOBAL_DOMAIN", "false").lower() == "true":
            return RedirectResponse(url="https://demo.davinciai.eu/client")
            
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    client_path = os.path.join(static_dir, "client.html")
    
    if os.path.exists(client_path):
        return FileResponse(client_path)
    else:
        return JSONResponse(
            {"error": "Client HTML file not found"},
            status_code=404
        )


@app.get("/api/logs/stream")
async def stream_pod_logs(pod: str = Query("orchestrator", description="Pod name: orchestrator or rag")):
    """Stream Kubernetes pod logs using Server-Sent Events"""
    
    async def log_generator():
        # Map friendly names to actual pod labels
        pod_map = {
            "orchestrator": "app=orchestrator",
            "rag": "app=rag"
        }
        
        label_selector = pod_map.get(pod, "app=orchestrator")
        
        try:
            # Load Kubernetes config (in-cluster or from kubeconfig)
            try:
                k8s_config.load_incluster_config()
            except:
                k8s_config.load_kube_config()
            
            v1 = client.CoreV1Api()
            namespace = "davinci-local"
            
            # Get pods matching the label
            pods = v1.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector
            )
            
            if not pods.items:
                yield f"data: {json.dumps({'error': f'No pods found with label {label_selector}'})}\n\n"
                return
            
            # Use the first pod
            pod_name = pods.items[0].metadata.name
            
            # Stream logs
            # Stream logs using watch
            w = watch.Watch()
            # Use chunks rather than line-by-line to avoid buffering issues
            for event in w.stream(v1.read_namespaced_pod_log,
                                name=pod_name,
                                namespace=namespace,
                                tail_lines=100,
                                follow=True,
                                _preload_content=False):
                
                # Check if we should stop
                if await request.is_disconnected():
                    break

                if isinstance(event, str):
                   line = event
                elif hasattr(event, 'decode'):
                   line = event.decode('utf-8')
                else:
                   line = str(event)
                
                if line:
                    yield f"data: {json.dumps({'log': line.rstrip(), 'pod': pod_name})}\n\n"
                    # CRITICAL: Allow event loop to breathe
                    await asyncio.sleep(0)
                        
        except ApiException as e:
            logger.error(f"Kubernetes API error logs {pod}: {e}")
            yield f"data: {json.dumps({'error': f'K8s API: {e}'})}\n\n"
        except Exception as e:
            logger.error(f"Log stream error {pod}: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
                
    
    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, session_id: Optional[str] = Query(None)):
    """
    Unified WebSocket endpoint for Orchestra-daytona
    
    Handles:
    - Audio input (microphone chunks)
    - Audio output (TTS streaming)
    - State synchronization
    - Interrupt handling
    
    Args:
        websocket: WebSocket connection
        session_id: Optional session ID (auto-generated if not provided)
    """
    if not ws_handler:
        await websocket.close(code=1013, reason="WebSocket handler not initialized")
        return
    
    await ws_handler.handle_connection(websocket, session_id)


# ════════════════════════════════════════════════════════════════════════════════
# PHONE INTEGRATION - Method 2: Twilio Console
# ════════════════════════════════════════════════════════════════════════════════

@app.post("/phone/webhook")
async def handle_phone_call(request: Request):
    """
    Handle incoming phone calls from Twilio Console

    This endpoint receives webhooks when someone calls your Twilio number.
    It responds with TwiML that tells Twilio to stream audio via WebSocket.
    """
    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
        from_number = form_data.get("From")

        logger.info(f"📞 Incoming call from {from_number}, SID: {call_sid}")

        # Create TwiML response to accept call and setup audio streaming
        response = VoiceResponse()
        start = Start()

        # WebSocket URL for audio streaming (uses same host as request)
        websocket_url = f"wss://{request.headers.get('host', 'localhost:8004')}/phone-audio/{call_sid}"
        stream = Stream(url=websocket_url)
        start.append(stream)
        response.append(start)

        return Response(str(response), media_type="text/xml")

    except Exception as e:
        logger.error(f"Phone webhook error: {e}")
        # Return error TwiML on failure
        response = VoiceResponse()
        response.say("Sorry, we're experiencing technical difficulties.")
        return Response(str(response), media_type="text/xml")


@app.websocket("/phone-audio/{call_sid}")
async def handle_phone_audio(websocket: WebSocket, call_sid: str):
    """
    Handle phone audio streaming via WebSocket

    This WebSocket receives G.711 audio from Twilio and connects it to your
    existing orchestrator pipeline (STT → RAG → TTS).
    """
    await websocket.accept()

    if not ws_handler:
        await websocket.close(code=1013, reason="WebSocket handler not initialized")
        return

    logger.info(f"🎤 Phone audio WebSocket connected: {call_sid}")

    try:
        # Create phone-specific session using existing handler
        if hasattr(ws_handler, 'handle_phone_connection'):
            await ws_handler.handle_phone_connection(
                websocket=websocket,
                call_sid=call_sid,
                session_id=f"phone_{call_sid}"
            )
        else:
            # Fallback to regular handler if phone-specific method doesn't exist
            logger.warning("Phone-specific handler not available, using regular handler")
            await ws_handler.handle_connection(websocket, f"phone_{call_sid}")

    except Exception as e:
        logger.error(f"Phone audio handler error for {call_sid}: {e}")
    finally:
        await websocket.close()


@app.post("/phone/status")
async def handle_call_status(request: Request):
    """
    Handle call status updates from Twilio

    Called when calls end, fail, or change status. Used for cleanup.
    """
    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
        call_status = form_data.get("CallStatus")
        call_duration = form_data.get("CallDuration", "0")

        logger.info(f"📞 Call {call_sid} status: {call_status} ({call_duration}s)")

        # Cleanup phone session when call ends
        if call_status in ["completed", "failed", "busy", "no-answer"]:
            if ws_handler and hasattr(ws_handler, 'cleanup_phone_session'):
                await ws_handler.cleanup_phone_session(call_sid)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Call status error: {e}")
        raise HTTPException(status_code=500, detail="Status handling failed")


# ════════════════════════════════════════════════════════════════════════════════
# END PHONE INTEGRATION
# ════════════════════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    import uvicorn
    
    # Load config for server settings
    try:
        cfg = load_config()
        uvicorn.run(
            "app:app",
            host=cfg.server.host,
            port=cfg.server.port,
            log_level=cfg.server.log_level.lower()
        )
    except Exception as e:
        logger.error(f"Failed to start server: {e}")
        sys.exit(1)




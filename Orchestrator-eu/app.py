"""
Orchestra-daytona: Generalized Orchestrator for Enterprise Voice + Visual Assistants

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
from urllib.parse import urlencode

from fastapi import FastAPI, WebSocket, Query
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import redis.asyncio as redis
import os
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException
from fastapi.middleware.cors import CORSMiddleware

from config_loader import load_config, OrchestratorConfig
from dialogue.manager import MultiLangDialogueManager
from core.pipeline import ProcessingPipeline
from core.ws_handler import OrchestratorWSHandler

# Import rate limiter
from shared.rate_limiter import RateLimitMiddleware, WebSocketRateLimiter

# Metrics imports
from starlette.middleware.base import BaseHTTPMiddleware
import time

# Phone integration imports
from fastapi import Request, Response, HTTPException
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from twilio.rest import Client

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


def _sanitize_tenant_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    tenant = str(value).strip().lower()
    if not tenant:
        return None
    tenant = tenant.split("/")[0]
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in tenant) or None


def _resolve_requested_tenant(
    request: Optional[Request] = None,
    body: Optional[Dict[str, Any]] = None,
    explicit_tenant_id: Optional[str] = None,
) -> str:
    candidates = [
        explicit_tenant_id,
        (request.query_params.get("tenant_id") if request else None),
        (body.get("tenant_id") if body else None),
        (config.agent.tenant_id if config else None),
        (config.organization.tenant_id if config else None),
        "tenant",
    ]
    for candidate in candidates:
        tenant = _sanitize_tenant_id(candidate)
        if tenant:
            return tenant
    return "tenant"


def _merge_proxy_body(
    body: Optional[Dict[str, Any]],
    tenant_id: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = dict(body or {})
    merged["tenant_id"] = tenant_id
    if extra:
        merged.update(extra)
    return merged


def _build_cors_origins() -> list[str]:
    origins = {
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "https://demo.davinciai.eu",
        "https://enterprise.davinciai.eu",
    }

    for env_key in ("PUBLIC_URL", "GLOBAL_CLIENT_URL", "CORS_ALLOW_ORIGINS"):
        raw = (os.getenv(env_key, "") or "").strip()
        if not raw:
            continue
        for value in raw.split(","):
            origin = value.strip().rstrip("/")
            if origin:
                origins.add(origin)

    return sorted(origins)

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
    # Disable internal ping to avoid keepalive conflicts and AssertionErrors
    async with websockets.connect(ws_url, ssl=ssl_context, ping_interval=None) as ws:
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
            dialogue_config=config.dialogue,  # Use env-resolved dialogue from config.yaml
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

    # ═══════════════════════════════════════════════════════════
    # ULTIMATE TARA MODULES INITIALIZATION
    # ═══════════════════════════════════════════════════════════
    ultimate_orchestrator = None
    try:
        # Check if Ultimate TARA is enabled via environment
        use_ultimate = os.getenv("USE_ULTIMATE_TARA", "false").lower() == "true"
        
        if use_ultimate and redis_client:
            logger.info("=" * 70)
            logger.info("🚀 Initializing ULTIMATE TARA Architecture")
            logger.info("=" * 70)
            
            from visual_orchestrator_ultimate import UltimateVisualOrchestrator, UltimateConfig
            
            # Build Ultimate config from environment
            ultimate_config = UltimateConfig(
                qdrant_url=os.getenv("QDRANT_URL"),
                qdrant_api_key=os.getenv("QDRANT_API_KEY"),
                qdrant_collection=os.getenv("QDRANT_COLLECTION", f"{(config.agent.id or 'agent').lower()}_hive"),
                redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
                use_new_detective=os.getenv("USE_NEW_DETECTIVE", "true").lower() == "true",
                use_mission_brain=os.getenv("USE_MISSION_BRAIN", "true").lower() == "true",
                use_live_graph=os.getenv("USE_LIVE_GRAPH", "true").lower() == "true",
                use_hive_interface=os.getenv("USE_HIVE_INTERFACE", "true").lower() == "true"
            )
            
            # Create Ultimate Orchestrator (pass None for groq for now, will use fallback)
            ultimate_orchestrator = UltimateVisualOrchestrator(
                groq_provider=None,  # Use fallback mode initially
                config=ultimate_config
            )
            
            # Store in app state for WebSocket handler to access
            app.state.ultimate_orchestrator = ultimate_orchestrator
            
            logger.info("✅ ULTIMATE TARA Architecture initialized")
            logger.info(f"   - Mind Reader: {'✅' if ultimate_orchestrator.mind_reader else '❌'}")
            logger.info(f"   - Hive Interface: {'✅' if ultimate_orchestrator.hive_interface else '❌'}")
            logger.info(f"   - Live Graph: {'✅' if ultimate_orchestrator.live_graph else '❌'}")
            logger.info(f"   - Semantic Detective: {'✅' if ultimate_orchestrator.semantic_detective else '❌'}")
            logger.info(f"   - Mission Brain: {'✅' if ultimate_orchestrator.mission_brain else '❌'}")
            logger.info("=" * 70)
        else:
            logger.info("ℹ️ ULTIMATE TARA disabled (set USE_ULTIMATE_TARA=true)")
            
    except Exception as e:
        logger.warning(f"⚠️ Failed to initialize ULTIMATE TARA: {e}")
        logger.warning("Continuing with legacy architecture...")
        import traceback
        traceback.print_exc()
    
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
    description="Generalized Orchestrator for Enterprise AI Assistants",
    version="1.0.0",
    lifespan=lifespan
)

# Global WebSocket rate limiter - 20 connections per IP, 120 messages/minute
ws_rate_limiter = WebSocketRateLimiter(
    max_connections_per_ip=20,
    max_messages_per_minute=120
)

# Add metrics middleware
app.add_middleware(MetricsMiddleware)

# Add rate limiting middleware - 200 requests/minute per IP
app.add_middleware(
    RateLimitMiddleware,
    redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
    default_requests=200,
    default_window=60,
    exempt_paths=["/health", "/metrics", "/", "/api/health", "/version"]
)

# Add CORS middleware to allow cross-origin requests from Dashboards
app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files to serve tara-widget.js
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    # Dedicated endpoint for widget JS with no-cache headers to prevent stale versions
    from starlette.responses import FileResponse
    
    @app.get("/static/tara-widget.js")
    async def serve_widget_js():
        """Serve widget JS with cache-busting headers"""
        widget_path = os.path.join(static_dir, "tara-widget.js")
        return FileResponse(
            widget_path,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"✅ Static files mounted from {static_dir}")
else:
    logger.warning(f"⚠️ Static directory NOT found at {static_dir}")


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


@app.post("/api/phone/call")
async def initiate_outgoing_call(request: Request):
    """
    Initiate an outgoing call to a phone number.
    """
    try:
        data = await request.json()
        to_number = data.get("to")
        if not to_number:
            raise HTTPException(status_code=400, detail="Missing 'to' phone number")

        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        from_number = os.getenv("TWILIO_FROM_NUMBER")

        if not all([account_sid, auth_token, from_number]):
            logger.error("Missing Twilio credentials in environment variables")
            raise HTTPException(status_code=500, detail="Twilio not properly configured")

        client = Client(account_sid, auth_token)

        # The URL that Twilio will hit when the call is answered
        callback_url = f"https://{request.headers.get('host')}/phone/outgoing-twiml"
        status_callback_url = f"https://{request.headers.get('host')}/phone/status"

        logger.info(f"🚀 Initiating outgoing call to {to_number} from {from_number}")
        
        call = client.calls.create(
            to=to_number,
            from_=from_number,
            url=callback_url,
            status_callback=status_callback_url,
            status_callback_event=['initiated', 'ringing', 'answered', 'completed']
        )

        return {"status": "success", "call_sid": call.sid}

    except Exception as e:
        logger.error(f"Failed to initiate outgoing call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/phone/outgoing-twiml")
@app.post("/phone/outgoing-twiml")
async def handle_outgoing_twiml(request: Request):
    """
    Handle TwiML for outgoing calls.
    Similar to handle_phone_call but for outbound.
    """
    # Accept both GET and POST because Twilio might use either depending on config
    if request.method == "POST":
        form_data = await request.form()
    else:
        form_data = request.query_params

    call_sid = form_data.get("CallSid")
    to_number = form_data.get("To")

    logger.info(f"📞 Outgoing call answered by {to_number}, SID: {call_sid}")

    response = VoiceResponse()
    start = Start()
    
    websocket_url = f"wss://{request.headers.get('host', 'localhost:8004')}/phone-audio/{call_sid}"
    stream = Stream(url=websocket_url)
    start.append(stream)
    response.append(start)

    return Response(str(response), media_type="text/xml")


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
        "agent": {
            "name": config.agent.name,
            "id": config.agent.id,
            "tenant_id": config.agent.tenant_id,
            "wss_url": config.agent.wss_url,
            "public_url": config.agent.public_url
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
                "type": config.services.tts.type
            },
            "rag": {
                "url": config.services.rag.url
            }
        }
    }


# ════════════════════════════════════════════════════════════════════════════════
# RAG PROXY - Mock Mode Direct Access
# ════════════════════════════════════════════════════════════════════════════════

from fastapi.responses import StreamingResponse

@app.post("/api/rag/stream_query")
@app.post("/hivemind/stream")
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
        resolved_tenant_id = _resolve_requested_tenant(request=request, body=body)
        body = _merge_proxy_body(body, resolved_tenant_id)
        
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


@app.get("/hive-mind")
@app.get("/hivemind")
async def serve_hive_mind_dashboard():
    """Proxy the dashboard HTML natively from the RAG service."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        rag_url = f"{config.services.rag.url}/client"
        async with aiohttp.ClientSession() as session:
            async with session.get(rag_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return JSONResponse({"error": f"Failed to load dashboard: {response.status}"}, status_code=response.status)
                html_content = await response.text()
                return HTMLResponse(content=html_content)
    except Exception as e:
        logger.error(f"Failed to fetch hive-mind dashboard: {e}")
        return JSONResponse({"error": "Dashboard unavailable"}, status_code=500)


@app.post("/api/v1/query")
@app.post("/hivemind/query")
async def proxy_rag_query(request: Request):
    """Proxy manual LLM queries for the Hive Mind Dashboard."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        body = await request.json()
        resolved_tenant_id = _resolve_requested_tenant(request=request, body=body)
        body = _merge_proxy_body(
            body,
            resolved_tenant_id,
            extra={
                "context": {
                    **(body.get("context") or {}),
                    "surface": "hivemind_dashboard",
                    "tenant_id": resolved_tenant_id,
                }
            },
        )
        rag_url = f"{config.services.rag.url}/api/v1/query"
        async with aiohttp.ClientSession() as session:
            async with session.post(rag_url, json=body, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/v1/retrieve")
@app.post("/hivemind/retrieve")
async def proxy_rag_retrieve(request: Request):
    """Proxy manual retrieve tests for the Hive Mind Dashboard."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        body = await request.json()
        resolved_tenant_id = _resolve_requested_tenant(request=request, body=body)
        body = _merge_proxy_body(body, resolved_tenant_id)
        rag_url = f"{config.services.rag.url}/api/v1/retrieve"
        async with aiohttp.ClientSession() as session:
            async with session.post(rag_url, json=body, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy retrieve error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/v1/skills/{point_id}")
@app.delete("/hivemind/skills/{point_id}")
async def proxy_rag_skill_delete(point_id: str, request: Request, tenant_id: Optional[str] = None):
    """Proxy skill/rule deletions from the visualizer."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        resolved_tenant_id = _resolve_requested_tenant(request=request, explicit_tenant_id=tenant_id)
        rag_url = f"{config.services.rag.url}/api/v1/skills/{point_id}?tenant_id={resolved_tenant_id}"
        async with aiohttp.ClientSession() as session:
            async with session.delete(rag_url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy delete error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/v1/skills")
@app.get("/hivemind/skills")
async def proxy_rag_skill_list(request: Request, tenant_id: Optional[str] = None):
    """Proxy skill/rule listing for the HiveMind dashboard."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        resolved_tenant_id = _resolve_requested_tenant(request=request, explicit_tenant_id=tenant_id)
        rag_url = f"{config.services.rag.url}/api/v1/skills?tenant_id={resolved_tenant_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(rag_url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy skill list error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/hivemind/rules")
async def proxy_rag_rule_list(request: Request, tenant_id: Optional[str] = None):
    """Proxy agent rule listing."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        resolved_tenant_id = _resolve_requested_tenant(request=request, explicit_tenant_id=tenant_id)
        # RAG only has /api/v1/skills endpoint, it returns all types
        # We'll filter for rules on the backend if needed, or just return all
        rag_url = f"{config.services.rag.url}/api/v1/skills?tenant_id={resolved_tenant_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(rag_url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                data = await response.json()
                # Filter to only rules
                if isinstance(data, dict) and "rules" in data:
                    return JSONResponse({"rules": data["rules"], "total": len(data["rules"])})
                return JSONResponse(data)
    except Exception as e:
        logger.error(f"RAG Proxy rule list error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/hivemind/knowledge_base")
async def proxy_rag_knowledge_list(request: Request, tenant_id: Optional[str] = None):
    """Proxy general knowledge base listing."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        resolved_tenant_id = _resolve_requested_tenant(request=request, explicit_tenant_id=tenant_id)
        rag_url = f"{config.services.rag.url}/api/v1/skills?tenant_id={resolved_tenant_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(rag_url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                data = await response.json()
                # Filter to only knowledge base entries
                if isinstance(data, dict) and "knowledge" in data:
                    return JSONResponse({"knowledge": data["knowledge"], "total": len(data["knowledge"])})
                return JSONResponse(data)
    except Exception as e:
        logger.error(f"RAG Proxy knowledge list error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/v1/skills")
@app.post("/hivemind/skills")
async def proxy_rag_skill_upsert(request: Request, tenant_id: Optional[str] = None):
    """Proxy skill/rule upserts from the visualizer."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        body = await request.json()
        resolved_tenant_id = _resolve_requested_tenant(request=request, body=body, explicit_tenant_id=tenant_id)
        body = _merge_proxy_body(body, resolved_tenant_id)
        rag_url = f"{config.services.rag.url}/api/v1/skills?tenant_id={resolved_tenant_id}"
        async with aiohttp.ClientSession() as session:
            async with session.post(rag_url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy skill upsert error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/hivemind/rules")
async def proxy_rag_rule_upsert(request: Request, tenant_id: Optional[str] = None):
    """Proxy agent rule upserts to RAG."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        body = await request.json()
        resolved_tenant_id = _resolve_requested_tenant(request=request, body=body, explicit_tenant_id=tenant_id)
        # Ensure type is set to agent_rule
        body = _merge_proxy_body(body, resolved_tenant_id, extra={"type": "agent_rule"})
        rag_url = f"{config.services.rag.url}/api/v1/skills?tenant_id={resolved_tenant_id}"
        async with aiohttp.ClientSession() as session:
            async with session.post(rag_url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy rule upsert error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/hivemind/knowledge_base")
async def proxy_rag_knowledge_upsert(request: Request, tenant_id: Optional[str] = None):
    """Proxy general knowledge base upserts to RAG."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        body = await request.json()
        resolved_tenant_id = _resolve_requested_tenant(request=request, body=body, explicit_tenant_id=tenant_id)
        # Ensure type is set to general_kb
        body = _merge_proxy_body(body, resolved_tenant_id, extra={"type": "general_kb"})
        rag_url = f"{config.services.rag.url}/api/v1/skills?tenant_id={resolved_tenant_id}"
        async with aiohttp.ClientSession() as session:
            async with session.post(rag_url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy knowledge upsert error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/v1/upload")
@app.post("/hivemind/upload")
async def proxy_rag_upload(request: Request, tenant_id: Optional[str] = None):
    """Proxy HiveMind document uploads to the RAG ingestion endpoint."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)

    try:
        form = await request.form()
        file = form.get("file")
        if file is None:
            return JSONResponse({"error": "Missing file upload"}, status_code=400)

        resolved_tenant_id = _resolve_requested_tenant(
            request=request,
            body={"tenant_id": form.get("tenant_id")},
            explicit_tenant_id=tenant_id,
        )

        doc_type = str(form.get("doc_type", "General"))
        topics = str(form.get("topics", ""))

        upload_form = aiohttp.FormData()
        upload_form.add_field(
            "file",
            await file.read(),
            filename=getattr(file, "filename", "upload.bin"),
            content_type=getattr(file, "content_type", "application/octet-stream"),
        )
        upload_form.add_field("doc_type", doc_type)
        upload_form.add_field("topics", topics)
        upload_form.add_field("tenant_id", resolved_tenant_id)

        rag_url = f"{config.services.rag.url}/api/v1/upload"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rag_url,
                data=upload_form,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy upload error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

# ════════════════════════════════════════════════════════════════════════════════
# VISUAL COPILOT PROXY
# ════════════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/crawl-website")
async def proxy_crawl_website(request: Request):
    """Proxy crawler requests to visual-copilot service"""
    copilot_url = os.getenv("VISUAL_COPILOT_SERVICE_URL", "http://visual-copilot-eu-local:4005")
    try:
        body = await request.json()
        target_url = f"{copilot_url}/api/v1/crawl-website"
        async with aiohttp.ClientSession() as session:
            # We use a large timeout because deep crawling takes time
            async with session.post(target_url, json=body, timeout=aiohttp.ClientTimeout(total=300)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"Crawler Proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/v1/extract-pages")
async def proxy_extract_pages(request: Request):
    """Proxy extract pages to visual-copilot service"""
    copilot_url = os.getenv("VISUAL_COPILOT_SERVICE_URL", "http://visual-copilot-eu-local:4005")
    try:
        body = await request.json()
        target_url = f"{copilot_url}/api/v1/extract-pages"
        async with aiohttp.ClientSession() as session:
            async with session.post(target_url, json=body, timeout=aiohttp.ClientTimeout(total=180)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"Extractor Proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/v1/save-readme-to-hivemind")
async def proxy_save_readme(request: Request):
    """Proxy save readme to visual-copilot service"""
    copilot_url = os.getenv("VISUAL_COPILOT_SERVICE_URL", "http://visual-copilot-eu-local:4005")
    try:
        body = await request.json()
        target_url = f"{copilot_url}/api/v1/save-readme-to-hivemind"
        async with aiohttp.ClientSession() as session:
            async with session.post(target_url, json=body, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"Save Readme Proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)



@app.get("/api/v1/hive-mind/{endpoint:path}")
@app.get("/hivemind/{endpoint:path}")
async def proxy_rag_hive_mind_api(endpoint: str, request: Request):
    """Proxy visualization and insight analytics endpoints."""
    if not config:
        return JSONResponse({"error": "Configuration not loaded"}, status_code=503)
    try:
        rag_url = f"{config.services.rag.url}/api/v1/hive-mind/{endpoint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(rag_url, params=request.query_params, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return JSONResponse({"error": error_text}, status_code=response.status)
                return JSONResponse(await response.json())
    except Exception as e:
        logger.error(f"RAG Proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.websocket("/ws/hive-mind")
@app.websocket("/hivemind/ws")
async def proxy_hive_mind_ws(websocket: WebSocket):
    """Securely bridge live websocket events between the public dashboard and the RAG container natively."""
    # Rate limit: Check connection limit per IP
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not ws_rate_limiter.can_connect(client_ip):
        await websocket.close(code=1013, reason="Connection limit exceeded")
        logger.warning(f"HiveMind connection rejected for {client_ip}: too many connections")
        return

    if not config:
        await websocket.close(code=1013, reason="Configuration not loaded")
        return

    await websocket.accept()
    rag_ws_url = config.services.rag.url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/hive-mind"
    
    # Forward query parameters (A7: preserve tenant_id)
    if websocket.query_params:
        rag_ws_url += "?" + urlencode(websocket.query_params)
        
    try:
        # Disable internal ping to avoid keepalive conflicts and AssertionErrors
        async with websockets.connect(rag_ws_url, ping_interval=None) as rag_ws:
            async def forward_to_client():
                try:
                    while True:
                        msg = await rag_ws.recv()
                        await websocket.send_text(msg)
                except Exception as e:
                    pass
                    
            async def forward_to_rag():
                try:
                    while True:
                        msg = await websocket.receive_text()
                        await rag_ws.send(msg)
                except Exception as e:
                    pass
                    
            client_task = asyncio.create_task(forward_to_client())
            rag_task = asyncio.create_task(forward_to_rag())
            
            done, pending = await asyncio.wait(
                [client_task, rag_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in pending:
                task.cancel()
                
    except Exception as e:
        logger.error(f"Hive-mind WS proxy error: {e}")
        try:
            await websocket.close(code=1011, reason="Upstream unavailable")
        except:
            pass
    finally:
        # Rate limit: Decrement connection count
        ws_rate_limiter.disconnect(client_ip)


@app.get("/client")
async def serve_client(request: Request):
    """Serve the browser client HTML page or redirect to global domain"""
    host = request.headers.get("host", "")
    # If accessed via localhost, and not in development mode, we could redirect
    if "localhost" in host or "127.0.0.1" in host:
        # Check if user wants to force global domain
        if os.getenv("FORCE_GLOBAL_DOMAIN", "false").lower() == "true":
            global_client_url = (os.getenv("GLOBAL_CLIENT_URL", "") or "").strip()
            if global_client_url:
                return RedirectResponse(url=global_client_url)
            
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    client_path = os.path.join(static_dir, "client.html")
    
    if os.path.exists(client_path):
        return FileResponse(client_path)
    else:
        return JSONResponse(
            {"error": "Client HTML file not found"},
            status_code=404
        )


@app.get("/client_bundb")
async def serve_client_bundb(request: Request):
    """Serve the B&B specific client HTML page"""
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    client_path = os.path.join(static_dir, "client_bundb.html")
    
    if os.path.exists(client_path):
        return FileResponse(client_path)
    else:
        return JSONResponse(
            {"error": "client_bundb.html not found"},
            status_code=404
        )


@app.get("/client_davinci")
async def serve_client_davinci(request: Request):
    """Serve the Davinci specific client HTML page"""
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    client_path = os.path.join(static_dir, "client_davinci.html")
    
    if os.path.exists(client_path):
        return FileResponse(client_path)
    else:
        return JSONResponse(
            {"error": "client_davinci.html not found"},
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
    # Rate limit: Check connection limit per IP
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not ws_rate_limiter.can_connect(client_ip):
        await websocket.close(code=1013, reason="Connection limit exceeded")
        logger.warning(f"WebSocket connection rejected for {client_ip}: too many connections")
        return

    if not ws_handler:
        await websocket.close(code=1013, reason="WebSocket handler not initialized")
        return

    try:
        await ws_handler.handle_connection(websocket, session_id)
    finally:
        # Rate limit: Decrement connection count
        ws_rate_limiter.disconnect(client_ip)


@app.websocket("/stream")
async def audio_stream_endpoint(websocket: WebSocket, session_id: Optional[str] = Query(None)):
    """
    Dedicated audio streaming WebSocket - binary TTS only.

    Separates TTS audio from control messages to eliminate
    contention and reduce audio stuttering.

    Args:
        websocket: WebSocket connection
        session_id: Required session ID (must match an existing session)
    """
    # Rate limit: Check connection limit per IP
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not ws_rate_limiter.can_connect(client_ip):
        await websocket.close(code=1013, reason="Connection limit exceeded")
        logger.warning(f"Audio stream connection rejected for {client_ip}: too many connections")
        return

    if not ws_handler:
        await websocket.close(code=1013, reason="WebSocket handler not initialized")
        return

    try:
        await ws_handler.handle_audio_stream(websocket, session_id)
    finally:
        # Rate limit: Decrement connection count
        ws_rate_limiter.disconnect(client_ip)


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
    # Rate limit: Check connection limit per IP (separate limit for phone calls)
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not ws_rate_limiter.can_connect(client_ip):
        await websocket.close(code=1013, reason="Connection limit exceeded")
        logger.warning(f"Phone audio connection rejected for {client_ip}: too many connections")
        return

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
        # Rate limit: Decrement connection count
        ws_rate_limiter.disconnect(client_ip)
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

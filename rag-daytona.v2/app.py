"""
RAG Service FastAPI Application

HTTP REST API for knowledge base queries with Redis caching.

Reference:
    - Cloud Transformation doc (lines 474-641) - RAG service specifications
    - services/intent/app.py - FastAPI pattern
"""

import os
import sys
import time
import logging
import json
import hashlib
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List, Union, TYPE_CHECKING

import redis.asyncio as redis
import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, File, UploadFile, Form
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

from daytona_agent.services.shared.redis_client import get_redis_client, close_redis_client, ping_redis
from daytona_agent.services.rag.config import RAGConfig
from daytona_agent.services.rag.rag_engine import RAGEngine
from .models.hivemind_schema import (
    agent_skill_payload, agent_rule_payload,
    read_text, read_summary, read_doc_type, read_created_at,
    SCHEMA_VERSION,
)
from daytona_agent.services.rag.index_builder import IndexBuilder

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)


# Pydantic Models
class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="User question")
    context: Optional[Dict[str, Any]] = Field(None, description="Context from intent service")
    enable_streaming: Optional[bool] = Field(None, description="Enable streaming response")
    history_context: Optional[Union[str, List[Dict[str, Any]]]] = Field(None, description="Conversation history for context-aware responses")
    language: Optional[str] = Field("english", description="Response language: 'english' or 'german'")
    tenant_id: Optional[str] = Field("tara", description="Tenant/Agent identifier for cache isolation")


class QueryResponse(BaseModel):
    answer: str = Field(..., description="Generated answer")
    sources: List[str] = Field(..., description="Source document filenames")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Retrieval confidence")
    timing_breakdown: Dict[str, float] = Field(..., description="Timing metrics")
    cached: bool = Field(..., description="Whether result was served from cache")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class HealthResponse(BaseModel):
    status: str
    index_loaded: bool
    index_size: int
    cache_hit_rate: float
    redis_connected: bool
    gemini_available: bool
    qdrant_enabled: bool
    qdrant_url: Optional[str] = None
    uptime_seconds: float


class RebuildIndexRequest(BaseModel):
    knowledge_base_path: Optional[str] = Field(None, description="Override knowledge base path")


class RebuildIndexResponse(BaseModel):
    status: str
    documents_indexed: int
    categories: int
    build_time_seconds: float


class SaveCaseRequest(BaseModel):
    user_id: str = Field(..., description="User phone number or ID")
    issue: Optional[str] = Field(None, description="The problem that was resolved")
    solution: Optional[str] = Field(None, description="How the problem was solved")
    history_context: Optional[str] = Field(None, description="Conversation history to distill")
    tenant_id: str = Field("tara", description="Tenant identifier")
    tenant_id: str = Field("tara", description="Tenant identifier")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional context")


class SaveCaseResponse(BaseModel):
    status: str
    message: str
    case_id: Optional[str] = None


class CheckDomainRequest(BaseModel):
    url: str = Field(..., description="Current page URL")
    client_id: str = Field("tara", description="Client/Tenant ID")

class AnalyzeSessionRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    history_context: List[Dict[str, Any]] = Field(..., description="Raw conversation logs")
    user_id: Optional[str] = Field(None, description="User identifier")
    tenant_id: str = Field("tara", description="Tenant identifier")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional context")
    brief_context: Optional[str] = Field(None, description="Brief summary of what happened in the session (pre-computed)")
    backend_url: Optional[str] = Field(None, description="Optional backend URL to send the session report to")


class AnalyzeSessionResponse(BaseModel):
    status: str
    report: Dict[str, Any]


class VisualOrchestrateRequest(BaseModel):
    query: str = Field(..., description="User query text")
    session_id: str = Field(..., description="Session identifier")
    dom_context: List[Dict[str, Any]] = Field(..., description="List of visible DOM elements")
    history_context: Optional[str] = Field(None, description="Conversation history")
    language: Optional[str] = Field("english", description="Response language")
    tenant_id: Optional[str] = Field("tara", description="Tenant identifier")
    
class PlanStepRequest(BaseModel):
    goal: str = Field(..., description="The user's overall mission goal")
    dom_context: List[Dict[str, Any]] = Field(..., description="Current visible DOM elements")
    step_number: int = Field(0, description="Current step count in the mission")
    warning_message: Optional[str] = Field("", description="Warning about stagnant DOM or previous failures")
    current_url: Optional[str] = Field("", description="Current page URL for GPS hints")
    last_action: Optional[str] = Field("", description="Summary of last action taken")
    action_history: Optional[List[str]] = Field(default_factory=list, description="List of recent actions for loop detection")
    map_hints: Optional[str] = Field("", description="Pre-fetched GPS navigation hints")
    client_id: Optional[str] = Field("tara", description="Client/Tenant ID for map lookup")
    session_id: str = Field(..., description="Session identifier")
    dom_diff: Optional[str] = Field("First Step", description="Diff summary of DOM changes")
    conversation_history: Optional[str] = Field("", description="Recent conversation context (last 4 user requests)")
    last_dom_context: Optional[List[Dict[str, Any]]] = Field(None, description="Pre-action DOM snapshot for validation")
    fast_sense_speech: Optional[str] = Field(None, description="Speech already spoken by fast_sense (to avoid double-speaking)")
    interaction_mode: str = Field("interactive", description="Interaction mode (turbo or interactive)")
    # v5: Semantic Page Graph fields
    active_states: Optional[Dict[str, Any]] = Field(None, description="Active nav/tab states from widget")
    data_tables: Optional[List[Dict[str, Any]]] = Field(None, description="Extracted table data from widget")
    page_title: Optional[str] = Field("", description="Document title from widget")
    # v6: Vision — base64-encoded JPEG screenshot of the current viewport (optional)
    screenshot_b64: Optional[str] = Field(None, description="Base64 JPEG screenshot of current page (for Groq Vision in last mile)")
    # Pre-route handoff (optional): produced by /api/v1/get_map_hints
    pre_decision: Optional[Dict[str, Any]] = Field(None, description="Pre-route decision payload from get_map_hints")
    route_hint: Optional[str] = Field("", description="Pre-route route hint from get_map_hints")

class MapHintsRequest(BaseModel):
    goal: str = Field(..., description="The user's mission goal")
    client_id: Optional[str] = Field("tara", description="Client/Tenant ID")
    current_url: Optional[str] = Field("", description="Current page URL for domain filtering")
    session_id: Optional[str] = Field("", description="Session ID for screenshot cache fallback")
    dom_context: Optional[List[Dict[str, Any]]] = Field(None, description="Optional DOM elements to pre-seed LiveGraph")
    screenshot_b64: Optional[str] = Field(
        None,
        description="Optional base64 JPEG screenshot for vision pre-routing",
    )

class FastSenseRequest(BaseModel):
    goal: str = Field(..., description="The user's mission goal")
    dom_context: List[Dict[str, Any]] = Field(..., description="List of visible DOM elements")
    session_id: str = Field(..., description="Session identifier")

class LiveGraphSeedRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    dom_context: List[Dict[str, Any]] = Field(..., description="Current visible DOM elements")
    current_url: Optional[str] = Field("", description="Current page URL")
    page_title: Optional[str] = Field("", description="Page title")


class EmbedRequest(BaseModel):
    text: str = Field(..., description="Text to embed")

class EmbedResponse(BaseModel):
    embedding: List[float]

class DynamicExitRequest(BaseModel):
    history_context: str = Field(..., description="Conversation history for context")
    language: Optional[str] = Field("english", description="Response language")


# ── Agent Skills & Rules Models ──────────────────────────────────────────────

class SkillRuleCreateRequest(BaseModel):
    text: str = Field(..., min_length=5, description="Skill or rule description text")
    type: str = Field(..., description="'agent_skill' or 'agent_rule'")
    topic: str = Field("general", description="Topic category (e.g. debugging, identity, format)")
    severity: Optional[str] = Field(None, description="For rules: 'critical' or 'standard'")
    tenant_id: str = Field("tara", description="Tenant identifier")

class SkillRuleItem(BaseModel):
    id: str
    text: str
    type: str
    topic: str
    severity: Optional[str] = None
    score: Optional[float] = None
    timestamp: Optional[int] = None

class SkillRuleListResponse(BaseModel):
    skills: List[SkillRuleItem]
    rules: List[SkillRuleItem]
    total: int


# Global state (initialized in lifespan)
rag_engine: Optional[RAGEngine] = None
visual_orchestrator: Optional[Any] = None
redis_client: Optional[redis.Redis] = None
session_analytics: Optional[Any] = None
cache_hits = 0
cache_misses = 0
app_start_time = 0.0
hive_mind_connections: List[WebSocket] = []
ingestion_service: Optional[Any] = None


# Redis client utilities
# Removed custom implementation in favor of shared client
# async def get_redis_client() -> redis.Redis: ...
# async def close_redis_client(client: redis.Redis): ...
# async def ping_redis(client: redis.Redis) -> bool: ...


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup/shutdown."""
    global rag_engine, redis_client, cache_hits, cache_misses, app_start_time
    
    # Startup
    logger.info(" Starting RAG service...")
    app_start_time = time.time()
    
    try:
        # Load config
        config = RAGConfig.from_env()
        logger.info(" Configuration loaded")

        # Log web search configuration
        if config.enable_web_search:
            if config.google_search_api_key and config.google_cse_id:
                logger.info(f" ✅ Web search ENABLED (API key: {config.google_search_api_key[:10]}..., CSE ID: {config.google_cse_id})")
            else:
                logger.warning(f" ⚠️ Web search enabled but credentials missing (API key: {'present' if config.google_search_api_key else 'missing'}, CSE ID: {'present' if config.google_cse_id else 'missing'})")
        else:
            logger.info(" ℹ️ Web search DISABLED (set ENABLE_WEB_SEARCH=true to enable)")

        # Create RAG engine
        rag_engine = RAGEngine(config)

        # Log Qdrant (Hive Mind) status
        if rag_engine.qdrant and rag_engine.qdrant.enabled:
            logger.info(f"🧠 ✅ Qdrant Hive Mind CONNECTED: {rag_engine.qdrant.url}")
            logger.info(f"🧠    Collection: {rag_engine.qdrant.collection_name}")
        else:
            logger.warning(f"🧠 ⚠️ Qdrant Hive Mind NOT AVAILABLE - check QDRANT_URL and QDRANT_API_KEY env vars")

        # Initialize Session Analytics
        from daytona_agent.services.rag.session_analytics import SessionAnalytics
        app.state.session_analytics = SessionAnalytics(
            llm_provider=rag_engine.llm,
            model_name=config.analytics_model
        )
        logger.info(f" ✅ Session Analytics initialized using model: {config.analytics_model}")
        logger.info(f" 🤖 RAG Engine using LLM model: {config.llm_model}")


        # Initialize Ingestion Service
        try:
            from core.processing.ingestion import IngestionService
            global ingestion_service
            # SHARE EMBEDDINGS to avoid double loading memory spike
            shared_embeddings = rag_engine.embeddings if rag_engine else None
            ingestion_service = IngestionService(embeddings=shared_embeddings)
            app.state.ingestion_service = ingestion_service
            logger.info(" ✅ Ingestion Service initialized (with shared embeddings)")
        except Exception as e:
            logger.error(f" ❌ Failed to initialize Ingestion Service: {e}")
        
        # If index not loaded, try to build it (only if local retrieval is enabled)
        if config.enable_local_retrieval:
            if not rag_engine.vector_store or not rag_engine.documents:
                logger.warning(" FAISS index not found, attempting to build...")
                from daytona_agent.services.rag.index_builder import IndexBuilder
                builder = IndexBuilder(config)
                if builder.build_index():
                    rag_engine.load_index()  # Reload after build
                    logger.info(f" Index built successfully: {len(rag_engine.documents)} documents")
                else:
                    logger.error(" Index build failed")
            
            # Final validation
            if not rag_engine.vector_store or not rag_engine.documents:
                logger.error(" FAISS index not available - service in degraded mode")
            else:
                logger.info(f" RAG engine initialized: {len(rag_engine.documents)} documents")
        else:
            logger.info("ℹ️ Local retrieval disabled - skipping FAISS index checks")
        
        # Connect to Redis (optional - service can run without it)
        try:
            # Use shared client which handles config from env vars
            redis_client = await asyncio.wait_for(get_redis_client(), timeout=15.0)
            await asyncio.wait_for(ping_redis(redis_client), timeout=5.0)
            logger.info(f" Redis connected successfully")
        except asyncio.TimeoutError:
            logger.warning(f" Redis connection timeout - service will run in degraded mode")
            redis_client = None
        except Exception as redis_error:
            logger.warning(f" Redis connection failed: {redis_error} - caching disabled")
            redis_client = None
        
        # Initialize Visual Orchestrator (requires Groq) - Wired AFTER Redis to enable sessions
        if config.llm_provider == "groq":
            from visual_orchestrator import VisualOrchestrator
            global visual_orchestrator
            # Pass redis_client for session persistence (A7)
            visual_orchestrator = VisualOrchestrator(
                rag_engine.llm,
                qdrant_client=rag_engine.qdrant,
                embeddings=rag_engine.embeddings,
                redis_client=redis_client
            )
            logger.info(f" ✅ Visual Orchestrator initialized with Qdrant GPS & Redis Sessions ({'Connected' if redis_client else 'Disconnected'})")
            
            # Store in app.state for Ultimate TARA to access GPS hints
            app.state.visual_orchestrator = visual_orchestrator
        else:
            logger.warning(f" ℹ️ Visual Orchestrator requires 'groq' provider (current: {config.llm_provider})")
        
        # Initialize counters
        cache_hits = 0
        cache_misses = 0
        
        # Store in app state
        app.state.rag_engine = rag_engine
        app.state.redis = redis_client
        app.state.cache_hits = cache_hits
        app.state.cache_misses = cache_misses
        app.state.start_time = app_start_time

        # ═══════════════════════════════════════════════════════════
        # ULTIMATE TARA MODULES INITIALIZATION (AFTER redis ready)
        # ═══════════════════════════════════════════════════════════
        logger.info("=" * 70)
        logger.info("🚀 Initializing ULTIMATE TARA Architecture")
        logger.info("=" * 70)
        
        # Import Ultimate TARA modules
        from tara_models import TacticalSchema, ActionIntent
        from mind_reader import MindReader
        from hive_interface import HiveInterface
        from live_graph import LiveGraph
        from semantic_detective import SemanticDetective
        from mission_brain import MissionBrain
        
        # Initialize Mind Reader
        try:
            app.state.mind_reader = MindReader(rag_engine.llm)
            logger.info("✅ Mind Reader initialized (Llama-3.1-8B for intent parsing)")
        except Exception as e:
            logger.warning(f"⚠️ Mind Reader init failed: {e}")
            app.state.mind_reader = None
        
        # Initialize Hive Interface
        try:
            app.state.hive_interface = HiveInterface(
                qdrant_client=rag_engine.qdrant.client if rag_engine.qdrant else None,
                embeddings=rag_engine.embeddings,
                redis_client=app.state.redis,
                collection_name=config.qdrant_collection or "tara_hive"
            )
            logger.info("✅ Hive Interface initialized (Qdrant dual-retrieval)")
        except Exception as e:
            logger.warning(f"⚠️ Hive Interface init failed: {e}")
            app.state.hive_interface = None
        
        # Initialize Live Graph
        try:
            app.state.live_graph = LiveGraph(app.state.redis)
            logger.info("✅ Live Graph initialized (Redis DOM mirror)")
        except Exception as e:
            logger.warning(f"⚠️ Live Graph init failed: {e}")
            app.state.live_graph = None
        
        # Initialize Semantic Detective
        # Reuse the already-loaded embeddings from rag_engine to avoid a second model load
        try:
            app.state.semantic_detective = SemanticDetective(
                live_graph=app.state.live_graph,
                embeddings=rag_engine.embeddings
            )
            logger.info("✅ Semantic Detective initialized (hybrid scoring)")
        except Exception as e:
            logger.warning(f"⚠️ Semantic Detective init failed: {e}")
            app.state.semantic_detective = None
        
        # Initialize Mission Brain
        try:
            app.state.mission_brain = MissionBrain(
                redis_client=app.state.redis,
                hive_interface=app.state.hive_interface
            )
            logger.info("✅ Mission Brain initialized (constraint enforcement)")
        except Exception as e:
            logger.warning(f"⚠️ Mission Brain init failed: {e}")
            app.state.mission_brain = None
        
        logger.info("=" * 70)
        logger.info("✅ ULTIMATE TARA Architecture Ready")
        logger.info("=" * 70)

        logger.info(" RAG service ready")
        
        yield
        
        # Shutdown
        logger.info(" Shutting down RAG service...")
        
        # Log performance stats
        if rag_engine:
            stats = rag_engine.get_performance_stats()
            logger.info(f" Performance stats: {stats}")
        
        # Close Redis
        if redis_client:
            # Shared client manages its own lifecycle, but we can close our reference
            # Actually, shared client is singleton, so we shouldn't close it here if other services use it
            # But since this is microservice, we are the only user in this process.
            # However, close_redis_client in shared lib closes the global client.
            await close_redis_client() 
            logger.info(" Redis connection closed")
        
        logger.info(" RAG service shutdown complete")
    
    except Exception as e:
        logger.error(f" Startup error: {e}", exc_info=True)
        raise


# Support for path-based routing (e.g. /rag)
root_path = os.getenv("RAG_ROOT_PATH", "")

# Create FastAPI app
app = FastAPI(
    title="Daytona RAG Service",
    description="Knowledge base retrieval and intelligence service",
    version="2.1.0",
    lifespan=lifespan,
    root_path=root_path
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# app.add_middleware(GZipMiddleware, minimum_size=1000) # Disabled to prevent blank page issues

# Middleware to strip /rag or /cartesia prefix if present (for path-based routing)
@app.middleware("http")
async def strip_path_prefix(request: Request, call_next):
    path = request.url.path
    logger.info(f"🔍 Incoming request path: {path}")
    
    # Store whether we should skip generation in request state
    # Triggered by /cartesia prefix
    request.state.skip_generation = path.startswith("/cartesia")
    
    prefix = None
    if path.startswith("/rag"):
        prefix = "/rag"
    elif path.startswith("/cartesia"):
        prefix = "/cartesia"
        
    if prefix:
        # Create a new scope with the adjusted path
        new_path = path[len(prefix):] or "/"
        logger.info(f"✂️ Stripping prefix '{prefix}': {path} -> {new_path}")
        request.scope["path"] = new_path
        # Also update raw_path if needed
        if "raw_path" in request.scope:
            request.scope["raw_path"] = new_path.encode()
    else:
        logger.info(f"➡️ No prefix matched (checked /rag, /cartesia)")
            
    response = await call_next(request)
    return response

# Helper function to get static directory path
def get_static_dir():
    """Get the static directory path, handling both direct and module imports."""
    # Try current file's directory first
    current_dir = os.path.dirname(os.path.abspath(__file__))
    static_dir = os.path.join(current_dir, "static")
    
    if os.path.exists(static_dir):
        return static_dir
    
    # Fallback: try relative to package root
    package_dir = os.path.dirname(os.path.dirname(current_dir))
    static_dir = os.path.join(package_dir, "rag-leibniz", "static")
    
    if os.path.exists(static_dir):
        return static_dir
    
    return None

# Mount static files directory for client.html
# Note: Mount must happen before route definitions to avoid conflicts
static_dir = get_static_dir()
if static_dir:
    try:
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        logger.info(f"✅ Static files mounted from: {static_dir}")
    except Exception as e:
        logger.warning(f"⚠️ Could not mount static files: {e}")
else:
    logger.warning(f"⚠️ Static directory not found")

@app.get("/")
async def root():
    """Serve the RAG testing client interface."""
    static_dir_path = get_static_dir()
    if static_dir_path:
        client_path = os.path.join(static_dir_path, "client.html")
        if os.path.exists(client_path):
            logger.info(f"Serving client from: {client_path}")
            return FileResponse(client_path, media_type="text/html")
        else:
            logger.error(f"Client file not found at: {client_path}")
    else:
        logger.error("Static directory not found")
    
    # Fallback: return simple HTML with instructions
    return {
        "message": "RAG Service API",
        "client_url": "/static/client.html",
        "docs": "/docs",
        "health": "/health"
    }

from fastapi.responses import RedirectResponse
@app.get("/client")
async def client_redirect():
    return RedirectResponse(url="/")


class RetrieveResponse(BaseModel):
    query_english: str
    original_language: str
    relevant_docs: List[Dict[str, Any]]
    agent_skills: List[Dict[str, Any]] = []
    agent_rules: List[Dict[str, Any]] = []
    general_kb: List[Dict[str, Any]] = []
    hive_mind_context: str
    web_results: str
    timing: Dict[str, float]
    fast_path_type: Optional[str]
    history_context: str


@app.post("/api/v1/retrieve", response_model=RetrieveResponse)
async def retrieve_only(request: QueryRequest):
    """
    Retrieve relevant context (Docs, Hive Mind, Web) WITHOUT generating an answer.
    
    Used by voice agents (abella) to fetch context for local generation.
    """
    try:
        # Ensure context exists and contains language
        context = request.context or {}
        if request.language:
            context['language'] = request.language
            
        # Call the new retrieve_context method
        result = await app.state.rag_engine.retrieve_context(
            request.query,
            context,
            history_context=request.history_context
        )
        
        return RetrieveResponse(**result)
    
    except Exception as e:
        logger.error(f"Retrieve error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Retrieval failed: {str(e)}")


@app.post("/api/v1/visual_orchestrate")
async def visual_orchestrate(request: VisualOrchestrateRequest):
    """
    Dual-stream orchestration for Visual Co-Pilot.
    Parallelizes voice filler and visual action reasoning.
    """
    if visual_orchestrator is None:
        raise HTTPException(status_code=501, detail="Visual Orchestrator not initialized or requires Groq")

    logger.info(f"🎨 Visual Orchestration Request | Session: {request.session_id} | Query: '{request.query}'")
    logger.debug(f"🔍 DOM Context: {len(request.dom_context)} elements")

    async def stream_generator():
        try:
            async for chunk in visual_orchestrator.orchestrate(
                query=request.query,
                dom_context=request.dom_context,
                history=request.history_context or "",
                language=request.language
            ):
                logger.debug(f"📤 Streaming chunk to Orchestrator: {chunk}")
                yield json.dumps(chunk) + "\n"
        except Exception as e:
            logger.error(f"Visual orchestration error: {e}", exc_info=True)
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"


    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")

# ═══════════════════════════════════════════════════════════
# ULTIMATE TARA ENDPOINTS (Primary Pipeline)
# ═══════════════════════════════════════════════════════════

# ── Analyse Page ─────────────────────────────────────────────────────────────

class AnalysePageRequest(BaseModel):
    session_id: str = Field(..., description="Orchestra session ID")
    analysis_mode: str = Field("dom", description="'dom' or 'vision'")
    current_url: str = Field("", description="Current page URL")
    page_title: str = Field("", description="Page title")
    screenshot_b64: Optional[str] = Field(None, description="Base64 JPEG for vision mode")
    dom_context: Optional[List[Dict[str, Any]]] = Field(None, description="DOM elements for dom mode")
    user_question: Optional[str] = Field(None, description="User's specific question about the page (None = overview narration)")


@app.post("/api/v1/analyse_page")
async def analyse_page_endpoint(request: AnalysePageRequest):
    """
    🔍 Analyse Page — one-shot page narration via Visual Co-pilot.

    Accepts DOM elements or a screenshot, returns a natural-language narration
    of what TARA sees on the page. Called by the Orchestrator when the user
    clicks DOM or Vision in the Analyse strip.
    """
    logger.info(
        f"🔍 Analyse Page | Session: {request.session_id} | "
        f"mode={request.analysis_mode} | url={request.current_url[:60]}"
    )
    try:
        from visual_copilot.api.analyse_page import analyse_page
        narration = await analyse_page(
            analysis_mode=request.analysis_mode,
            current_url=request.current_url,
            page_title=request.page_title,
            session_id=request.session_id,
            screenshot_b64=request.screenshot_b64,
            dom_context=request.dom_context,
            user_question=request.user_question,
        )
        return {
            "success": True,
            "narration": narration,
            "analysis_mode": request.analysis_mode,
            "session_id": request.session_id,
        }
    except Exception as e:
        logger.error(f"❌ Analyse Page failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/plan_next_step")
async def plan_next_step(request: PlanStepRequest):
    """
    ULTIMATE TARA PIPELINE - Primary planning endpoint.
    
    Uses:
    - Mind Reader (intent parsing)
    - Hive Interface (Qdrant retrieval)
    - Mission Brain (constraint enforcement)
    - Semantic Detective (hybrid scoring)
    - Live Graph (Redis DOM mirror)
    
    Falls back to legacy if Ultimate TARA modules unavailable.
    """
    logger.info(f"🚀 Ultimate TARA Plan | Session: {request.session_id} | Goal: '{request.goal}' | Step: {request.step_number}")
    if request.screenshot_b64:
        cache = getattr(app.state, "latest_screenshots", None)
        if cache is None:
            cache = {}
            app.state.latest_screenshots = cache
        cache[request.session_id] = request.screenshot_b64
    
    # Check if Ultimate TARA modules are available
    if not all([
        app.state.mind_reader,
        app.state.hive_interface,
        app.state.mission_brain,
        app.state.semantic_detective,
        app.state.live_graph
    ]):
        logger.warning("⚠️ Ultimate TARA not fully available, falling back to legacy")
        return await legacy_plan_next_step(request)
    
    try:
        from ultimate_api import ultimate_plan_next_step
        
        # First ingest DOM into Live Graph
        if request.dom_context:
            from tara_models import GraphNode
            nodes = [GraphNode.from_dict(el) for el in request.dom_context if isinstance(el, dict)]
            delta = {
                "delta_type": "full_scan",
                "nodes": [n.to_redis_dict() for n in nodes],
                "url": request.current_url,
                "timestamp": time.time()
            }
            await app.state.live_graph.ingest_delta(request.session_id, delta)
        
        # Run Ultimate TARA pipeline
        result = await ultimate_plan_next_step(
            app=app,
            session_id=request.session_id,
            goal=request.goal,
            current_url=request.current_url,
            step_number=request.step_number,
            action_history=request.action_history or [],
            screenshot_b64=request.screenshot_b64 or "",
            pre_decision=request.pre_decision,
            route_hint=request.route_hint or "",
        )

        if isinstance(result, dict):
            action_obj = result.get("action")
            action_type = "none"
            action_target = "none"
            if isinstance(action_obj, dict):
                action_type = action_obj.get("type", "none") or "none"
                action_target = action_obj.get("target_id", "none") or "none"
            elif isinstance(action_obj, list):
                # Bundled pipeline: report the first meaningful actionable step.
                for step in action_obj:
                    if not isinstance(step, dict):
                        continue
                    step_type = (step.get("type") or "").strip()
                    if step_type in {"", "wait", "scroll"}:
                        continue
                    action_type = step_type
                    action_target = step.get("target_id", "none") or "none"
                    break
                if action_type == "none" and action_obj:
                    # Fallback to last valid step if pipeline has only passive actions.
                    for step in reversed(action_obj):
                        if isinstance(step, dict):
                            action_type = (step.get("type") or "none") or "none"
                            action_target = step.get("target_id", "none") or "none"
                            break
            logger.info(
                f"✅ Ultimate TARA { 'success' if result.get('success') else 'failure' }: "
                f"{action_type} on {action_target}"
            )
            return result

        # ═══════════════════════════════════════════════════════════
        # Fallback to legacy — but INJECT hive context so the LLM
        # doesn't hallucinate navigation steps
        # ═══════════════════════════════════════════════════════════
        if isinstance(result, dict) and result.get("no_legacy_fallback"):
            logger.info("⏩ Ultimate TARA returned no_legacy_fallback, skipping legacy")
            return result
        logger.warning("⚠️ Ultimate TARA returned no result, falling back to legacy with hive context")
        try:
            from tara_models import TacticalSchema, ActionIntent
            from mind_reader import MindReader
            schema = await app.state.mind_reader.translate(
                user_input=request.goal,
                current_url=request.current_url
            )
            hive_response = await app.state.hive_interface.retrieve(schema)
            hive_context_parts = []
            if hive_response.strategy:
                hive_context_parts.append(
                    f"STRATEGY SEQUENCE: {' → '.join(hive_response.strategy.sequence)}"
                )
            if hive_response.visual_hints:
                hint_strs = [
                    f"- {h.text_pattern} ({h.element_type} in {h.zone}, selector={h.selector})"
                    for h in hive_response.visual_hints[:8]
                ]
                hive_context_parts.append(
                    "VISUAL HINTS:\n" + "\n".join(hint_strs)
                )
            if hive_context_parts:
                extra_hints = "\n\n".join(hive_context_parts)
                request.map_hints = (request.map_hints or "") + "\n\n" + extra_hints
                logger.info(f"   💡 Injected hive context into legacy fallback: strategy={bool(hive_response.strategy)}, hints={len(hive_response.visual_hints)}")
        except Exception as hive_err:
            logger.warning(f"⚠️ Could not inject hive context into fallback: {hive_err}")
        
        return await legacy_plan_next_step(request)
        
    except Exception as e:
        logger.error(f"❌ Ultimate TARA failed: {e}", exc_info=True)
        logger.warning("⚠️ Falling back to legacy pipeline")
        return await legacy_plan_next_step(request)


async def legacy_plan_next_step(request: PlanStepRequest):
    """
    Legacy planning endpoint (fallback).
    Uses old visual_orchestrator with detective.py
    """
    if visual_orchestrator is None:
        raise HTTPException(status_code=501, detail="Visual Orchestrator not initialized")

    logger.info(f"🎯 [LEGACY] Planner Request | Session: {request.session_id} | Goal: '{request.goal}'")

    try:
        result = await visual_orchestrator.plan_next_step(
            goal=request.goal,
            dom_context=request.dom_context,
            step_number=request.step_number,
            warning_message=request.warning_message,
            current_url=request.current_url,
            last_action=request.last_action,
            map_hints=request.map_hints,
            client_id=request.client_id,
            action_history=request.action_history or [],
            dom_diff=request.dom_diff or "",
            conversation_history=request.conversation_history or "",
            last_dom_context=request.last_dom_context,
            fast_sense_speech=request.fast_sense_speech,
            interaction_mode=request.interaction_mode,
            session_id=request.session_id,
            active_states=request.active_states,
            data_tables=request.data_tables,
            page_title=request.page_title or "",
        )
        return result
    except Exception as e:
        logger.error(f"Legacy planning error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/fast_sense")
async def fast_sense(request: FastSenseRequest):
    """
    Fast Sense: Quick DOM scan for immediate TTS response.
    Uses Mind Reader for fast intent parsing.
    """
    logger.info(f"⚡ Fast Sense | Session: {request.session_id} | Goal: '{request.goal}' | DOM: {len(request.dom_context)} elements")
    
    # Try Ultimate TARA fast sense first
    if app.state.mind_reader:
        try:
            # Bypass Mind Reader LLM for fast_sense, just do basic extraction
            goal_lower = request.goal.lower()
            prefixes_to_strip = ["i want to ", "can you ", "please ", "show me ", "find ", "click ", "go to ", "navigate to ", "what is "]
            extracted_entity = request.goal
            for prefix in prefixes_to_strip:
                if goal_lower.startswith(prefix):
                    extracted_entity = request.goal[len(prefix):]
                    break
            
            speech = f"Looking for {extracted_entity}..."
            
            return {
                "speech": speech,
                "relevant_ids": [],
                "intent": "navigation",
                "pipeline": "ultimate_fast"
            }
        except Exception as e:
            logger.warning(f"⚠️ Ultimate fast sense failed: {e}")
    
    # Fallback to legacy
    if visual_orchestrator is None:
        return {"speech": "", "relevant_ids": []}
    
    try:
        result = await visual_orchestrator._run_fast_sense(
            goal=request.goal,
            dom_context=request.dom_context
        )
        logger.info(f"⚡ [LEGACY] Fast Sense Response | Speech: \"{result.get('speech', '')[:50]}...\"")
        return result
    except Exception as e:
        logger.error(f"Fast sense error: {e}", exc_info=True)
        return {"speech": "", "relevant_ids": []}

@app.post("/api/v1/get_map_hints")
async def get_map_hints(request: MapHintsRequest):
    """
    Fetch modular Hive hints for a given goal.
    Called once at mission start by the widget.
    """
    from visual_copilot.api.map_hints import build_map_hints
    screenshot_b64 = request.screenshot_b64
    if not screenshot_b64 and request.session_id:
        cache = getattr(app.state, "latest_screenshots", {}) or {}
        screenshot_b64 = cache.get(request.session_id)

    # Optional inline seed: allows frontend to warm LiveGraph in the same map_hints call.
    if request.session_id and request.dom_context and getattr(app.state, "live_graph", None):
        try:
            from tara_models import GraphNode
            seed_nodes = [GraphNode.from_dict(el) for el in request.dom_context if isinstance(el, dict)]
            seed_delta = {
                "delta_type": "full_scan",
                "nodes": [n.to_redis_dict() for n in seed_nodes],
                "url": request.current_url or "",
                "timestamp": time.time(),
            }
            await app.state.live_graph.ingest_delta(request.session_id, seed_delta)
            logger.info(
                f"🗺️ Map Hints pre-seed | Session: {request.session_id} | nodes={len(seed_nodes)}"
            )
        except Exception as seed_err:
            logger.warning(f"Map Hints pre-seed skipped due to error: {seed_err}")

    return await build_map_hints(
        goal=request.goal,
        client_id=request.client_id or "tara",
        current_url=request.current_url or "",
        screenshot_b64=screenshot_b64,
        mind_reader=app.state.mind_reader,
        hive_interface=app.state.hive_interface,
        mission_brain=getattr(app.state, "mission_brain", None),
        live_graph=getattr(app.state, "live_graph", None),
        session_id=getattr(request, "session_id", "") or "",
    )


class PushScreenshotRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    screenshot_b64: str = Field(..., description="Base64-encoded JPEG screenshot")
    source: Optional[str] = Field("orchestrator", description="Who pushed this screenshot")

@app.post("/api/v1/push_screenshot")
async def push_screenshot(request: PushScreenshotRequest):
    """
    Called by the Orchestrator whenever it receives a fresh screenshot from the browser.
    Caches it in app.state.latest_screenshots so the Last-Mile screenshot broker
    can retrieve a live image without needing its own direct WebSocket to the browser.
    """
    if not request.screenshot_b64:
        return {"success": False, "reason": "empty_screenshot"}

    cache = getattr(app.state, "latest_screenshots", None)
    if cache is None:
        cache = {}
        app.state.latest_screenshots = cache

    prev_len = len(cache.get(request.session_id, "") or "")
    cache[request.session_id] = request.screenshot_b64
    new_len = len(request.screenshot_b64)
    logger.info(
        f"📸 PUSH_SCREENSHOT | session={request.session_id} "
        f"source={request.source} size={new_len // 1024}KB (prev={prev_len // 1024}KB)"
    )
    return {"success": True, "size_kb": new_len // 1024}


@app.post("/api/v1/livegraph_seed")
async def livegraph_seed(request: LiveGraphSeedRequest):
    """
    Seed LiveGraph early (e.g., when widget opens) so pre-route has DOM ready.
    """
    if not getattr(app.state, "live_graph", None):
        raise HTTPException(status_code=503, detail="LiveGraph not initialized")

    start = time.time()
    try:
        from tara_models import GraphNode

        # ── Seed throttling: avoid burst full-scans for identical/near-identical payloads ──
        now = time.time()
        throttle = getattr(app.state, "livegraph_seed_throttle", None)
        if throttle is None:
            throttle = {}
            app.state.livegraph_seed_throttle = throttle

        min_interval_s = float(os.getenv("LIVEGRAPH_SEED_MIN_INTERVAL_MS", "700")) / 1000.0
        hard_min_interval_s = float(os.getenv("LIVEGRAPH_SEED_HARD_MIN_INTERVAL_MS", "120")) / 1000.0
        max_small_delta_ratio = float(os.getenv("LIVEGRAPH_SEED_SMALL_DELTA_RATIO", "0.06"))
        dedupe_ttl_s = float(os.getenv("LIVEGRAPH_SEED_DEDUPE_TTL_MS", "2500")) / 1000.0
        cleanup_ttl_s = float(os.getenv("LIVEGRAPH_SEED_CLEANUP_TTL_SEC", "600"))

        dom_context = request.dom_context or []
        node_count = len(dom_context)

        # Lightweight signature from URL + node count + sampled IDs/text
        head = dom_context[:20]
        tail = dom_context[-8:] if node_count > 20 else []
        sample = head + tail
        sample_tokens: List[str] = []
        for el in sample:
            if not isinstance(el, dict):
                continue
            token = (
                str(el.get("id", "")) or str(el.get("node_id", ""))
            )[:32] or (str(el.get("text", ""))[:24])
            if token:
                sample_tokens.append(token)
        signature_src = f"{request.current_url or ''}|{node_count}|{'|'.join(sample_tokens)}"
        dom_signature = hashlib.md5(signature_src.encode("utf-8")).hexdigest()[:16]

        sess = request.session_id
        prev = throttle.get(sess, {})
        prev_ts = float(prev.get("ts", 0.0) or 0.0)
        prev_count = int(prev.get("count", 0) or 0)
        prev_sig = str(prev.get("sig", "") or "")
        age = now - prev_ts
        delta_ratio = abs(node_count - prev_count) / max(prev_count, 1) if prev_count else 1.0

        # Hard guard for request storms
        if age < hard_min_interval_s:
            logger.info(
                f"🌱 LiveGraph Seed SKIP(hard-throttle) | Session: {sess} | "
                f"nodes={node_count} age_ms={int(age*1000)}"
            )
            return {
                "success": True,
                "session_id": sess,
                "seeded_nodes": prev_count,
                "visible_nodes": 0,
                "duration_ms": int((time.time() - start) * 1000),
                "skipped": True,
                "skip_reason": "hard_throttle",
            }

        # Soft dedupe: repeated same/near-identical seeds inside short window
        if prev_ts and age < min_interval_s:
            if prev_sig == dom_signature or delta_ratio <= max_small_delta_ratio:
                logger.info(
                    f"🌱 LiveGraph Seed SKIP(soft-dedupe) | Session: {sess} | "
                    f"nodes={node_count} prev={prev_count} Δ={delta_ratio:.3f} age_ms={int(age*1000)}"
                )
                return {
                    "success": True,
                    "session_id": sess,
                    "seeded_nodes": prev_count,
                    "visible_nodes": 0,
                    "duration_ms": int((time.time() - start) * 1000),
                    "skipped": True,
                    "skip_reason": "soft_dedupe",
                }

        # Dedupe identical signature for a longer TTL window
        if prev_ts and prev_sig == dom_signature and age < dedupe_ttl_s:
            logger.info(
                f"🌱 LiveGraph Seed SKIP(signature-ttl) | Session: {sess} | "
                f"nodes={node_count} age_ms={int(age*1000)}"
            )
            return {
                "success": True,
                "session_id": sess,
                "seeded_nodes": prev_count,
                "visible_nodes": 0,
                "duration_ms": int((time.time() - start) * 1000),
                "skipped": True,
                "skip_reason": "signature_ttl",
            }

        nodes = [GraphNode.from_dict(el) for el in dom_context if isinstance(el, dict)]
        delta = {
            "delta_type": "full_scan",
            "nodes": [n.to_redis_dict() for n in nodes],
            "url": request.current_url or "",
            "timestamp": time.time(),
        }
        await app.state.live_graph.ingest_delta(request.session_id, delta)
        visible_nodes = await app.state.live_graph.get_visible_nodes(request.session_id)
        duration_ms = int((time.time() - start) * 1000)

        logger.info(
            f"🌱 LiveGraph Seed | Session: {request.session_id} | "
            f"seeded={len(nodes)} visible={len(visible_nodes)} ({duration_ms}ms)"
        )

        throttle[sess] = {"ts": now, "count": len(nodes), "sig": dom_signature}
        # Opportunistic cleanup to keep memory bounded
        if len(throttle) > 512:
            cutoff = now - cleanup_ttl_s
            stale = [k for k, v in throttle.items() if float(v.get("ts", 0.0) or 0.0) < cutoff]
            for k in stale[:256]:
                throttle.pop(k, None)

        return {
            "success": True,
            "session_id": request.session_id,
            "seeded_nodes": len(nodes),
            "visible_nodes": len(visible_nodes),
            "duration_ms": duration_ms,
            "skipped": False,
        }
    except Exception as e:
        logger.error(f"❌ LiveGraph Seed failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/generate_exit")
async def generate_exit(request_data: DynamicExitRequest):
    """Generate a dynamic closing statement for the session."""
    if app.state.rag_engine is None:
        raise HTTPException(status_code=503, detail="RAG engine not initialized")
    
    try:
        exit_speech = await app.state.rag_engine.generate_dynamic_exit(
            request_data.history_context,
            request_data.language
        )
        return {"exit_speech": exit_speech}
    except Exception as e:
        logger.error(f"Exit generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/check_domain_status")
async def check_domain_status(request: CheckDomainRequest):
    """Check if domain is Mapped or Explorer mode."""
    from visual_copilot.api.domain_status import check_domain_status_modular

    return await check_domain_status_modular(
        url=request.url,
        client_id=request.client_id,
        visual_orchestrator=visual_orchestrator,
    )

@app.post("/api/v1/embed", response_model=EmbedResponse)
async def embed_text(request_data: EmbedRequest):
    """Generate embedding for the given text."""
    if app.state.rag_engine is None or app.state.rag_engine.embeddings is None:
        raise HTTPException(status_code=501, detail="Embeddings not initialized")
    
    try:
        # Use embed_query from the configured embeddings provider
        embedding = app.state.rag_engine.embeddings.embed_query(request_data.text)
        return EmbedResponse(embedding=embedding)
    except Exception as e:
        logger.error(f"Embedding error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Skills & Rules Management Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/skills", response_model=SkillRuleItem)
async def create_skill_or_rule(request: SkillRuleCreateRequest):
    """
    Upsert a single agent skill or rule into Qdrant HiveMind.
    Uses Universal Payload Schema.
    """
    if not app.state.rag_engine or not app.state.rag_engine.embeddings:
        raise HTTPException(status_code=503, detail="Embeddings not initialized")
    if not app.state.rag_engine.qdrant or not app.state.rag_engine.qdrant.enabled:
        raise HTTPException(status_code=503, detail="Qdrant HiveMind not available")

    if request.type not in ("agent_skill", "agent_rule"):
        raise HTTPException(status_code=400, detail="type must be 'agent_skill' or 'agent_rule'")

    try:
        import uuid as _uuid
        from qdrant_client.http import models as _models

        # Embed the text
        vector = app.state.rag_engine.embeddings.embed_query(request.text)

        # Build payload via factory
        if request.type == "agent_skill":
            payload = agent_skill_payload(
                text=request.text,
                topic=request.topic,
                tenant_id=request.tenant_id,
            )
        else:
            payload = agent_rule_payload(
                text=request.text,
                topic=request.topic,
                severity=request.severity or "standard",
                tenant_id=request.tenant_id,
            )

        point_id = payload.pop("uuid")
        timestamp = payload["created_at"]

        await app.state.rag_engine.qdrant.client.upsert(
            collection_name=app.state.rag_engine.qdrant.collection_name,
            points=[
                _models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            ],
        )

        label = "Skill" if request.type == "agent_skill" else "Rule"
        logger.info(f"🎯 {label} created: [{request.topic}] {request.text[:60]}...")

        return SkillRuleItem(
            id=point_id,
            text=request.text,
            type=request.type,
            topic=request.topic,
            severity=request.severity,
            timestamp=timestamp,
        )

    except Exception as e:
        logger.error(f"Failed to create skill/rule: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/skills", response_model=SkillRuleListResponse)
async def list_skills_and_rules(tenant_id: str = "tara"):
    """
    List all agent skills and rules stored in Qdrant for a given tenant.
    """
    if not app.state.rag_engine or not app.state.rag_engine.qdrant or not app.state.rag_engine.qdrant.enabled:
        raise HTTPException(status_code=503, detail="Qdrant HiveMind not available")

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as _models

        qdrant = app.state.rag_engine.qdrant
        sync_client = QdrantClient(url=qdrant.url, api_key=qdrant.api_key, check_compatibility=False)

        skills = []
        rules = []

        for item_type in ("agent_skill", "agent_rule"):
            # Match BOTH new schema doc_type AND legacy type field
            result = sync_client.scroll(
                collection_name=qdrant.collection_name,
                scroll_filter=_models.Filter(
                    must=[
                        _models.FieldCondition(key="tenant_id", match=_models.MatchValue(value=tenant_id)),
                    ],
                    should=[
                        _models.FieldCondition(key="type", match=_models.MatchValue(value=item_type)),
                        _models.FieldCondition(
                            key="doc_type",
                            match=_models.MatchValue(value="Agent_Skill" if item_type == "agent_skill" else "Agent_Rule")
                        ),
                    ]
                ),
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
            for p in result[0]:
                item = SkillRuleItem(
                    id=str(p.id),
                    text=read_text(p.payload),
                    type=item_type,
                    topic=p.payload.get("topic", "general"),
                    severity=p.payload.get("severity"),
                    timestamp=read_created_at(p.payload) or p.payload.get("timestamp"),
                )
                if item_type == "agent_skill":
                    skills.append(item)
                else:
                    rules.append(item)

        logger.info(f"🎯 Listed skills/rules: {len(skills)} skills, {len(rules)} rules")
        return SkillRuleListResponse(skills=skills, rules=rules, total=len(skills) + len(rules))

    except Exception as e:
        logger.error(f"Failed to list skills/rules: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/v1/skills/{point_id}")
async def delete_skill_or_rule(point_id: str, tenant_id: str = "tara"):
    """
    Delete a specific agent skill or rule by its Qdrant point ID.
    """
    if not app.state.rag_engine or not app.state.rag_engine.qdrant or not app.state.rag_engine.qdrant.enabled:
        raise HTTPException(status_code=503, detail="Qdrant HiveMind not available")

    try:
        from qdrant_client.http import models as _models

        await app.state.rag_engine.qdrant.client.delete(
            collection_name=app.state.rag_engine.qdrant.collection_name,
            points_selector=_models.PointIdsList(points=[point_id]),
        )

        logger.info(f"🗑️ Deleted skill/rule: {point_id}")
        return {"status": "deleted", "id": point_id}

    except Exception as e:
        logger.error(f"Failed to delete skill/rule: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/upload")
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form("General"),
    topics: str = Form(""),
    tenant_id: str = Form("tara")
):
    """
    Upload and process a document into the Knowledge Base.
    Target: HiveMind (Qdrant)
    """
    if not getattr(app.state, "ingestion_service", None):
        raise HTTPException(status_code=503, detail="Ingestion service not initialized")
    
    try:
        result = await app.state.ingestion_service.ingest_file(
            file=file,
            doc_type=doc_type,
            topics=topics,
            tenant_id=tenant_id
        )
        return result
    except Exception as e:
        logger.error(f"Upload error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/query", response_model=QueryResponse)
async def query_knowledge_base(request_data: QueryRequest, request: Request):
    """
    Process knowledge base query with context-aware retrieval.
    
    If request arrived via /cartesia prefix, skips LLM generation and 
    returns retrieval context only.
    """
    try:
        # Check if we should skip generation (detected by middleware)
        skip_generation = getattr(request.state, "skip_generation", False)
        
        if skip_generation:
            logger.info(f"⚡ Context-only retrieval triggered (via /cartesia)")
            # Execute retrieval only
            context_data = request_data.context or {}
            if request_data.language:
                context_data['language'] = request_data.language
            
            result = await app.state.rag_engine.retrieve_context(
                request_data.query,
                context_data,
                history_context=request_data.history_context
            )
            return result

        # Original query logic proceeds...
        # Generate cache key (include language to prevent cross-language cache pollution)
        lang_suffix = f":{request_data.language}" if request_data.language else ""
        tenant_prefix = f"{request_data.tenant_id or 'demo'}:"
        cache_key = f"rag:{tenant_prefix}{hashlib.md5(request_data.query.encode()).hexdigest()}{lang_suffix}"
        
        # Check Redis cache (only if connected and TTL > 0)
        cached = None
        if app.state.redis and app.state.rag_engine.config.cache_ttl > 0:
            try:
                cached = await app.state.redis.get(cache_key)
            except Exception as cache_read_error:
                logger.warning(f" Cache read failed: {cache_read_error}")
                cached = None
        
        if cached:
            # Cache hit
            app.state.cache_hits += 1
            result = json.loads(cached)
            result['cached'] = True
            
            logger.info(f" CACHE HIT: {request_data.query[:50]}...")
            return QueryResponse(**result)
        
        # Cache miss
        app.state.cache_misses += 1
        
        
        # Ensure context exists and contains language
        context_data = request_data.context or {}
        if request_data.language:
            context_data['language'] = request_data.language
            
        # Process query
        result = await app.state.rag_engine.process_query(
            request_data.query,
            context_data,
            streaming_callback=None,  # Streaming handled separately if needed
            history_context=request_data.history_context,
            tenant_id=request_data.tenant_id,
            force_non_stream=("gpt-oss" in str(getattr(app.state.rag_engine.config, "llm_model", "")).lower()),
            generation_config={
                "max_tokens": 1024,
                "temperature": 0.55,
                "stop": ["</resp>", "</turn>", "</ctxt>"]
            }
        )
        
        # Add cached flag
        result['cached'] = False
        
        # Cache result (only if Redis is available and TTL > 0)
        if app.state.redis and app.state.rag_engine.config.cache_ttl > 0:
            try:
                await app.state.redis.setex(
                    cache_key,
                    app.state.rag_engine.config.cache_ttl,
                    json.dumps({
                        'answer': result['answer'],
                        'sources': result['sources'],
                        'confidence': result['confidence'],
                        'timing_breakdown': result['timing_breakdown'],
                        'metadata': result['metadata']
                    })
                )
            except Exception as cache_error:
                logger.warning(f"️ Cache write failed: {cache_error}")
        
        # Log query
        if app.state.rag_engine.config.log_queries:
            logger.info(
                f" QUERY: {request_data.query[:50]}... → "
                f"{result['confidence']:.2f} confidence, "
                f"{result['timing_breakdown']['total_ms']:.1f}ms"
            )
            logger.info(f" RESPONSE: {result['answer'][:100]}...")
            if 'first_chunk_ms' in result['timing_breakdown']:
                logger.info(f" FIRST CHUNK TIME: {result['timing_breakdown']['first_chunk_ms']:.1f}ms")
        
        return QueryResponse(**result)
    
    except Exception as e:
        logger.error(f" Query error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Query processing failed: {str(e)}")


@app.post("/api/v1/stream_query")
async def stream_query_knowledge_base(request: QueryRequest):
    """
    Stream knowledge base query response.
    Returns a stream of JSON objects: {"text": "...", "is_final": bool}
    """
    async def event_generator():
        # Generate cache key (include history hash AND language to prevent stale/wrong-language responses)
        history_hash = hashlib.md5((request.history_context or "").encode()).hexdigest()[:8]
        lang_suffix = f":{request.language}" if request.language else ""
        tenant_prefix = f"{request.tenant_id or 'demo'}:"
        cache_key = f"rag:{tenant_prefix}{hashlib.md5(request.query.encode()).hexdigest()}:{history_hash}{lang_suffix}"

        # Check Redis cache (only if connected)
        cached = None
        if app.state.redis:
            try:
                cached = await app.state.redis.get(cache_key)
            except Exception as cache_read_error:
                logger.warning(f" Cache read failed: {cache_read_error}")
                cached = None
        
        if cached:
            # Cache hit - simulate streaming
            app.state.cache_hits += 1
            result = json.loads(cached)
            answer = result.get('answer', '')
            
            logger.info(f"✅ CACHE HIT (Streaming): {request.query[:50]}...")
            logger.info(f"📤 Complete cached response:")
            logger.info(f"   {answer}")
            
            # Stream the cached answer in chunks to simulate natural typing/speech
            # CRITICAL FIX: Use larger chunks and slower pacing to prevent TTS flooding
            import re
            
            # Clean markdown from entire answer first
            cleaned_answer = re.sub(r'\*\*([^*]+)\*\*', r'\1', answer)
            cleaned_answer = re.sub(r'\*([^*]+)\*', r'\1', cleaned_answer)
            
            # Split by sentences for more natural chunking (fallback to word-based if no periods)
            sentences = re.split(r'([.!?]\s+)', cleaned_answer)
            chunks = []
            current_chunk = ""
            
            for part in sentences:
                if len(current_chunk) + len(part) > 80:  # Target ~80 chars per chunk
                    if current_chunk.strip():
                        chunks.append(current_chunk.strip())
                    current_chunk = part
                else:
                    current_chunk += part
            
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            
            # If no sentence boundaries found, fall back to word-based chunking
            if not chunks:
                words = cleaned_answer.split()
                current_chunk = ""
                for word in words:
                    if len(current_chunk) + len(word) + 1 > 80:
                        if current_chunk.strip():
                            chunks.append(current_chunk.strip())
                        current_chunk = word
                    else:
                        current_chunk += (" " + word if current_chunk else word)
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
            
            # Stream chunks with proper pacing (50ms delay to prevent TTS flooding)
            for chunk in chunks:
                if chunk.strip():  # Skip empty chunks
                    yield json.dumps({"text": chunk, "is_final": False}) + "\n"
                    # 50ms delay between chunks (prevents detected_unusual_activity in TTS)
                    await asyncio.sleep(0.05)
            
            # Final chunk
            yield json.dumps({"text": "", "is_final": True}) + "\n"
            return

        # Cache miss
        app.state.cache_misses += 1
        
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()
        
        # Container for the full result to cache later
        full_result_container = {}
        accumulated_response = ""  # Track complete response for logging
        
        def callback(text, is_final):
            # Clean text of markdown (e.g., **bold** or *italic*)
            import re
            cleaned_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Remove **bold**
            cleaned_text = re.sub(r'\*([^*]+)\*', r'\1', cleaned_text)  # Remove *italic*
            q.put_nowait((cleaned_text, is_final))
            
        async def run_query():
            try:
                # Build context with language if provided
                query_context = request.context or {}
                
                # Map short language codes to full names
                lang = request.language or "english"
                if lang.lower() in ("en", "eng"):
                    lang = "english"
                elif lang.lower() in ("de", "deu", "ger"):
                    lang = "german"
                
                query_context['language'] = lang
                
                # Process query with streaming callback
                result = await app.state.rag_engine.process_query(
                    request.query,
                    query_context,
                    streaming_callback=callback,
                    history_context=request.history_context,
                    tenant_id=request.tenant_id,
                    force_non_stream=("gpt-oss" in str(getattr(app.state.rag_engine.config, "llm_model", "")).lower()),
                    generation_config={
                        "max_tokens": 1024,
                        "temperature": 0.6,
                        "stop": ["</resp>", "</turn>", "</ctxt>"]
                    }
                )
                
                # Store result for caching
                full_result_container['data'] = result
                
            except Exception as e:
                logger.error(f"Streaming query error: {e}")
                loop.call_soon_threadsafe(q.put_nowait, (f"Error: {str(e)}", True))
            finally:
                await q.put(None) # Sentinel

        # Start query task
        asyncio.create_task(run_query())

        # JITTER BUFFER: Aggregate tokens into sentences for smoother TTS
        buffer = ""
        import re
        
        while True:
            item = await q.get()
            if item is None:
                # Flush remaining buffer at end of stream
                if buffer.strip():
                     accumulated_response += buffer  # Accumulate for logging
                     yield json.dumps({"text": buffer, "is_final": False}) + "\n"
                break
            
            text, is_final = item
            buffer += text
            
            # Flush on sentence boundaries or significant length
            # Check for: . ! ? followed by space or end of string, OR newline
            if re.search(r'[.!?](\s+|$)', buffer) or '\n' in buffer or len(buffer) > 50:
                 # Check if we are splitting a word (simple heuristic: ends with space)
                 # If it doesn't end with space/punctuation and is just long, maybe wait? 
                 # But to be safe for latency, we let >50 chars go if needed, but prefer sentence breaks.
                 
                 # Optimization: If length > 80 force flush, otherwise only flush on punctuation
                 if len(buffer) > 80 or re.search(r'[.!?](\s+|$)', buffer):
                    accumulated_response += buffer
                    yield json.dumps({"text": buffer, "is_final": False}) + "\n"
                    buffer = ""
            
            if is_final:
                # Extract llm_usage from result if available
                llm_usage = {}
                if 'data' in full_result_container:
                    llm_usage = full_result_container['data'].get('llm_usage', {})
                
                # If the final flag is set from the engine, flush everything
                if buffer:
                    accumulated_response += buffer
                    yield json.dumps({"text": buffer, "is_final": True, "llm_usage": llm_usage}) + "\n"
                    buffer = ""
                else:
                    yield json.dumps({"text": "", "is_final": True, "llm_usage": llm_usage}) + "\n"
            
        # Log complete response after streaming finishes
        if accumulated_response:
            logger.info(f"📤 Complete streaming response for query '{request.query[:50]}...':")
            logger.info(f"   {accumulated_response}")
            
        # After streaming is done, cache the result if we have it and TTL > 0
        if 'data' in full_result_container and app.state.redis and app.state.rag_engine.config.cache_ttl > 0:
            try:
                result = full_result_container['data']
                await app.state.redis.setex(
                    cache_key,
                    app.state.rag_engine.config.cache_ttl,
                    json.dumps({
                        'answer': result['answer'],
                        'sources': result['sources'],
                        'confidence': result['confidence'],
                        'timing_breakdown': result['timing_breakdown'],
                        'metadata': result['metadata']
                    })
                )
                logger.info(f" Cached streamed result for: {request.query[:30]}...")
            except Exception as cache_error:
                logger.warning(f"️ Cache write failed: {cache_error}")

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")




@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Service health check.
    
    Returns index status, cache hit rate, Redis/Gemini availability, and uptime.
    """
    try:
        # Calculate cache hit rate
        total_requests = app.state.cache_hits + app.state.cache_misses
        cache_hit_rate = app.state.cache_hits / total_requests if total_requests > 0 else 0.0
        
        # Check Redis health (if available)
        redis_connected = False
        if app.state.redis:
            redis_connected = await ping_redis(app.state.redis)
        
        # Get RAG engine stats
        index_loaded = app.state.rag_engine.vector_store is not None
        index_size = len(app.state.rag_engine.documents)
        gemini_available = app.state.rag_engine.llm is not None
        
        # Get Qdrant status
        qdrant_enabled = False
        qdrant_url = None
        if app.state.rag_engine.qdrant:
            qdrant_enabled = app.state.rag_engine.qdrant.enabled
            qdrant_url = app.state.rag_engine.qdrant.url
        
        # Calculate uptime
        uptime_seconds = time.time() - app.state.start_time
        
        # Determine status
        if not index_loaded:
            status = "unhealthy"
            status_code = 503
        elif not redis_connected:
            status = "degraded"
            status_code = 200
        else:
            status = "healthy"
            status_code = 200
        
        return HealthResponse(
            status=status,
            index_loaded=index_loaded,
            index_size=index_size,
            cache_hit_rate=cache_hit_rate,
            redis_connected=redis_connected,
            gemini_available=gemini_available,
            qdrant_enabled=qdrant_enabled,
            qdrant_url=qdrant_url,
            uptime_seconds=uptime_seconds
        )
    
    except Exception as e:
        logger.error(f"Health check error: {e}")
        raise HTTPException(status_code=503, detail="Service unavailable")


@app.get("/metrics")
async def get_metrics():
    """
    Detailed performance metrics.
    
    Returns RAG engine stats, cache stats, and index stats.
    """
    try:
        # RAG engine stats
        rag_stats = app.state.rag_engine.get_performance_stats()
        
        # Cache stats
        total_requests = app.state.cache_hits + app.state.cache_misses
        cache_stats = {
            'cache_hits': app.state.cache_hits,
            'cache_misses': app.state.cache_misses,
            'cache_hit_rate': app.state.cache_hits / total_requests if total_requests > 0 else 0.0
        }
        
        # Index stats
        index_stats = {
            'total_documents': len(app.state.rag_engine.documents),
            'categories': len(set(m.get('category', '') for m in app.state.rag_engine.doc_metadata)),
            'embedding_dimension': app.state.rag_engine.vector_store.d if app.state.rag_engine.vector_store else 0
        }
        
        # Qdrant (Hive Mind) stats
        qdrant_stats = {
            'enabled': app.state.rag_engine.qdrant.enabled if app.state.rag_engine.qdrant else False,
            'url': app.state.rag_engine.qdrant.url if app.state.rag_engine.qdrant else None,
            'collection': app.state.rag_engine.qdrant.collection_name if app.state.rag_engine.qdrant else None
        }
        
        # Uptime
        uptime_seconds = time.time() - app.state.start_time
        
        return {
            'rag_engine': rag_stats,
            'cache': cache_stats,
            'index': index_stats,
            'qdrant_hive_mind': qdrant_stats,
            'uptime_seconds': uptime_seconds
        }
    
    except Exception as e:
        logger.error(f"Metrics error: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve metrics")


@app.post("/api/v1/admin/rebuild_index", response_model=RebuildIndexResponse)
async def rebuild_index(request: RebuildIndexRequest):
    """
    Rebuild FAISS index from knowledge base (admin endpoint).
    
    Useful for knowledge base updates. Clears cache after rebuild.
    """
    try:
        build_start = time.time()
        
        # Create config (override path if provided)
        config = RAGConfig.from_env()
        if request.knowledge_base_path:
            config.knowledge_base_path = request.knowledge_base_path
        
        # Build index
        builder = IndexBuilder(config)
        success = builder.build_index()
        
        if not success:
            raise HTTPException(status_code=500, detail="Index build failed")
        
        # Reload index in RAG engine
        app.state.rag_engine.load_index()
        
        # Clear cache (only if Redis is available)
        if app.state.redis:
            try:
                # Delete all rag:* keys
                keys = await app.state.redis.keys("rag:*")
                if keys:
                    await app.state.redis.delete(*keys)
                    logger.info(f"️ Cleared {len(keys)} cached queries")
            except Exception as cache_error:
                logger.warning(f"️ Cache clear failed: {cache_error}")
        else:
            logger.info("️ Redis not available - skipping cache clear")
        
        # Get stats
        stats = builder.get_index_stats()
        build_time = time.time() - build_start
        
        logger.info(f" Index rebuilt: {stats['total_documents']} documents in {build_time:.2f}s")
        
        return RebuildIndexResponse(
            status="success",
            documents_indexed=stats['total_documents'],
            categories=stats['categories'],
            build_time_seconds=build_time
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Rebuild error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Rebuild failed: {str(e)}")


@app.post("/api/v1/analyze_session", response_model=AnalyzeSessionResponse)
async def analyze_session(request: AnalyzeSessionRequest):
    """
    DavinciAI Sentiment & Reasoning Pipeline.
    Analyzes post-session logs for business intelligence and Hivemind storage.
    
    If backend_url is provided, the full report (including brief_context) will be sent there.
    """
    try:
        analytics_engine = app.state.session_analytics
        if not analytics_engine:
            raise HTTPException(status_code=503, detail="Session Analytics engine not initialized")

        # Guardrail: do not finalize analytics while a mission is still actively running.
        mission_brain = getattr(app.state, "mission_brain", None)
        if mission_brain:
            try:
                active_mission = await mission_brain._load_session_mission(request.session_id)
            except Exception as mission_lookup_err:
                active_mission = None
                logger.warning(
                    f"analyze_session mission lookup failed for {request.session_id}: {mission_lookup_err}"
                )
            if active_mission and getattr(active_mission, "status", "") == "in_progress":
                phase = str(getattr(active_mission, "phase", "strategy") or "strategy")
                if phase != "done":
                    logger.info(
                        f"📊 ANALYZE_SESSION_DEFERRED: mission still active "
                        f"session={request.session_id} mission={active_mission.mission_id} phase={phase}"
                    )
                    return AnalyzeSessionResponse(
                        status="deferred",
                        report={
                            "session_id": request.session_id,
                            "deferred": True,
                            "reason": "mission_in_progress",
                            "mission_id": active_mission.mission_id,
                            "phase": phase,
                        },
                    )
            
        # 1. Run full analysis (Reasoning + Sentiment + Distillation)
        # Pass brief_context if provided, otherwise it will be auto-generated
        report = await analytics_engine.analyze_session(
            raw_logs=request.history_context, 
            session_id=request.session_id,
            brief_context=request.brief_context
        )
        
        # 2. Extract & Save Collective Knowledge to Hive Mind
        saved_hivemind_chunks = []
        if "distilled_knowledge" in report:
            kb_units = report["distilled_knowledge"]
            if kb_units and app.state.rag_engine and app.state.rag_engine.qdrant and app.state.rag_engine.qdrant.enabled:
                logger.info(f"🧠 Unified Pipeline: Distilling {len(kb_units)} knowledge units into Hivemind...")
                
                for unit in kb_units:
                    issue = unit.get("issue")
                    solution = unit.get("solution")
                    reliability = unit.get("reliability_score", 0.0)
                    
                    # Only save high-reliability knowledge
                    if issue and solution and reliability > 0.6:
                        try:
                            # Generate embedding for the issue
                            vector = app.state.rag_engine.embeddings.embed_query(issue)
                            
                            # Upsert to Qdrant
                            await app.state.rag_engine.qdrant.upsert_case(
                                user_id=request.user_id or "anonymous",
                                issue=issue,
                                solution=solution,
                                vector=vector,
                                tenant_id=request.tenant_id,
                                metadata={
                                    "session_id": request.session_id,
                                    "category": unit.get("category", "general"),
                                    "reliability": reliability,
                                    "extracted_via": "unified_reasoning_pipeline"
                                }
                            )
                            
                            # Real-time Broadcast to Hive Mind Visualizer
                            if hive_mind_connections:
                                # We would ideally project this new vector to 2D here, 
                                # but for simplicity we'll just notify clients to refresh or send the raw node
                                # projecting is expensive, so we just send the notification
                                broadcast_msg = {
                                    "type": "new_knowledge",
                                    "node": {
                                        "issue": issue,
                                        "solution": solution,
                                        "issue_type": unit.get("category", "general"),
                                        "customer_segment": "unknown"
                                    }
                                }
                                asyncio.create_task(broadcast_hive_mind_update(broadcast_msg))

                            saved_hivemind_chunks.append({
                                "issue": issue,
                                "category": unit.get("category", "general")
                            })
                        except Exception as e:
                            logger.error(f"Failed to save unified knowledge node: {e}")
        
        # 3. Enrich report with metadata about saved knowledge
        report["hivemind_updates"] = {
            "chunks_saved": len(saved_hivemind_chunks),
            "summary": saved_hivemind_chunks
        }
        
        # 4. Send report to backend URL if provided
        backend_response = None
        if request.backend_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    payload = {
                        "session_id": request.session_id,
                        "user_id": request.user_id,
                        "tenant_id": request.tenant_id,
                        "timestamp": report["timestamp"],
                        "brief_context": report.get("brief_context", ""),
                        "metrics": report["metrics"],
                        "business_signals": report["business_signals"],
                        "analysis": report["analysis"],
                        "hivemind_updates": report["hivemind_updates"],
                        "metadata": request.metadata or {}
                    }
                    backend_resp = await client.post(request.backend_url, json=payload)
                    backend_response = {
                        "status_code": backend_resp.status_code,
                        "sent": True
                    }
                    logger.info(f"📤 Session report sent to backend: {request.backend_url} (status: {backend_resp.status_code})")
            except Exception as e:
                logger.warning(f"Failed to send report to backend URL {request.backend_url}: {e}")
                backend_response = {
                    "sent": False,
                    "error": str(e)
                }
        
        # 5. Clear Final Report in Logs
        logger.info(f"📊 FINAL SESSION REPORT: {request.session_id}")
        logger.info(f"   ├─ Brief Context: {report.get('brief_context', 'N/A')[:80]}...")
        logger.info(f"   ├─ Sentiment: {report['analysis'].get('overall_sentiment', 'N/A')} ({report['analysis'].get('resolution_status', 'Unknown')})")
        logger.info(f"   ├─ Churn Risk: {report['business_signals'].get('is_churn_risk', 'N/A')} | Priority: {report['business_signals'].get('priority_level', 'NORMAL')}")
        logger.info(f"   ├─ Agent IQ: {report['metrics'].get('agent_iq', 'N/A')} | Velocity: {report['metrics'].get('frustration_velocity', 'STABLE')}")
        logger.info(f"   ├─ Hivemind: {len(saved_hivemind_chunks)} knowledge units learned and archived.")
        if backend_response:
            logger.info(f"   └─ Backend Report: {'✅ Sent' if backend_response.get('sent') else '❌ Failed'}")
        
        if saved_hivemind_chunks:
            for i, chunk in enumerate(saved_hivemind_chunks):
                logger.info(f"      • Learning [{i+1}]: {chunk['issue'][:60]}...")
        
        return AnalyzeSessionResponse(status="success", report=report)
        
    except Exception as e:
        logger.error(f"Session analysis failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")


@app.post("/api/v1/save_case", response_model=SaveCaseResponse)
async def save_case(request: SaveCaseRequest):
    """
    Save a resolved case to Qdrant Hive Mind for collective learning.
    If history_context is provided, distills it into issue/solution first.
    """
    try:
        if not app.state.rag_engine:
            raise HTTPException(status_code=503, detail="RAG engine not initialized")
        
        if not app.state.rag_engine.qdrant or not app.state.rag_engine.qdrant.enabled:
            return SaveCaseResponse(
                status="skipped",
                message="Qdrant not configured - case not saved"
            )

        # Determine the cases to be saved
        effective_cases = []
        
        # 1. Try Distillation if history is provided and manual info is missing
        if request.history_context and (not request.issue or not request.solution):
            logger.info(f"🧠 Distilling history for User {request.user_id[:10]}...")
            distilled_list = await app.state.rag_engine.distill_history_to_case(request.history_context)
            for c in distilled_list:
                i = c.get('issue')
                s = c.get('solution')
                if i and s:
                    effective_cases.append((i, s))
        
        # 2. Fallback to manual issue/solution if distillation didn't yield anything
        if not effective_cases and request.issue and request.solution:
            effective_cases.append((request.issue, request.solution))

        if not effective_cases:
            return SaveCaseResponse(
                status="failed",
                message="Missing issue/solution and distillation failed"
            )
        
        # 3. Save each effective case to Qdrant
        saved_count = 0
        for issue_text, solution_text in effective_cases:
            # Embed the issue for vector storage
            issue_vector = app.state.rag_engine.embeddings.embed_query(issue_text)
            
            # Save to Qdrant
            await app.state.rag_engine.qdrant.upsert_case(
                user_id=request.user_id,
                issue=issue_text,
                solution=solution_text,
                vector=issue_vector,
                tenant_id=request.tenant_id,
                metadata=request.metadata
            )
            
            # Real-time Broadcast to Hive Mind Visualizer
            if hive_mind_connections:
                broadcast_msg = {
                    "type": "new_knowledge",
                    "node": {
                        "text": issue_text,
                        "summary": solution_text,
                        "doc_type": "Case_Memory",
                        # Backward compat
                        "issue": issue_text,
                        "solution": solution_text,
                        "issue_type": "manual_entry",
                        "customer_segment": "unknown"
                    }
                }
                asyncio.create_task(broadcast_hive_mind_update(broadcast_msg))

            saved_count += 1
            logger.info(f"🧠 Saved case to Hive Mind: {issue_text[:50]}...")
        
        return SaveCaseResponse(
            status="success",
            message=f"{saved_count} case(s) yielded and saved to Hive Mind successfully"
        )
    
    except Exception as e:
        logger.error(f"Failed to save case: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save case: {str(e)}")


# ═══════════════════════════════════════════════════════════════════════════════
# Hive Mind Visualization Endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class HiveMindPoint(BaseModel):
    id: str
    x: float
    y: float
    text: str
    summary: str
    doc_type: Optional[str] = None
    domain: Optional[str] = None
    # Backward compat
    issue: Optional[str] = None
    solution: Optional[str] = None
    issue_type: Optional[str] = None
    customer_segment: Optional[str] = None


class HiveMindVisualizationResponse(BaseModel):
    points: List[HiveMindPoint]
    collection_name: str
    total_points: int
    dimension: int
    algorithm: str


@app.get("/api/v1/hive-mind/visualize", response_model=HiveMindVisualizationResponse)
async def visualize_hive_mind(limit: int = 100, algorithm: str = "tsne", tenant_id: str = "tara"):
    """
    Fetch Hive Mind vectors and reduce to 2D for visualization.
    
    Uses t-SNE (default) or PCA to project high-dimensional embeddings to 2D coordinates.
    Returns points with x,y coordinates and payload metadata for interactive visualization.
    
    Args:
        limit: Maximum number of points to return (default 100)
        algorithm: Dimensionality reduction algorithm ('tsne' or 'pca')
    
    Returns:
        List of points with 2D coordinates and metadata
    """
    try:
        if not app.state.rag_engine or not app.state.rag_engine.qdrant:
            raise HTTPException(status_code=503, detail="Hive Mind (Qdrant) not available")
        
        qdrant = app.state.rag_engine.qdrant
        if not qdrant.enabled:
            raise HTTPException(status_code=503, detail="Hive Mind is disabled")
        
        # Fetch vectors with payloads from Qdrant
        from qdrant_client import QdrantClient
        from qdrant_client.http import models
        
        sync_client = QdrantClient(url=qdrant.url, api_key=qdrant.api_key)
        
        # Scroll through collection to get all points (filtered by tenant)
        scroll_result = sync_client.scroll(
            collection_name=qdrant.collection_name,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="tenant_id",
                        match=models.MatchValue(value=tenant_id)
                    )
                ]
            ),
            limit=min(limit, 500),  # Cap at 500 for performance
            with_payload=True,
            with_vectors=True
        )
        
        points_data = scroll_result[0]
        
        if not points_data:
            return HiveMindVisualizationResponse(
                points=[],
                collection_name=qdrant.collection_name,
                total_points=0,
                dimension=qdrant.embedding_dim,
                algorithm=algorithm
            )
        
        # Extract vectors and payloads
        import numpy as np
        vectors = []
        payloads = []
        point_ids = []
        
        for point in points_data:
            if point.vector:
                vectors.append(point.vector)
                payloads.append(point.payload or {})
                point_ids.append(str(point.id))
        
        if len(vectors) < 2:
            # Need at least 2 points for dimensionality reduction
            return HiveMindVisualizationResponse(
                points=[
                    HiveMindPoint(
                        id=point_ids[0] if point_ids else "0",
                        x=0.0,
                        y=0.0,
                        text=read_text(payloads[0]) if payloads else "",
                        summary=read_summary(payloads[0]) if payloads else "",
                        doc_type=read_doc_type(payloads[0]) if payloads else None,
                        domain=payloads[0].get("domain") if payloads else None,
                        issue=payloads[0].get("issue", read_text(payloads[0])) if payloads else None,
                        solution=payloads[0].get("solution", read_summary(payloads[0])) if payloads else None,
                        issue_type=payloads[0].get("issue_type") if payloads else None,
                        customer_segment=payloads[0].get("customer_segment") if payloads else None
                    )
                ] if vectors else [],
                collection_name=qdrant.collection_name,
                total_points=len(vectors),
                dimension=qdrant.embedding_dim,
                algorithm=algorithm
            )
        
        vectors_np = np.array(vectors)
        
        # Dimensionality reduction
        if algorithm.lower() == "pca" or len(vectors) < 3:
            # Use PCA for small datasets (t-SNE needs perplexity < n_samples)
            from sklearn.decomposition import PCA
            reducer = PCA(n_components=min(2, len(vectors)))
            coords_2d = reducer.fit_transform(vectors_np)
            if coords_2d.shape[1] == 1:
                import numpy as np
                coords_2d = np.column_stack([coords_2d, np.zeros(len(coords_2d))])
        else:
            # Default: t-SNE
            from sklearn.manifold import TSNE
            # Perplexity must be < n_samples
            perplexity = min(30, max(2, len(vectors) // 3))
            perplexity = min(perplexity, len(vectors) - 1)
            reducer = TSNE(n_components=2, perplexity=perplexity, random_state=42, max_iter=500)
            coords_2d = reducer.fit_transform(vectors_np)
        
        # Normalize to [-1, 1] range for visualization
        coords_min = coords_2d.min(axis=0)
        coords_max = coords_2d.max(axis=0)
        coords_range = coords_max - coords_min
        coords_range[coords_range == 0] = 1  # Avoid division by zero
        coords_normalized = 2 * (coords_2d - coords_min) / coords_range - 1
        
        # Build response points
        result_points = []
        for i, (x, y) in enumerate(coords_normalized):
            payload = payloads[i]
            result_points.append(HiveMindPoint(
                id=point_ids[i],
                x=float(x),
                y=float(y),
                text=read_text(payload),
                summary=read_summary(payload),
                doc_type=read_doc_type(payload),
                domain=payload.get("domain"),
                issue=payload.get("issue", read_text(payload)),
                solution=payload.get("solution", read_summary(payload)),
                issue_type=payload.get("issue_type"),
                customer_segment=payload.get("customer_segment")
            ))
        
        logger.info(f"🧠 Hive Mind visualization: {len(result_points)} points using {algorithm.upper()}")
        
        return HiveMindVisualizationResponse(
            points=result_points,
            collection_name=qdrant.collection_name,
            total_points=len(result_points),
            dimension=qdrant.embedding_dim,
            algorithm=algorithm
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Hive Mind visualization error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Visualization failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════════════════
# Hive Mind Insights Endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class InsightItem(BaseModel):
    id: str
    text: str
    summary: str
    doc_type: Optional[str] = None
    domain: Optional[str] = None
    # Backward compat
    issue: Optional[str] = None
    solution: Optional[str] = None
    issue_type: Optional[str] = None
    customer_segment: Optional[str] = None
    timestamp: Optional[str] = None


class DomainStat(BaseModel):
    domain: str
    count: int
    percentage: float


class HiveMindInsightsResponse(BaseModel):
    recent_knowledge: List[InsightItem]
    trending_domains: List[DomainStat]
    total_knowledge: int
    unique_domains: int
    customer_segments: Dict[str, int]
    collection_name: str


@app.get("/api/v1/hive-mind/insights", response_model=HiveMindInsightsResponse)
async def get_hive_mind_insights(limit: int = 10):
    """
    Get insights from the Hive Mind collective intelligence.
    
    Returns:
        - Recent knowledge additions
        - Trending/popular domains
        - Customer segment breakdown
        - Total knowledge count
    """
    try:
        if not app.state.rag_engine or not app.state.rag_engine.qdrant:
            raise HTTPException(status_code=503, detail="Hive Mind (Qdrant) not available")
        
        qdrant = app.state.rag_engine.qdrant
        if not qdrant.enabled:
            raise HTTPException(status_code=503, detail="Hive Mind is disabled")
        
        from qdrant_client import QdrantClient
        sync_client = QdrantClient(url=qdrant.url, api_key=qdrant.api_key)
        
        # Fetch all points for analysis (cap at 500)
        scroll_result = sync_client.scroll(
            collection_name=qdrant.collection_name,
            limit=500,
            with_payload=True,
            with_vectors=False  # Don't need vectors for insights
        )
        
        all_points = scroll_result[0]
        
        if not all_points:
            return HiveMindInsightsResponse(
                recent_knowledge=[],
                trending_domains=[],
                total_knowledge=0,
                unique_domains=0,
                customer_segments={},
                collection_name=qdrant.collection_name
            )
        
        # Extract recent knowledge (by ID, higher = newer for our dataset)
        # Sort by ID descending to get recent first
        sorted_points = sorted(all_points, key=lambda p: int(p.id) if str(p.id).isdigit() else 0, reverse=True)
        recent = sorted_points[:limit]
        
        recent_knowledge = [
            InsightItem(
                id=str(p.id),
                text=read_text(p.payload),
                summary=read_summary(p.payload),
                doc_type=read_doc_type(p.payload),
                domain=p.payload.get("domain"),
                issue=p.payload.get("issue", read_text(p.payload)),
                solution=read_summary(p.payload)[:200] + "..." if len(read_summary(p.payload)) > 200 else read_summary(p.payload),
                issue_type=p.payload.get("issue_type"),
                customer_segment=p.payload.get("customer_segment"),
                timestamp=str(read_created_at(p.payload)) if read_created_at(p.payload) else None
            )
            for p in recent
        ]
        
        # Calculate domain statistics
        domain_counts = {}
        segment_counts = {}
        
        for point in all_points:
            payload = point.payload or {}
            domain = payload.get("domain") or read_doc_type(payload)
            segment = payload.get("customer_segment", "unknown")
            
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            segment_counts[segment] = segment_counts.get(segment, 0) + 1
        
        total = len(all_points)
        
        # Sort domains by count (trending)
        trending_domains = [
            DomainStat(
                domain=domain.replace("_", " ").title(),
                count=count,
                percentage=round(count / total * 100, 1)
            )
            for domain, count in sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:8]
        ]
        
        logger.info(f"🧠 Hive Mind insights: {total} nodes, {len(domain_counts)} domains")
        
        return HiveMindInsightsResponse(
            recent_knowledge=recent_knowledge,
            trending_domains=trending_domains,
            total_knowledge=total,
            unique_domains=len(domain_counts),
            customer_segments=segment_counts,
            collection_name=qdrant.collection_name
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Hive Mind insights error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Insights failed: {str(e)}")


async def broadcast_hive_mind_update(message: Dict[str, Any]):
    """Broadcast a Hive Mind update to all connected WebSocket clients."""
    if not hive_mind_connections:
        return
    
    logger.info(f"📢 Broadcasting Hive Mind update to {len(hive_mind_connections)} clients")
    dead_connections = []
    
    for ws in hive_mind_connections:
        try:
            await ws.send_json(message)
        except Exception:
            dead_connections.append(ws)
    
    for dead in dead_connections:
        if dead in hive_mind_connections:
            hive_mind_connections.remove(dead)


@app.websocket("/ws/hive-mind")
async def hive_mind_websocket(websocket: WebSocket):
    """WebSocket for real-time Hive Mind visualization updates."""
    await websocket.accept()
    hive_mind_connections.append(websocket)
    logger.info(f"🔌 Hive Mind Visualizer connected. Total: {len(hive_mind_connections)}")
    
    try:
        while True:
            # Keep connection alive, wait for client messages (if any)
            data = await websocket.receive_text()
            # We don't expect messages from visualizer for now, but we handle them
            logger.debug(f"📩 Received from visualizer: {data}")
    except WebSocketDisconnect:
        if websocket in hive_mind_connections:
            hive_mind_connections.remove(websocket)
        logger.info(f"🔌 Hive Mind Visualizer disconnected. Remaining: {len(hive_mind_connections)}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in hive_mind_connections:
            hive_mind_connections.remove(websocket)


# Static file serving (for index.html demo page)
if __name__ == "__main__":
    import uvicorn
    
    # Docker vs local: single worker to avoid FAISS index duplication
    # Rely on async concurrency for throughput
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8003")),
        workers=1,
        log_level="info"
    )

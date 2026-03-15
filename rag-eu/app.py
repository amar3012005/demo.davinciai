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
import re
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
    agent_skill_payload, agent_rule_payload, general_kb_payload,
    read_text, read_summary, read_doc_type, read_created_at,
    SCHEMA_VERSION,
)
from daytona_agent.services.rag.index_builder import IndexBuilder

# Import rate limiter
from shared.rate_limiter import RateLimitMiddleware, WebSocketRateLimiter

# Import tts_safe for German text post-processing before TTS
from context_architecture import tts_safe

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

def _resolve_effective_tenant_id(
    tenant_id: Optional[str],
    agent_name: Optional[str] = None,
    default: str = "tara",
) -> str:
    """
    Normalize tenant identity so retrieval and hivemind writes use the same namespace.
    Uses the provided tenant_id (slug name like 'bundb') directly if available.
    """
    raw_tenant = (tenant_id or default).strip().lower()
    
    # Backward compatibility mapping for UUIDs if they still appear
    uuid_map = {
        "9d81967f-83d3-4cfc-a1bc-22f8b14ecaa1": "bundb",
        "0dd18031-d35b-4ec9-81d6-cc082420b492": "davinci",
        "5fc3fa72-d15d-48dc-812c-5c845b5172eb": "demo",
        "5f172b23-a407-454e-8981-3c19c2db8fdc": "techz"
    }
    
    if raw_tenant in uuid_map:
        return uuid_map[raw_tenant]

    # Use the slug name directly (e.g. 'bundb')
    effective_tenant = raw_tenant or default
    
    # Fallback to agent name if tenant is generic
    clean_agent = (agent_name or "").strip().lower().replace(" agent", "").strip()
    if (len(raw_tenant) > 20 or raw_tenant == "tenant") and clean_agent and clean_agent != "unknown":
        effective_tenant = clean_agent

    effective_tenant = effective_tenant.split("/")[0]
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in effective_tenant) or default


# Pydantic Models
class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="User question")
    context: Optional[Dict[str, Any]] = Field(None, description="Context from intent service")
    enable_streaming: Optional[bool] = Field(None, description="Enable streaming response")
    history_context: Optional[Union[str, List[Dict[str, Any]]]] = Field(None, description="Conversation history for context-aware responses")
    language: Optional[str] = Field("german", description="Response language: 'english' or 'german'")
    tenant_id: Optional[str] = Field("tara", description="Tenant/Agent identifier for cache isolation")
    session_id: Optional[str] = Field(None, description="Session identifier from orchestrator")
    user_id: Optional[str] = Field(None, description="User identifier")
    agent_name: Optional[str] = Field(None, description="Agent name for session config")
    # Interruption handling fields (barge-in support)
    interrupted_text: Optional[str] = Field(None, description="The assistant's response text that was interrupted by user speech")
    interruption_transcripts: Optional[List[str]] = Field(None, description="User's interruption transcripts collected during interruption")
    interruption_type: Optional[str] = Field(None, description="Type of interruption: 'addon', 'topic_change', 'clarification', or 'noise'")


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
    language: Optional[str] = Field("german", description="Response language")
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

class MapHintsRequest(BaseModel):
    goal: str = Field(..., description="The user's mission goal")
    client_id: Optional[str] = Field("tara", description="Client/Tenant ID")
    current_url: Optional[str] = Field("", description="Current page URL for domain filtering")

class FastSenseRequest(BaseModel):
    goal: str = Field(..., description="The user's mission goal")
    dom_context: List[Dict[str, Any]] = Field(..., description="List of visible DOM elements")
    session_id: str = Field(..., description="Session identifier")


class EmbedRequest(BaseModel):
    text: str = Field(..., description="Text to embed")

class EmbedResponse(BaseModel):
    embedding: List[float]

class DynamicExitRequest(BaseModel):
    history_context: str = Field(..., description="Conversation history for context")
    language: Optional[str] = Field("german", description="Response language")


# ═══════════════════════════════════════════════════════════════════════════════
# FSM Routing Models (Schema-Driven Appointment Booking)
# ═══════════════════════════════════════════════════════════════════════════════

class FSMSchemaField(BaseModel):
    """Schema definition for a single FSM field"""
    required: bool = True
    collect_prompt: str = ""
    confirm_prompt: str = ""
    validation_regex: Optional[str] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None


class FSMSchema(BaseModel):
    """Complete FSM appointment schema"""
    fields: Dict[str, FSMSchemaField] = Field(default_factory=dict)
    cancel_keywords: List[str] = Field(default_factory=list)
    max_retries: int = 3
    fallback_messages: Dict[str, str] = Field(default_factory=dict)
    resume_prompt_template: str = "Back to booking, {pending_field_prompt}"


class FSMContext(BaseModel):
    """Current FSM state passed from Orchestrator"""
    active: bool = False
    pending_field: Optional[str] = None  # e.g., "name", "email", "topic"
    collected_data: Dict[str, Any] = Field(default_factory=dict)
    retry_counts: Dict[str, int] = Field(default_factory=dict)
    schema: Optional[FSMSchema] = None


class FSMRouteRequest(BaseModel):
    """Request model for FSM routing endpoint"""
    user_text: str = Field(..., min_length=1, description="User input text")
    session_id: str = Field(..., description="Session identifier")
    tenant_id: str = Field("tara", description="Tenant identifier")
    language: str = Field("german", description="Response language")
    fsm_context: FSMContext = Field(..., description="Current FSM state")
    history_context: Optional[Union[str, List[Dict[str, Any]]]] = Field(None, description="Conversation history")


class FSMRouteResponse(BaseModel):
    """Response model for FSM routing endpoint"""
    action: str = Field(..., description="Routing action: collect_field, confirm_field, detour_rag, cancel, invalid_retry")
    field: Optional[str] = Field(None, description="Field being collected/confirmed")
    normalized_value: Optional[str] = Field(None, description="Normalized field value if applicable")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Classification confidence")
    reason: str = Field(..., description="Reason for the routing decision")
    resume_prompt: Optional[str] = Field(None, description="FSM pending question to ask after detour")
    cancelled: bool = Field(False, description="Whether FSM should be cancelled")


# ── Agent Skills & Rules Models ──────────────────────────────────────────────

class SkillRuleCreateRequest(BaseModel):
    text: str = Field(..., min_length=5, description="Skill or rule description text")
    type: str = Field(..., description="'agent_skill', 'agent_rule', or 'general_kb'")
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
session_banner_logged: set[str] = set()


def _strip_reasoning_artifacts(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<think>.*?</think>", " ", str(text), flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"</?think>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


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
                logger.info(f" ✅ Web search ENABLED (API key: {config.google_search_api_key[:2]}..., CSE ID: {config.google_cse_id})")
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


        # Initialize Ingestion Service (optional; can be disabled for lightweight local runs)
        enable_ingestion_service = os.getenv("ENABLE_INGESTION_SERVICE", "true").strip().lower() == "true"
        if enable_ingestion_service:
            try:
                from core.processing.ingestion import IngestionService
                global ingestion_service
                # SHARE EMBEDDINGS to avoid double loading memory spike
                shared_embeddings = rag_engine.embeddings if rag_engine else None
                ingestion_service = IngestionService(
                    embeddings=shared_embeddings,
                    qdrant_backend=(rag_engine.qdrant if rag_engine else None),
                )
                app.state.ingestion_service = ingestion_service
                logger.info(" ✅ Ingestion Service initialized (with shared embeddings)")
            except Exception as e:
                logger.error(f" ❌ Failed to initialize Ingestion Service: {e}")
        else:
            logger.info("ℹ️ Ingestion Service disabled (ENABLE_INGESTION_SERVICE=false)")
        
        # If index not loaded, try to build it (only if local retrieval is enabled)
        # Local index logic removed - using Qdrant Hivemind exclusively
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
        # ULTIMATE MODULES INITIALIZATION (OPTIONAL)
        # ═══════════════════════════════════════════════════════════
        if os.getenv("USE_ULTIMATE_TARA", "true").strip().lower() == "true":
            logger.info("=" * 70)
            logger.info("🚀 Initializing ULTIMATE TARA Architecture")
            logger.info("=" * 70)

            # Import Ultimate modules lazily so local runs can skip heavy imports
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
        else:
            app.state.mind_reader = None
            app.state.hive_interface = None
            app.state.live_graph = None
            app.state.semantic_detective = None
            app.state.mission_brain = None
            logger.info("ℹ️ ULTIMATE TARA modules disabled (USE_ULTIMATE_TARA=false)")

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

# Global WebSocket rate limiter - 15 connections per IP
ws_rate_limiter = WebSocketRateLimiter(
    max_connections_per_ip=15,
    max_messages_per_minute=120
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "https://demo.davinciai.eu,https://enterprise.davinciai.eu").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# Rate limiting middleware - 150 requests/minute per IP
app.add_middleware(
    RateLimitMiddleware,
    redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
    default_requests=150,
    default_window=60,
    exempt_paths=["/health", "/metrics", "/", "/version", "/api/v1/health"]
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
            action_history=request.action_history or []
        )
        
        if result and result.get("success"):
            logger.info(f"✅ Ultimate TARA success: {result.get('action', {}).get('type')} on {result.get('action', {}).get('target_id', 'none')}")
            return result
        
        # ═══════════════════════════════════════════════════════════
        # Fallback to legacy — but INJECT hive context so the LLM
        # doesn't hallucinate navigation steps
        # ═══════════════════════════════════════════════════════════
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
            # Fast intent parsing with Mind Reader
            schema = await app.state.mind_reader.translate(
                user_input=request.goal,
                current_url=""
            )
            
            # Simple speech generation based on intent
            speech = f"Looking for {schema.target_entity}..."
            
            return {
                "speech": speech,
                "relevant_ids": [],
                "intent": schema.action.value,
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
    Fetch GPS navigation hints from Qdrant for a given goal.
    Called ONCE at mission start, then cached by orchestrator.
    """
    if visual_orchestrator is None:
        return {"hints": ""}
    
    logger.info(f"🗺️ Map Hints Request | Goal: '{request.goal}' | Client: {request.client_id}")
    
    try:
        hints = await visual_orchestrator.get_navigation_hints(
            goal=request.goal,
            client_id=request.client_id,
            current_url=request.current_url or ""
        )
        return {"hints": hints}
    except Exception as e:
        logger.warning(f"Map hints fetch failed: {e}")
        return {"hints": ""}

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
    if visual_orchestrator is None:
        return {"mode": "explorer", "reason": "Visual Orchestrator not ready"}
    
    try:
        status = await visual_orchestrator.check_hivemind_status(
            current_url=request.url,
            client_id=request.client_id
        )
        return status
    except Exception as e:
        logger.error(f"Domain status check failed: {e}")
        return {"mode": "explorer", "reason": str(e)}

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
    Upsert a single agent skill, rule, or general knowledge item into Qdrant HiveMind.
    Uses Universal Payload Schema.
    """
    if not app.state.rag_engine or not app.state.rag_engine.embeddings:
        raise HTTPException(status_code=503, detail="Embeddings not initialized")
    if not app.state.rag_engine.qdrant or not app.state.rag_engine.qdrant.enabled:
        raise HTTPException(status_code=503, detail="Qdrant HiveMind not available")

    if request.type not in ("agent_skill", "agent_rule", "general_kb"):
        raise HTTPException(status_code=400, detail="type must be 'agent_skill', 'agent_rule', or 'general_kb'")

    try:
        from qdrant_client.http import models as _models

        # Embed the text
        vector = app.state.rag_engine.embeddings.embed_query(request.text)
        sync_client, _, collection_name = app.state.rag_engine.qdrant._get_clients_for_tenant(request.tenant_id)  # pylint: disable=protected-access
        if not sync_client or not collection_name:
            raise HTTPException(status_code=503, detail="Tenant Qdrant collection not available")
        app.state.rag_engine.qdrant._ensure_collection_for(sync_client, collection_name)  # pylint: disable=protected-access
        vector_name = app.state.rag_engine.qdrant._get_vector_name_for_collection(sync_client, collection_name)  # pylint: disable=protected-access

        # Build payload via factory
        if request.type == "agent_skill":
            payload = agent_skill_payload(
                text=request.text,
                topic=request.topic,
                tenant_id=request.tenant_id,
            )
        elif request.type == "agent_rule":
            payload = agent_rule_payload(
                text=request.text,
                topic=request.topic,
                severity=request.severity or "standard",
                tenant_id=request.tenant_id,
            )
        else:
            payload = general_kb_payload(
                text=request.text,
                tenant_id=request.tenant_id,
                doc_type_detail=request.topic or "General",
                topics=request.topic or "general",
            )

        point_id = payload.pop("uuid")
        timestamp = payload["created_at"]

        point_payload = _models.PointStruct(
            id=point_id,
            vector={vector_name: vector} if vector_name else vector,
            payload=payload,
        )
        try:
            sync_client.upsert(
                collection_name=collection_name,
                points=[point_payload],
            )
        except Exception as upsert_error:
            error_text = str(upsert_error)
            if "Not existing vector name error" not in error_text:
                raise

            cache_key = f"{id(sync_client)}:{collection_name}"
            app.state.rag_engine.qdrant._vector_name_cache.pop(cache_key, None)  # pylint: disable=protected-access
            refreshed_vector_name = app.state.rag_engine.qdrant._get_vector_name_for_collection(sync_client, collection_name)  # pylint: disable=protected-access

            logger.warning(
                "Vector name mismatch for collection %s (tenant=%s, vector_name=%s, refreshed=%s). Retrying alternate vector format.",
                collection_name,
                request.tenant_id,
                vector_name,
                refreshed_vector_name,
            )

            if refreshed_vector_name and refreshed_vector_name != vector_name:
                retry_vector = {refreshed_vector_name: vector}
            elif vector_name:
                retry_vector = vector
            elif refreshed_vector_name:
                retry_vector = {refreshed_vector_name: vector}
            else:
                raise

            fallback_point = _models.PointStruct(
                id=point_id,
                vector=retry_vector,
                payload=payload,
            )
            sync_client.upsert(
                collection_name=collection_name,
                points=[fallback_point],
            )

        label = "Skill" if request.type == "agent_skill" else "Rule" if request.type == "agent_rule" else "Knowledge"
        logger.info(f"🎯 {label} created in {collection_name}: [{request.topic}] {request.text[:60]}...")

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
        from qdrant_client.http import models as _models

        qdrant = app.state.rag_engine.qdrant
        sync_client, _, collection_name = qdrant._get_clients_for_tenant(tenant_id)  # pylint: disable=protected-access
        if not sync_client or not collection_name:
            raise HTTPException(status_code=503, detail="Tenant Qdrant collection not available")

        skills = []
        rules = []

        for item_type in ("agent_skill", "agent_rule"):
            # Match BOTH new schema doc_type AND legacy type field
            result = sync_client.scroll(
                collection_name=collection_name,
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

        logger.info(f"🎯 Listed skills/rules from {collection_name}: {len(skills)} skills, {len(rules)} rules")
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

        sync_client, _, collection_name = app.state.rag_engine.qdrant._get_clients_for_tenant(tenant_id)  # pylint: disable=protected-access
        if not sync_client or not collection_name:
            raise HTTPException(status_code=503, detail="Tenant Qdrant collection not available")

        sync_client.delete(
            collection_name=collection_name,
            points_selector=_models.PointIdsList(points=[point_id]),
        )

        logger.info(f"🗑️ Deleted skill/rule from {collection_name}: {point_id}")
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
        effective_tenant_id = _resolve_effective_tenant_id(tenant_id, default="tara")
        result = await app.state.ingestion_service.ingest_file(
            file=file,
            doc_type=doc_type,
            topics=topics,
            tenant_id=effective_tenant_id
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
        effective_agent_name = (request_data.agent_name or "unknown").strip()
        effective_tenant_id = _resolve_effective_tenant_id(
            request_data.tenant_id,
            effective_agent_name,
            default="tara",
        )

        # Generate cache key with tenant + org + context to avoid cross-brand cache bleed.
        lang_suffix = f":{request_data.language}" if request_data.language else ""
        tenant_prefix = f"{effective_tenant_id}:"
        org_slug = str(getattr(app.state.rag_engine.config, "organization_name", "org")).strip().lower()
        context_hash = hashlib.md5(
            json.dumps(request_data.context or {}, sort_keys=True, default=str).encode()
        ).hexdigest()[:8]
        query_hash = hashlib.md5(request_data.query.encode()).hexdigest()
        cache_key = f"rag:{tenant_prefix}{org_slug}:{query_hash}:{context_hash}{lang_suffix}"
        
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
        is_hivemind_dashboard = context_data.get("surface") == "hivemind_dashboard"
        result = await app.state.rag_engine.process_query(
            request_data.query,
            context_data,
            streaming_callback=None,  # Streaming handled separately if needed
            history_context=request_data.history_context,
            tenant_id=effective_tenant_id,
            force_non_stream=(not is_hivemind_dashboard) and ("gpt-oss" in str(getattr(app.state.rag_engine.config, "llm_model", "")).lower()),
            generation_config={
                "max_tokens": 400,
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
        effective_session_id = (request.session_id or "unknown").strip()
        effective_agent_name = (request.agent_name or ((request.context or {}).get("agent_name")) or "unknown").strip()
        effective_tenant_id = _resolve_effective_tenant_id(
            request.tenant_id,
            effective_agent_name,
            default="tara",
        )

        # Emit one banner per session at first stream query (session handshake into RAG path)
        if effective_session_id not in session_banner_logged:
            session_banner_logged.add(effective_session_id)
            qdrant_cfg = {"url": None, "api_key": None, "collection_name": None}
            qdrant_default_url = None
            qdrant_default_collection = None
            qdrant_override = False
            if app.state.rag_engine and app.state.rag_engine.qdrant:
                qdrant_default_url = app.state.rag_engine.qdrant.url
                qdrant_default_collection = app.state.rag_engine.qdrant.collection_name
                try:
                    qdrant_cfg = app.state.rag_engine.qdrant._resolve_tenant_qdrant(effective_tenant_id)  # pylint: disable=protected-access
                    qdrant_override = (
                        (qdrant_cfg.get("url") or "") != (qdrant_default_url or "")
                        or (qdrant_cfg.get("collection_name") or "") != (qdrant_default_collection or "")
                    )
                except Exception:
                    pass

            masked_api_key = "unset"
            raw_key = qdrant_cfg.get("api_key")
            if raw_key:
                masked_api_key = f"{raw_key[:2]}***{raw_key[-2:]}" if len(raw_key) >= 8 else "***"

            logger.info("============================================================")
            logger.info("============================================================")
            logger.info(
                f"SESSION START (STREAM) | TENANT_ID={effective_tenant_id} | "
                f"SESSION_ID={effective_session_id} | AGENT={effective_agent_name}"
            )
            logger.info("============================================================")
            logger.info("SESSION CONFIG")
            logger.info(f"  - tenant_id: {effective_tenant_id}")
            logger.info(f"  - session_id: {effective_session_id}")
            logger.info(f"  - agent_name: {effective_agent_name}")
            logger.info(f"  - language: {request.language or 'english'}")
            logger.info(f"  - user_id: {request.user_id or 'anonymous'}")
            logger.info(f"  - qdrant_default_url: {qdrant_default_url or 'unset'}")
            logger.info(f"  - qdrant_default_collection: {qdrant_default_collection or 'unset'}")
            logger.info(f"  - qdrant_effective_url: {qdrant_cfg.get('url') or 'unset'}")
            logger.info(f"  - qdrant_effective_collection: {qdrant_cfg.get('collection_name') or 'unset'}")
            logger.info(f"  - qdrant_api_key: {masked_api_key}")
            logger.info(f"  - qdrant_override_applied: {qdrant_override}")
            logger.info("============================================================")
            logger.info("============================================================")

        # Generate cache key with tenant + org + context/history + language.
        history_hash = hashlib.md5(
            json.dumps(request.history_context or "", sort_keys=True, default=str).encode()
        ).hexdigest()[:8]
        context_hash = hashlib.md5(
            json.dumps(request.context or {}, sort_keys=True, default=str).encode()
        ).hexdigest()[:8]
        lang_suffix = f":{request.language}" if request.language else ""
        tenant_prefix = f"{effective_tenant_id}:"
        org_slug = str(getattr(app.state.rag_engine.config, "organization_name", "org")).strip().lower()
        query_hash = hashlib.md5(request.query.encode()).hexdigest()
        cache_key = f"rag:{tenant_prefix}{org_slug}:{query_hash}:{history_hash}:{context_hash}{lang_suffix}"

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
            # Apply German TTS post-processing (umlauts, numbers, loanwords)
            cleaned_answer = tts_safe(cleaned_answer)
            
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
            # Apply German TTS post-processing (umlauts, numbers, loanwords)
            cleaned_text = tts_safe(cleaned_text)
            q.put_nowait((cleaned_text, is_final))
            
        async def run_query():
            try:
                # Build context with language if provided
                query_context = request.context or {}
                
                # Map short language codes to full names
                lang = request.language or "german"
                if lang.lower() in ("en", "eng"):
                    lang = "english"
                elif lang.lower() in ("de", "deu", "ger"):
                    lang = "german"
                
                query_context['language'] = lang
                is_hivemind_dashboard = query_context.get("surface") == "hivemind_dashboard"
                
                # Process query with streaming callback
                result = await app.state.rag_engine.process_query(
                    request.query,
                    query_context,
                    streaming_callback=callback,
                    history_context=request.history_context,
                    tenant_id=effective_tenant_id,
                    force_non_stream=(not is_hivemind_dashboard) and ("gpt-oss" in str(getattr(app.state.rag_engine.config, "llm_model", "")).lower()),
                    generation_config={
                        "max_tokens": 400,
                        "temperature": 0.6,
                        "stop": ["</resp>", "</turn>", "</ctxt>"]
                    },
                    interrupted_text=request.interrupted_text,
                    interruption_transcripts=request.interruption_transcripts,
                    interruption_type=request.interruption_type,
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
        index_loaded = True # Deprecated local index, always true
        index_size = 0 # Deprecated local index size
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
        if not redis_connected:
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
        
        # Index stats - Deprecated, using Qdrant
        index_stats = {
            'total_documents': 0,
            'categories': 0,
            'embedding_dimension': 384
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
    
    Deprecated: Local FAISS indexing is removed in favor of Qdrant Hivemind.
    """
    raise HTTPException(status_code=501, detail="Local indexing disabled. Please use Hivemind ingestion APIs instead.")


@app.post("/api/v1/analyze_session", response_model=AnalyzeSessionResponse)
async def analyze_session(request: AnalyzeSessionRequest):
    """
    DavinciAI Sentiment & Reasoning Pipeline.
    Analyzes post-session logs for business intelligence and Hivemind storage.
    
    If backend_url is provided, the full report (including brief_context) will be sent there.
    """
    try:
        effective_tenant_id = _resolve_effective_tenant_id(
            request.tenant_id,
            ((request.metadata or {}).get("agent_name") or "unknown"),
            default="tara",
        )
        effective_agent_id = ((request.metadata or {}).get("agent_id") or "unknown")
        effective_session_type = ((request.metadata or {}).get("session_type") or "unknown")
        logger.info("============================================================")
        logger.info("============================================================")
        
        # Resolve target collection for diagnostics
        target_collection = "unknown"
        if app.state.rag_engine and app.state.rag_engine.qdrant:
             target_collection = app.state.rag_engine.qdrant._resolve_collection_name(effective_tenant_id)
             
        logger.info(
            f"SESSION START (ANALYZE) | TENANT_ID={effective_tenant_id} | "
            f"TARGET_COLLECTION={target_collection} | "
            f"SESSION_ID={request.session_id} | AGENT={((request.metadata or {}).get('agent_name') or 'unknown')}"
        )
        logger.info("============================================================")
        logger.info("============================================================")
        analytics_engine = app.state.session_analytics
        if not analytics_engine:
            raise HTTPException(status_code=503, detail="Session Analytics engine not initialized")
            
        # 1. Run full analysis (Reasoning + Sentiment + Distillation)
        # Pass brief_context if provided, otherwise it will be auto-generated
        report = await analytics_engine.analyze_session(
            raw_logs=request.history_context, 
            session_id=request.session_id,
            brief_context=request.brief_context
        )
        
        # 2. Extract & Save Collective Knowledge to Hive Mind
        saved_hivemind_chunks = []
        hivemind_candidates = 0
        hivemind_skipped = 0
        if "distilled_knowledge" in report:
            kb_units = report["distilled_knowledge"]
            hivemind_candidates = len(kb_units or [])
            if kb_units and app.state.rag_engine and app.state.rag_engine.qdrant and app.state.rag_engine.qdrant.enabled:
                logger.info(f"🧠 Unified Pipeline: Distilling {len(kb_units)} knowledge units into Hivemind...")
                
                for unit in kb_units:
                    issue = unit.get("issue")
                    solution = unit.get("solution")
                    reliability = unit.get("reliability_score", 0.0)
                    
                    # Save all extractive knowledge as requested (low threshold)
                    if issue and solution and reliability >= 0.0:
                        try:
                            # Generate embedding for the issue
                            vector = app.state.rag_engine.embeddings.embed_query(issue)
                            
                            # Upsert to Qdrant
                            await app.state.rag_engine.qdrant.upsert_case(
                                user_id=request.user_id or "anonymous",
                                issue=issue,
                                solution=solution,
                                vector=vector,
                                tenant_id=effective_tenant_id,
                                metadata={
                                    "session_id": request.session_id,
                                    "agent_id": effective_agent_id,
                                    "session_type": effective_session_type,
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
                    else:
                        hivemind_skipped += 1
        
        # 3. Enrich report with metadata about saved knowledge
        report["hivemind_updates"] = {
            "chunks_saved": len(saved_hivemind_chunks),
            "chunks_candidates": hivemind_candidates,
            "chunks_skipped": hivemind_skipped,
            "summary": saved_hivemind_chunks
        }
        report["session_metadata"] = {
            "session_id": request.session_id,
            "tenant_id": effective_tenant_id,  # Use the slug name consistently
            "session_type": effective_session_type,
            "user_id": request.user_id or "anonymous",
            "agent_name": (request.metadata or {}).get("agent_name"),
            "agent_id": (request.metadata or {}).get("agent_id"),
            "source": (request.metadata or {}).get("source"),
            "report": (request.metadata or {}).get("report"),
        }
        
        # 4. Send report to backend URL if provided
        backend_response = None
        if request.backend_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    payload = {
                        "session_id": request.session_id,
                        "user_id": request.user_id,
                        "tenant_id": effective_tenant_id, # Use the slug name consistently
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
        clean_brief = _strip_reasoning_artifacts(report.get('brief_context', 'N/A'))
        report["brief_context"] = clean_brief
        logger.info(f"📊 FINAL SESSION REPORT: {request.session_id}")
        logger.info(f"   ├─ Brief Context: {clean_brief[:120]}...")
        logger.info(f"   ├─ Sentiment (heuristic): {report['analysis'].get('overall_sentiment', 'N/A')} ({report['analysis'].get('resolution_status', 'Unknown')})")
        logger.info(f"   ├─ Churn Risk: {report['business_signals'].get('is_churn_risk', 'N/A')} | Priority: {report['business_signals'].get('priority_level', 'NORMAL')}")
        logger.info(f"   ├─ Agent IQ (deterministic): {report['metrics'].get('agent_iq', 'N/A')} | Velocity (deterministic): {report['metrics'].get('frustration_velocity', 'STABLE')}")
        logger.info(
            f"   ├─ Hivemind: saved={len(saved_hivemind_chunks)} | "
            f"candidates={hivemind_candidates} | skipped={hivemind_skipped}"
        )
        if backend_response:
            logger.info(f"   └─ Backend Report: {'✅ Sent' if backend_response.get('sent') else '❌ Failed'}")
        
        if saved_hivemind_chunks:
            for i, chunk in enumerate(saved_hivemind_chunks):
                logger.info(f"      • Learning [{i+1}]: {chunk['issue'][:60]}...")
        logger.info("============================================================")
        logger.info("============================================================")
        logger.info(
            f"SESSION END (ANALYZE) | TENANT_ID={effective_tenant_id} | "
            f"SESSION_ID={request.session_id} | HIVEMIND_SAVED={len(saved_hivemind_chunks)}"
        )
        logger.info("============================================================")
        logger.info("============================================================")
        session_banner_logged.discard(request.session_id)
        
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
        effective_tenant_id = _resolve_effective_tenant_id(
            request.tenant_id,
            ((request.metadata or {}).get("agent_name") or "unknown"),
            default="tara",
        )
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
                tenant_id=effective_tenant_id,
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
        sync_client, _, collection_name = qdrant._get_clients_for_tenant(tenant_id)  # pylint: disable=protected-access
        if sync_client is None:
            # Fallback to default collection if tenant is not configured
            sync_client = QdrantClient(url=qdrant.url, api_key=qdrant.api_key)
            collection_name = qdrant.collection_name
        
        # Scroll through collection to get all points (filtered by tenant)
        scroll_result = sync_client.scroll(
            collection_name=collection_name,
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
                collection_name=collection_name,
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
                # Named vectors support: point.vector can be dict{name: list}
                vec = point.vector
                if isinstance(vec, dict):
                    # For hybrid collections, first vector is usually dense, second is sparse
                    # We want the dense one (usually first in the dict or named 'dense' or 'vector')
                    if "dense" in vec:
                        vec = vec["dense"]
                    elif "vector" in vec:
                        vec = vec["vector"]
                    else:
                        vec = next(iter(vec.values())) if vec else None
                
                if vec is None:
                    continue
                
                # Robust Dimension Filtering:
                # Ensure all vectors in the visualization set have identical dimensions
                if not vectors:
                    # First valid vector sets the primary dimension
                    vectors.append(vec)
                    payloads.append(point.payload or {})
                    point_ids.append(str(point.id))
                elif len(vec) == len(vectors[0]):
                    # Only keep vectors that match the primary dimension
                    vectors.append(vec)
                    payloads.append(point.payload or {})
                    point_ids.append(str(point.id))
                else:
                    logger.warning(
                        f"Skipping point {point.id} in visualization: "
                        f"dimension mismatch ({len(vec)} vs {len(vectors[0])})"
                    )
        
        if len(vectors) < 2:
            # t-SNE / PCA need at least 2 points to calculate relationships
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
                collection_name=collection_name,
                total_points=len(vectors),
                dimension=len(vectors[0]) if vectors else qdrant.embedding_dim,
                algorithm=algorithm
            )
        
        vectors_np = np.array(vectors)
        
        # Dimensionality reduction
        used_algorithm = algorithm.lower()
        try:
            if used_algorithm == "pca" or len(vectors) < 3:
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
        except ModuleNotFoundError:
            # Graceful fallback when sklearn is unavailable
            logger.warning("sklearn not installed; falling back to first-2-dim projection for Hive Mind visualize")
            import numpy as np
            if vectors_np.shape[1] >= 2:
                coords_2d = vectors_np[:, :2]
            else:
                coords_2d = np.column_stack([vectors_np[:, 0], np.zeros(len(vectors_np))])
            used_algorithm = "fallback"
        
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
        
        logger.info(
            f"🧠 Hive Mind visualization: {len(result_points)} points using {used_algorithm.upper()} "
            f"(tenant={tenant_id}, collection={collection_name})"
        )
        
        return HiveMindVisualizationResponse(
            points=result_points,
            collection_name=collection_name,
            total_points=len(result_points),
            dimension=qdrant.embedding_dim,
            algorithm=used_algorithm
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
async def get_hive_mind_insights(limit: int = 10, tenant_id: str = "tara"):
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
        from qdrant_client.http import models as _models
        sync_client, _, collection_name = qdrant._get_clients_for_tenant(tenant_id)  # pylint: disable=protected-access
        if sync_client is None:
            # Fallback to default collection if tenant is not configured
            sync_client = QdrantClient(url=qdrant.url, api_key=qdrant.api_key)
            collection_name = qdrant.collection_name
        
        # Fetch all points for analysis (cap at 500)
        scroll_result = sync_client.scroll(
            collection_name=collection_name,
            scroll_filter=_models.Filter(
                must=[
                    _models.FieldCondition(
                        key="tenant_id",
                        match=_models.MatchValue(value=tenant_id)
                    )
                ]
            ),
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
                collection_name=collection_name
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
        
        logger.info(f"🧠 Hive Mind insights: {total} nodes, {len(domain_counts)} domains (tenant={tenant_id}, collection={collection_name})")
        
        return HiveMindInsightsResponse(
            recent_knowledge=recent_knowledge,
            trending_domains=trending_domains,
            total_knowledge=total,
            unique_domains=len(domain_counts),
            customer_segments=segment_counts,
            collection_name=collection_name
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Hive Mind insights error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Insights failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════════════════
# FSM Routing Endpoint (Schema-Driven Appointment Booking)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/fsm/route", response_model=FSMRouteResponse)
async def fsm_route(request: FSMRouteRequest):
    """
    FSM Routing Endpoint for Schema-Driven Appointment Booking.
    
    Decides per turn whether to:
    1. continue slot collection (collect_field/confirm_field)
    2. answer a general RAG question (detour_rag)
    3. resume FSM after detour
    4. cancel the appointment flow
    
    Returns:
        FSMRouteResponse with action, field, normalized_value, confidence, reason, resume_prompt
    """
    try:
        if not app.state.rag_engine:
            raise HTTPException(status_code=503, detail="RAG engine not initialized")
        
        logger.info(f"🔀 FSM Route | Session: {request.session_id} | Pending: {request.fsm_context.pending_field} | Text: '{request.user_text[:50]}...'")

        # Convert Pydantic models to dicts for engine
        fsm_context_dict = request.fsm_context.model_dump() if hasattr(request.fsm_context, 'model_dump') else {
            'active': request.fsm_context.active,
            'pending_field': request.fsm_context.pending_field,
            'collected_data': request.fsm_context.collected_data,
            'retry_counts': request.fsm_context.retry_counts,
            'schema': request.fsm_context.schema
        }

        # Call the routing logic in rag_engine
        result = await app.state.rag_engine.route_fsm_turn(
            user_text=request.user_text,
            session_id=request.session_id,
            tenant_id=request.tenant_id,
            language=request.language,
            fsm_context=fsm_context_dict,
            history_context=request.history_context
        )
        
        logger.info(f"✅ FSM Route Result | Action: {result['action']} | Confidence: {result['confidence']:.2f} | Reason: {result['reason']}")
        
        return FSMRouteResponse(**result)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FSM routing error: {e}", exc_info=True)
        # Fallback to invalid_retry on error
        return FSMRouteResponse(
            action="invalid_retry",
            confidence=0.0,
            reason=f"Routing error: {str(e)}",
            cancelled=False
        )


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
    # Rate limit: Check connection limit per IP
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not ws_rate_limiter.can_connect(client_ip):
        await websocket.close(code=1013, reason="Connection limit exceeded")
        logger.warning(f"HiveMind connection rejected for {client_ip}: too many connections")
        return

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
        logger.error(f"📠 WebSocket error: {e}")
        if websocket in hive_mind_connections:
            hive_mind_connections.remove(websocket)
    finally:
        # Rate limit: Decrement connection count
        ws_rate_limiter.disconnect(client_ip)


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

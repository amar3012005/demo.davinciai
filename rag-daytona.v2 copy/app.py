"""
Visual Copilot Microservice — FastAPI Application

Standalone microservice for TARA's Visual Co-Pilot feature.
Handles DOM-grounded mission planning, visual page analysis,
and real-time action routing via the Ultimate TARA pipeline.

This service connects to the Orchestrator via HTTP REST and
receives DOM snapshots, screenshots, and user goals.
"""

import os
import sys
import time
import logging
import json
import hashlib
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List

import redis.asyncio as redis
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from shared.redis_client import get_redis_client, close_redis_client, ping_redis

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Pydantic Request/Response Models
# ═══════════════════════════════════════════════════════════════════════════════

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
    conversation_history: Optional[str] = Field("", description="Recent conversation context")
    last_dom_context: Optional[List[Dict[str, Any]]] = Field(None, description="Pre-action DOM snapshot")
    fast_sense_speech: Optional[str] = Field(None, description="Speech already spoken by fast_sense")
    interaction_mode: str = Field("interactive", description="Interaction mode (turbo or interactive)")
    active_states: Optional[Dict[str, Any]] = Field(None, description="Active nav/tab states from widget")
    data_tables: Optional[List[Dict[str, Any]]] = Field(None, description="Extracted table data from widget")
    page_title: Optional[str] = Field("", description="Document title from widget")
    screenshot_b64: Optional[str] = Field(None, description="Base64 JPEG screenshot of current page")
    pre_decision: Optional[Dict[str, Any]] = Field(None, description="Pre-route decision payload from get_map_hints")
    route_hint: Optional[str] = Field("", description="Pre-route route hint from get_map_hints")


class MapHintsRequest(BaseModel):
    goal: str = Field(..., description="The user's mission goal")
    client_id: Optional[str] = Field("tara", description="Client/Tenant ID")
    current_url: Optional[str] = Field("", description="Current page URL for domain filtering")
    session_id: Optional[str] = Field("", description="Session ID for screenshot cache fallback")
    dom_context: Optional[List[Dict[str, Any]]] = Field(None, description="Optional DOM elements to pre-seed LiveGraph")
    screenshot_b64: Optional[str] = Field(None, description="Optional base64 JPEG screenshot for vision pre-routing")


class FastSenseRequest(BaseModel):
    goal: str = Field(..., description="The user's mission goal")
    dom_context: List[Dict[str, Any]] = Field(..., description="List of visible DOM elements")
    session_id: str = Field(..., description="Session identifier")


class LiveGraphSeedRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    dom_context: List[Dict[str, Any]] = Field(..., description="Current visible DOM elements")
    current_url: Optional[str] = Field("", description="Current page URL")
    page_title: Optional[str] = Field("", description="Page title")


class PushScreenshotRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    screenshot_b64: str = Field(..., description="Base64-encoded JPEG screenshot")
    source: Optional[str] = Field("orchestrator", description="Who pushed this screenshot")


class AnalysePageRequest(BaseModel):
    session_id: str = Field(..., description="Orchestra session ID")
    analysis_mode: str = Field("dom", description="'dom' or 'vision'")
    current_url: str = Field("", description="Current page URL")
    page_title: str = Field("", description="Page title")
    screenshot_b64: Optional[str] = Field(None, description="Base64 JPEG for vision mode")
    dom_context: Optional[List[Dict[str, Any]]] = Field(None, description="DOM elements for dom mode")
    user_question: Optional[str] = Field(None, description="User's specific question about the page")


class CheckDomainRequest(BaseModel):
    url: str = Field(..., description="Current page URL")
    client_id: str = Field("tara", description="Client/Tenant ID")


class AnalyzeSessionRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    history_context: List[Dict[str, Any]] = Field(..., description="Raw conversation logs")
    user_id: Optional[str] = Field(None, description="User identifier")
    tenant_id: str = Field("tara", description="Tenant identifier")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional context")
    brief_context: Optional[str] = Field(None, description="Brief summary of what happened in the session")
    backend_url: Optional[str] = Field(None, description="Optional backend URL to send the session report to")


class AnalyzeSessionResponse(BaseModel):
    status: str
    report: Dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════════
# Global State
# ═══════════════════════════════════════════════════════════════════════════════

redis_client: Optional[redis.Redis] = None
app_start_time = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Lifespan: Initialize Visual Copilot modules directly
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup/shutdown."""
    global redis_client, app_start_time

    logger.info("🚀 Starting Visual Copilot Microservice...")
    app_start_time = time.time()

    try:
        # ── Redis ────────────────────────────────────────────────────────────
        try:
            redis_client = await asyncio.wait_for(get_redis_client(), timeout=15.0)
            await asyncio.wait_for(ping_redis(redis_client), timeout=5.0)
            logger.info("✅ Redis connected successfully")
        except asyncio.TimeoutError:
            logger.warning("⚠️ Redis connection timeout — service will run in degraded mode")
            redis_client = None
        except Exception as redis_error:
            logger.warning(f"⚠️ Redis connection failed: {redis_error} — caching disabled")
            redis_client = None

        app.state.redis = redis_client

        # ── LLM Provider (Groq) ─────────────────────────────────────────────
        llm_provider = None
        try:
            from llm_providers.groq_provider import GroqProvider
            groq_api_key = os.getenv("GROQ_API_KEY")
            if groq_api_key:
                llm_provider = GroqProvider(api_key=groq_api_key)
                logger.info("✅ GroqProvider initialized")
            else:
                logger.warning("⚠️ GROQ_API_KEY not set — Mind Reader will use fallback")
        except Exception as e:
            logger.warning(f"⚠️ GroqProvider init failed: {e}")
        app.state.llm_provider = llm_provider

        # ── Embeddings (for Semantic Detective hybrid scoring) ───────────────
        embeddings = None
        try:
            from sentence_transformers import SentenceTransformer
            embeddings = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("✅ SentenceTransformer embeddings loaded (all-MiniLM-L6-v2)")
        except Exception as e:
            logger.warning(f"⚠️ SentenceTransformer not available, keyword fallback: {e}")
        app.state.embeddings = embeddings

        # ── Qdrant (Hive Mind) ──────────────────────────────────────────────
        qdrant = None
        try:
            from qdrant_addon import QdrantAddon
            qdrant = QdrantAddon(
                embedding_dim=384,
                url=os.getenv("QDRANT_URL"),
                api_key=os.getenv("QDRANT_API_KEY"),
                collection_name=os.getenv("QDRANT_COLLECTION", "tara_hive"),
            )
            if qdrant.enabled:
                logger.info(f"✅ Qdrant Hive Mind connected: {qdrant.url}")
            else:
                logger.warning("⚠️ Qdrant Hive Mind NOT AVAILABLE")
                qdrant = None
        except Exception as e:
            logger.warning(f"⚠️ Qdrant init failed: {e}")
        app.state.qdrant = qdrant

        # ── Session Analytics ────────────────────────────────────────────────
        try:
            from session_analytics import SessionAnalytics
            analytics_model = os.getenv("ANALYTICS_MODEL", "llama-3.1-8b-instant")
            app.state.session_analytics = SessionAnalytics(
                llm_provider=llm_provider,
                model_name=analytics_model,
            )
            logger.info(f"✅ Session Analytics initialized (model: {analytics_model})")
        except Exception as e:
            logger.warning(f"⚠️ Session Analytics init failed: {e}")
            app.state.session_analytics = None

        # ── Visual Orchestrator (legacy fallback) ────────────────────────────
        try:
            from visual_orchestrator import VisualOrchestrator
            app.state.visual_orchestrator = VisualOrchestrator(
                llm_provider,
                qdrant_client=qdrant,
                embeddings=embeddings,
                redis_client=redis_client,
            )
            logger.info("✅ Visual Orchestrator initialized (legacy fallback)")
        except Exception as e:
            logger.warning(f"⚠️ Visual Orchestrator init failed: {e}")
            app.state.visual_orchestrator = None

        # ═════════════════════════════════════════════════════════════════════
        # ULTIMATE TARA MODULES INITIALIZATION
        # ═════════════════════════════════════════════════════════════════════
        logger.info("=" * 70)
        logger.info("🚀 Initializing ULTIMATE TARA Architecture")
        logger.info("=" * 70)

        from tara_models import TacticalSchema, ActionIntent
        from mind_reader import MindReader
        from hive_interface import HiveInterface
        from live_graph import LiveGraph
        from semantic_detective import SemanticDetective
        from mission_brain import MissionBrain

        # Mind Reader
        try:
            app.state.mind_reader = MindReader(llm_provider)
            logger.info("✅ Mind Reader initialized")
        except Exception as e:
            logger.warning(f"⚠️ Mind Reader init failed: {e}")
            app.state.mind_reader = None

        # Hive Interface
        try:
            app.state.hive_interface = HiveInterface(
                qdrant_client=qdrant.client if qdrant else None,
                embeddings=embeddings,
                redis_client=redis_client,
                collection_name=os.getenv("QDRANT_COLLECTION", "tara_hive"),
            )
            logger.info("✅ Hive Interface initialized")
        except Exception as e:
            logger.warning(f"⚠️ Hive Interface init failed: {e}")
            app.state.hive_interface = None

        # Live Graph
        try:
            app.state.live_graph = LiveGraph(redis_client)
            logger.info("✅ Live Graph initialized (Redis DOM mirror)")
        except Exception as e:
            logger.warning(f"⚠️ Live Graph init failed: {e}")
            app.state.live_graph = None

        # Semantic Detective
        try:
            app.state.semantic_detective = SemanticDetective(
                live_graph=app.state.live_graph,
                embeddings=embeddings,
            )
            logger.info("✅ Semantic Detective initialized (hybrid scoring)")
        except Exception as e:
            logger.warning(f"⚠️ Semantic Detective init failed: {e}")
            app.state.semantic_detective = None

        # Mission Brain
        try:
            app.state.mission_brain = MissionBrain(
                redis_client=redis_client,
                hive_interface=app.state.hive_interface,
            )
            logger.info("✅ Mission Brain initialized (constraint enforcement)")
        except Exception as e:
            logger.warning(f"⚠️ Mission Brain init failed: {e}")
            app.state.mission_brain = None

        logger.info("=" * 70)
        logger.info("✅ ULTIMATE TARA Architecture Ready")
        logger.info("=" * 70)
        logger.info("🟢 Visual Copilot Microservice ready")

        yield

        # Shutdown
        logger.info("🔴 Shutting down Visual Copilot Microservice...")
        if redis_client:
            await close_redis_client()
            logger.info("Redis connection closed")
        logger.info("Visual Copilot shutdown complete")

    except Exception as e:
        logger.error(f"Startup error: {e}", exc_info=True)
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════════════════════

root_path = os.getenv("VC_ROOT_PATH", "")

app = FastAPI(
    title="Visual Copilot Microservice",
    description="TARA's Visual Co-Pilot — DOM-grounded mission planning and action routing.",
    version="1.0.0",
    lifespan=lifespan,
    root_path=root_path,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Middleware to strip /rag or /cartesia prefix if present (for backward compat)
@app.middleware("http")
async def strip_path_prefix(request: Request, call_next):
    path = request.url.path
    logger.info(f"🔍 Incoming request path: {path}")
    prefix = None
    if path.startswith("/rag"):
        prefix = "/rag"
    elif path.startswith("/cartesia"):
        prefix = "/cartesia"
    if prefix:
        new_path = path[len(prefix):] or "/"
        logger.info(f"✂️ Stripping prefix '{prefix}': {path} -> {new_path}")
        request.scope["path"] = new_path
        if "raw_path" in request.scope:
            request.scope["raw_path"] = new_path.encode()
    else:
        logger.info(f"➡️ No prefix matched (checked /rag, /cartesia)")
    response = await call_next(request)
    return response


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Service health check."""
    return {
        "status": "healthy",
        "service": "visual-copilot",
        "uptime_seconds": round(time.time() - app_start_time, 1),
        "redis_connected": redis_client is not None,
        "modules": {
            "mind_reader": app.state.mind_reader is not None,
            "hive_interface": app.state.hive_interface is not None,
            "live_graph": app.state.live_graph is not None,
            "semantic_detective": app.state.semantic_detective is not None,
            "mission_brain": app.state.mission_brain is not None,
        },
    }


# ── Analyse Page ─────────────────────────────────────────────────────────────

@app.post("/api/v1/analyse_page")
async def analyse_page_endpoint(request: AnalysePageRequest):
    """
    🔍 Analyse Page — one-shot page narration via Visual Co-pilot.
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


# ── Plan Next Step (Ultimate TARA Pipeline) ──────────────────────────────────

@app.post("/api/v1/plan_next_step")
async def plan_next_step(request: PlanStepRequest):
    """
    ULTIMATE TARA PIPELINE — Primary planning endpoint.
    """
    logger.info(f"🚀 Ultimate TARA Plan | Session: {request.session_id} | Goal: '{request.goal}' | Step: {request.step_number}")

    # Cache screenshot if provided
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
        app.state.live_graph,
    ]):
        logger.warning("⚠️ Ultimate TARA not fully available, falling back to legacy")
        return await _legacy_plan_next_step(request)

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
                "timestamp": time.time(),
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
                    for step in reversed(action_obj):
                        if isinstance(step, dict):
                            action_type = (step.get("type") or "none") or "none"
                            action_target = step.get("target_id", "none") or "none"
                            break
            logger.info(
                f"✅ Ultimate TARA {'success' if result.get('success') else 'failure'}: "
                f"{action_type} on {action_target}"
            )
            return result

        # Fallback to legacy with hive context
        if isinstance(result, dict) and result.get("no_legacy_fallback"):
            return result
        logger.warning("⚠️ Ultimate TARA returned no result, falling back to legacy")
        return await _legacy_plan_next_step(request)

    except Exception as e:
        logger.error(f"Ultimate TARA error: {e}", exc_info=True)
        return await _legacy_plan_next_step(request)


async def _legacy_plan_next_step(request: PlanStepRequest):
    """Legacy planning endpoint (fallback)."""
    vo = getattr(app.state, "visual_orchestrator", None)
    if vo is None:
        raise HTTPException(status_code=501, detail="Visual Orchestrator not initialized")

    logger.info(f"🎯 [LEGACY] Planner Request | Session: {request.session_id} | Goal: '{request.goal}'")
    try:
        result = await vo.plan_next_step(
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


# ── Fast Sense ───────────────────────────────────────────────────────────────

@app.post("/api/v1/fast_sense")
async def fast_sense(request: FastSenseRequest):
    """Fast Sense: Quick DOM scan for immediate TTS response."""
    logger.info(f"⚡ Fast Sense | Session: {request.session_id} | Goal: '{request.goal}'")

    if app.state.mind_reader:
        try:
            goal_lower = request.goal.lower()
            prefixes_to_strip = [
                "i want to ", "can you ", "please ", "show me ",
                "find ", "click ", "go to ", "navigate to ", "what is ",
            ]
            extracted_entity = request.goal
            for prefix in prefixes_to_strip:
                if goal_lower.startswith(prefix):
                    extracted_entity = request.goal[len(prefix):]
                    break
            return {
                "speech": f"Looking for {extracted_entity}...",
                "relevant_ids": [],
                "intent": "navigation",
                "pipeline": "ultimate_fast",
            }
        except Exception as e:
            logger.warning(f"⚠️ Fast sense failed: {e}")

    vo = getattr(app.state, "visual_orchestrator", None)
    if vo is None:
        return {"speech": "", "relevant_ids": []}
    try:
        result = await vo._run_fast_sense(goal=request.goal, dom_context=request.dom_context)
        return result
    except Exception as e:
        logger.error(f"Fast sense error: {e}", exc_info=True)
        return {"speech": "", "relevant_ids": []}


# ── Get Map Hints ────────────────────────────────────────────────────────────

@app.post("/api/v1/get_map_hints")
async def get_map_hints(request: MapHintsRequest):
    """Fetch modular Hive hints for a given goal."""
    from visual_copilot.api.map_hints import build_map_hints

    screenshot_b64 = request.screenshot_b64
    if not screenshot_b64 and request.session_id:
        cache = getattr(app.state, "latest_screenshots", {}) or {}
        screenshot_b64 = cache.get(request.session_id)

    # Optional inline LiveGraph seed
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
            logger.info(f"🗺️ Map Hints pre-seed | Session: {request.session_id} | nodes={len(seed_nodes)}")
        except Exception as seed_err:
            logger.warning(f"Map Hints pre-seed skipped: {seed_err}")

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


# ── Push Screenshot ──────────────────────────────────────────────────────────

@app.post("/api/v1/push_screenshot")
async def push_screenshot(request: PushScreenshotRequest):
    """Cache a screenshot pushed by the Orchestrator."""
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


# ── LiveGraph Seed ───────────────────────────────────────────────────────────

@app.post("/api/v1/livegraph_seed")
async def livegraph_seed(request: LiveGraphSeedRequest):
    """Seed LiveGraph early so pre-route has DOM ready."""
    if not getattr(app.state, "live_graph", None):
        raise HTTPException(status_code=503, detail="LiveGraph not initialized")

    start = time.time()
    try:
        from tara_models import GraphNode

        # Seed throttling
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

        # Lightweight signature
        head = dom_context[:20]
        tail = dom_context[-8:] if node_count > 20 else []
        sample = head + tail
        sample_tokens: List[str] = []
        for el in sample:
            if not isinstance(el, dict):
                continue
            token = (str(el.get("id", "")) or str(el.get("node_id", "")))[:32] or (str(el.get("text", ""))[:24])
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

        skip_response = lambda reason: {
            "success": True, "session_id": sess, "seeded_nodes": prev_count,
            "visible_nodes": 0, "duration_ms": int((time.time() - start) * 1000),
            "skipped": True, "skip_reason": reason,
        }

        if age < hard_min_interval_s:
            logger.info(f"🌱 LiveGraph Seed SKIP(hard-throttle) | Session: {sess} | nodes={node_count}")
            return skip_response("hard_throttle")

        if prev_ts and age < min_interval_s:
            if prev_sig == dom_signature or delta_ratio <= max_small_delta_ratio:
                logger.info(f"🌱 LiveGraph Seed SKIP(soft-dedupe) | Session: {sess} | nodes={node_count}")
                return skip_response("soft_dedupe")

        if prev_ts and prev_sig == dom_signature and age < dedupe_ttl_s:
            logger.info(f"🌱 LiveGraph Seed SKIP(signature-ttl) | Session: {sess} | nodes={node_count}")
            return skip_response("signature_ttl")

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

        logger.info(f"🌱 LiveGraph Seed | Session: {request.session_id} | seeded={len(nodes)} visible={len(visible_nodes)} ({duration_ms}ms)")

        throttle[sess] = {"ts": now, "count": len(nodes), "sig": dom_signature}
        if len(throttle) > 512:
            cutoff = now - cleanup_ttl_s
            stale = [k for k, v in throttle.items() if float(v.get("ts", 0.0) or 0.0) < cutoff]
            for k in stale[:256]:
                throttle.pop(k, None)

        return {
            "success": True, "session_id": request.session_id,
            "seeded_nodes": len(nodes), "visible_nodes": len(visible_nodes),
            "duration_ms": duration_ms, "skipped": False,
        }
    except Exception as e:
        logger.error(f"❌ LiveGraph Seed failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Check Domain Status ──────────────────────────────────────────────────────

@app.post("/api/v1/check_domain_status")
async def check_domain_status(request: CheckDomainRequest):
    """Check if domain is Mapped or Explorer mode."""
    from visual_copilot.api.domain_status import check_domain_status_modular
    return await check_domain_status_modular(
        url=request.url,
        client_id=request.client_id,
        visual_orchestrator=getattr(app.state, "visual_orchestrator", None),
    )


# ── Analyze Session ──────────────────────────────────────────────────────────

@app.post("/api/v1/analyze_session", response_model=AnalyzeSessionResponse)
async def analyze_session(request: AnalyzeSessionRequest):
    """Post-session analysis for business intelligence."""
    try:
        analytics_engine = getattr(app.state, "session_analytics", None)
        if not analytics_engine:
            raise HTTPException(status_code=503, detail="Session Analytics engine not initialized")

        # Guardrail: defer if mission still active
        mission_brain = getattr(app.state, "mission_brain", None)
        if mission_brain:
            try:
                active_mission = await mission_brain._load_session_mission(request.session_id)
            except Exception:
                active_mission = None
            if active_mission and getattr(active_mission, "status", "") == "in_progress":
                phase = str(getattr(active_mission, "phase", "strategy") or "strategy")
                if phase != "done":
                    logger.info(f"📊 ANALYZE_SESSION_DEFERRED: mission still active session={request.session_id}")
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

        report = await analytics_engine.analyze_session(
            raw_logs=request.history_context,
            session_id=request.session_id,
            brief_context=request.brief_context,
        )

        # Send report to backend URL if provided
        if request.backend_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    payload = {
                        "session_id": request.session_id,
                        "user_id": request.user_id,
                        "tenant_id": request.tenant_id,
                        "timestamp": report.get("timestamp"),
                        "brief_context": report.get("brief_context", ""),
                        "metrics": report.get("metrics", {}),
                        "business_signals": report.get("business_signals", {}),
                        "analysis": report.get("analysis", {}),
                        "metadata": request.metadata or {},
                    }
                    await client.post(request.backend_url, json=payload)
                    logger.info(f"📤 Session report sent to backend: {request.backend_url}")
            except Exception as e:
                logger.warning(f"Failed to send report to backend: {e}")

        return AnalyzeSessionResponse(status="success", report=report)

    except Exception as e:
        logger.error(f"Session analysis failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")


# ── Web Crawler ────────────────────────────────────────────────────────────────
# Shared caching for crawler and extract steps
_CRAWL_CACHE = {}

@app.post("/api/v1/crawl-website")
async def api_crawl_website(request: dict):
    from visual_copilot.api.crawler import crawl_website, CrawlRequest
    req = CrawlRequest(**request)
    result = await crawl_website(req)
    
    # Store text content in cache for extraction step
    for page in result.get("pages", []):
        url = page["url"]
        text = page.pop("text", "")
        if text:
            _CRAWL_CACHE[url] = text
            
    return result

@app.post("/api/v1/extract-pages")
async def api_extract_pages(request: dict):
    from visual_copilot.api.crawler import extract_pages, ExtractRequest
    req = ExtractRequest(**request)
    
    llm_provider = getattr(app.state, "mind_reader", None)
    if llm_provider and hasattr(llm_provider, 'llm_client'):
        provider = llm_provider.llm_client
    else:
        # fallback to GroqProvider
        from llm_providers.groq_provider import GroqProvider
        provider = GroqProvider(model_name="openai/gpt-oss-120b")
        
    result = await extract_pages(req, _CRAWL_CACHE, provider)
    return result

@app.post("/api/v1/save-readme-to-hivemind")
async def api_save_readme(request: dict):
    from visual_copilot.api.crawler import SaveReadmeRequest
    req = SaveReadmeRequest(**request)
    qdrant = getattr(app.state, "qdrant", None)
    
    if not qdrant:
        return {"status": "error", "message": "Qdrant not initialized"}
        
    try:
        # Just save it as a big document blob in case memory
        await qdrant.upsert_case(
            tenant_id=req.tenant_id,
            case_data={"readme": req.readme_content, "domain": req.domain},
            analysis=req.readme_content[:1000],
            tags=["domain_readme", req.domain]
        )
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Save readme failed: {e}")
        return {"status": "error", "message": str(e)}

# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "4005")),
        workers=1,
        log_level="info",
    )

"""
Unified WebSocket Handler for Orchestra-daytona

Single bidirectional WebSocket connection handles:
- Audio input (microphone chunks from browser)
- Audio output (TTS streaming to browser)
- State synchronization
- Interrupt handling (barge-in)
- Multi-language support (English & German)
"""

import os
import asyncio
import json
import logging
import time
from copy import deepcopy
from datetime import datetime
import base64
import uuid
import secrets
import wave
import numpy as np
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import httpx

import aiohttp
from fastapi import WebSocket, WebSocketDisconnect

from core.state_manager import StateManager, State
from core.service_client import STTClient, TTSClient
from core.pipeline import ProcessingPipeline
from core.history_manager import HistoryManager
from dialogue.manager import MultiLangDialogueManager, DialogueType
from utils.lang_detect import detect_language
from fsm.appointment_fsm import SimpleAppointmentFSM, DEFAULT_V1_SCHEMA
from config_loader import OrchestratorConfig

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorSession:
    """Single unified session for a WebSocket connection"""
    session_id: str
    websocket: WebSocket
    state_manager: StateManager
    user_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    # Mode configuration
    stt_mode: str = "audio"  # "audio" or "text"
    tts_mode: str = "audio"  # "audio" or "text"

    # Conversation history for context-aware responses
    history_manager: HistoryManager = field(default_factory=lambda: HistoryManager(max_turns=10))

    # Service clients
    stt_client: Optional[STTClient] = None
    tts_client: Optional[TTSClient] = None

    # Audio state
    tts_task: Optional[asyncio.Task] = None
    filler_task: Optional[asyncio.Task] = None
    pipeline_task: Optional[asyncio.Task] = None

    # Metrics
    audio_chunks_received: int = 0
    audio_chunks_sent: int = 0
    last_activity: float = field(default_factory=time.time)

    # Timeout tracking
    timeout_count: int = 0
    current_language: str = "en"

    # Visual Co-Pilot states
    mode: str = "voice" # "voice" or "visual-copilot"
    dom_context: List[Dict[str, Any]] = field(default_factory=list)
    last_dom_context: List[Dict[str, Any]] = field(default_factory=list)  # Pre-action snapshot for validation
    is_executing_action: bool = False
    
    # Mission Agent State (Visual Co-Pilot Only)
    current_goal: Optional[str] = None
    is_mission_active: bool = False
    goal_step_count: int = 0
    max_mission_steps: int = 10
    last_dom_hash: Optional[str] = None
    stagnation_count: int = 0
    max_stagnation: int = 3
    current_url: str = ""  # Current page URL for GPS hints
    last_action: str = ""  # Summary of last action taken
    action_history: List[str] = field(default_factory=list)  # Last N actions for loop detection
    dom_history: List[List[Dict[str, Any]]] = field(default_factory=list)  # Last 3 DOM snapshots for context diffing
    map_hints: str = ""  # Cached Qdrant GPS hints (fetched once at Step 0)
    speaker_muted: bool = False  # Track if user has muted the speaker (for turbo mode)
    interaction_mode: str = "interactive"  # "interactive" or "turbo" - set per session

    # Dedicated audio WebSocket (for /stream endpoint)
    audio_websocket: Optional[WebSocket] = None
    audio_ws_connected: bool = False
    audio_send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Language override (for dynamic language switching)
    stream_out_override: Optional[str] = None

    # Connection state tracking
    is_closed: bool = False

    # Audio frame buffer for alignment
    fg_buffer: bytearray = field(default_factory=bytearray)
    
    # STT final transcript accumulation (for combining multiple FINAL segments)
    accumulated_final_text: str = ""
    final_accumulation_timer: Optional[asyncio.Task] = None
    final_accumulation_timeout: float = 0.05  # 50ms window to accumulate FINAL segments
    
    # Early transition tracking (for low-latency speech_end handling)
    pending_transcript: bool = False  # True when waiting for final transcript after speech_end
    partial_transcript: str = ""  # Store partial transcripts for early processing
    transcript_timeout_task: Optional[asyncio.Task] = None  # Timeout task for transcript arrival
    
    # Audio playback tracking (for accurate playback_done handling)
    audio_playback_start_time: Optional[float] = None
    audio_playback_duration: Optional[float] = None  # Expected duration in seconds
    audio_playback_predicted_end: Optional[float] = None  # Predicted end timestamp (from client)
    audio_playback_server_timer: Optional[asyncio.Task] = None  # Server-side timer task
    audio_playback_turn_id: int = 0  # Monotonic playback id for stale-event rejection
    
    # Filler coordination
    immediate_filler_played: bool = False  # Track if immediate filler was played to prevent double filling

    # TTS configuration (synced from TTS service)
    tts_sample_rate: int = 44100
    tts_format: str = "pcm_f32le"

    # Task limiting
    task_semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(10))  # Max 10 concurrent tasks
    
    # Background service tasks
    stt_receive_task: Optional[asyncio.Task] = None
    tts_receive_task: Optional[asyncio.Task] = None
    
    # Performance metrics and history
    turn_metrics: List[Dict[str, Any]] = field(default_factory=list)
    current_turn_timers: Dict[str, float] = field(default_factory=dict)
    
    # Cumulative LLM + TTS metrics (across all turns)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    llm_model: str = ""
    tts_streamed_chars: int = 0
    tts_total_time_ms: float = 0.0
    
    # Communication queues for background tasks
    tts_audio_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    
    # Connection status events
    tts_connected_event: asyncio.Event = field(default_factory=asyncio.Event)
    stt_connected_event: asyncio.Event = field(default_factory=asyncio.Event)
    
    # Concurrency control for WebSocket sends
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    
    # FSM Appointment booking
    appointment_fsm: Optional[SimpleAppointmentFSM] = None  # Active FSM instance
    fsm_active: bool = False  # True when in FSM mode

    metadata: Dict[str, Any] = field(default_factory=dict)
    map_status_checked: bool = False

    # Barge-in validation (deferred VAD interrupt)
    last_query_text: str = ""
    last_query_language: str = "en"
    barge_in_pending: bool = False  # True = VAD fired during SPEAKING, awaiting STT validation

    # Host-based configuration overrides
    agent_name: str = "agent"
    agent_id: Optional[str] = "agent"
    tenant_id: Optional[str] = "tenant"
    secondary_language: Optional[str] = None
    flow_config: Dict[str, Any] = field(default_factory=dict)
    rag_url: Optional[str] = None
    voice_id_override: Optional[str] = None

    # Connection health tracking
    last_pong_time: Optional[float] = None  # Last time browser responded to ping
    
    # Audio chunk counter (continuous across _enqueue_foreground_audio calls)
    _audio_chunk_counter: int = 0

    def is_connected(self) -> bool:
        """Check if the WebSocket is still connected and open."""
        if self.is_closed:
            return False
        try:
            # FastAPI WebSocket has client_state attribute
            return self.websocket.client_state.name == "CONNECTED"
        except Exception:
            return not self.is_closed

    def verify_state(self, expected_state: State, operation: str) -> bool:
        """Verify session is in expected state before operation"""
        if self.is_closed:
            logger.debug(f"[{self.session_id}] Session closed, skipping {operation}")
            return False
        if self.state_manager.state != expected_state:
            logger.warning(f"[{self.session_id}] State mismatch for {operation}: expected {expected_state.value}, got {self.state_manager.state.value}")
            return False
        return True


class OrchestratorWSHandler:
    """
    Unified WebSocket handler for Orchestra-daytona.
    
    Single connection handles:
    - Audio input (microphone chunks)
    - Audio output (TTS streaming)
    - State synchronization
    - Interrupt handling
    - Multi-language support
    """
    
    def __init__(self, 
                 config: OrchestratorConfig,
                 dialogue_manager: MultiLangDialogueManager,
                 pipeline: ProcessingPipeline,
                 redis_client=None):
        self.config = config
        self.dialogue_manager = dialogue_manager
        self.pipeline = pipeline
        self.redis_client = redis_client
        self.sessions: Dict[str, OrchestratorSession] = {}
        
        logger.info(f"✅ OrchestratorWSHandler initialized (Stream Out: {self.config.languages.stream_out})")

    def _is_bargin_enabled(self) -> bool:
        """Feature gate for barge-in behavior."""
        return bool(getattr(self.config.session, "bargin_feature", False))

    async def shutdown(self):
        """
        Gracefully shutdown the handler and all active sessions.
        """
        logger.info("🛑 OrchestratorWSHandler shutting down...")
        active_sessions = list(self.sessions.values())
        for session in active_sessions:
            try:
                logger.info(f"  Closing session: {session.session_id}")
                await self._cleanup_session(session)
                if not session.is_closed:
                    await session.websocket.close(code=1001, reason="Server shutting down")
            except Exception as e:
                logger.error(f"  Error closing session {session.session_id}: {e}")
        
        logger.info(f"✅ Closed {len(active_sessions)} active sessions")

    def _validate_message(self, msg: Any) -> bool:
        """
        Validate incoming WebSocket message structure and size.
        """
        if not isinstance(msg, dict):
            logger.warning("❌ Invalid message format: not a dictionary")
            return False
            
        # Check basic structure (accept 'type' or 'action')
        identifier = msg.get("type") or msg.get("action")
        if not identifier:
            logger.warning("❌ Invalid message: missing identifier ('type' or 'action')")
            return False
            
        if not isinstance(identifier, str):
            logger.warning("❌ Invalid message: identifier ('type' or 'action') must be a string")
            return False
            
        # Check size (rough estimation) - limit to 1MB
        msg_str = json.dumps(msg)
        if len(msg_str) > 1024 * 1024:
            logger.warning(f"❌ Message too large: {len(msg_str)} bytes")
            return False
            
        return True
    
    def _get_output_language(self, detected_language: str, session: Optional[OrchestratorSession] = None) -> str:
        """
        Determine output language based on stream_out configuration.

        Args:
            detected_language: Language detected from user input
            session: Optional session object to check for language overrides

        Returns:
            Language code to use for TTS and audio files
        """
        # Check for session-specific language override first
        if session and hasattr(session, 'stream_out_override') and session.stream_out_override:
            logger.debug(f"Using session language override: {session.stream_out_override}")
            return session.stream_out_override

        stream_out_raw = self.config.languages.stream_out
        stream_out = stream_out_raw.lower()

        if stream_out == "auto":
            # Use detected language, fallback to default
            return detected_language or self.config.languages.default
        elif stream_out_raw:
            # Force specific language regardless of input (supports BCP-47 codes)
            return stream_out_raw
        else:
            # Empty value, fallback to auto behavior
            logger.warning("Empty stream_out value, using auto mode")
            return detected_language or self.config.languages.default

    @staticmethod
    def _intro_lang_suffix(language: str) -> str:
        lang = (language or "").strip().lower()
        if lang in ("de", "de-de", "german", "deu"):
            return "DE"
        return "EN"

    def _resolve_intro_from_env(self, session: OrchestratorSession, output_language: str) -> str:
        """
        Resolve intro text from env (tenant-specific first, then compose fallback).
        Env patterns:
        - TENANT_ID_INTRO=<tenant_key>
        - INTRO_<TENANT_KEY>_EN / INTRO_<TENANT_KEY>_DE
        - <TENANT_KEY>_INTRO_EN / <TENANT_KEY>_INTRO_DE
        - INTRO_<TENANT_KEY> / <TENANT_KEY>_INTRO
        - DEFAULT_INTRO_EN / DEFAULT_INTRO_DE / DEFAULT_INTRO
        """
        suffix = self._intro_lang_suffix(output_language)

        tenant_candidates: List[str] = []
        if session.tenant_id:
            tenant_candidates.append(str(session.tenant_id).strip().lower())
        if session.agent_name:
            clean_name = session.agent_name.lower().replace(" agent", "").strip()
            if clean_name and clean_name not in tenant_candidates:
                tenant_candidates.append(clean_name)
        if session.agent_id:
            if session.agent_id.lower() not in tenant_candidates:
                tenant_candidates.append(session.agent_id.lower())
                
        intro_tenant = (os.getenv("TENANT_ID_INTRO", "") or "").strip().lower()
        if intro_tenant and intro_tenant not in tenant_candidates:
            tenant_candidates.append(intro_tenant)

        for tenant_key in tenant_candidates:
            norm = tenant_key.upper().replace("-", "_")
            tenant_keys = [
                f"INTRO_{norm}_{suffix}",
                f"{norm}_INTRO_{suffix}",
                f"INTRO_{norm}",
                f"{norm}_INTRO",
            ]
            for key in tenant_keys:
                val = (os.getenv(key, "") or "").strip()
                if val:
                    return val

        for key in (f"DEFAULT_INTRO_{suffix}", "DEFAULT_INTRO"):
            val = (os.getenv(key, "") or "").strip()
            if val:
                return val

        if suffix == "DE":
            return "Hallo! Ich bin TARA, Ihre KI-Assistentin. Wie kann ich Ihnen helfen?"
        return "Hello! I am TARA, your AI assistant. How can I help you today?"

    def _clear_playback_tracking(self, session: OrchestratorSession) -> None:
        """Reset playback timing metadata for the current turn."""
        session.audio_playback_start_time = None
        session.audio_playback_duration = None
        session.audio_playback_predicted_end = None

    def _detect_language_switch_command(self, text_lower: str) -> Optional[str]:
        """
        Detect if user is requesting a language switch.
        
        Args:
            text_lower: Lowercase user input text
            
        Returns:
            New language code ('en' or 'de') if switch detected, None otherwise
        """
        # German language switch patterns (user wants German responses)
        GERMAN_SWITCH_PATTERNS = [
            # Direct commands
            "speak in german", "speak german", "switch to german",
            "respond in german", "answer in german", "reply in german",
            "talk in german", "use german", "change to german",
            # Repeat/continue in German
            "repeat in german", "say that in german", "in german please",
            "german please", "auf deutsch", "auf deutsch bitte",
            "sprich deutsch", "antworte auf deutsch", "antworten auf deutsch",
            "bitte auf deutsch", "können sie deutsch", "sprechen sie deutsch",
            "in deutsch", "deutsch bitte", "wechsel zu deutsch",
            # Can you speak German?
            "can you speak german", "do you speak german",
            "kannst du deutsch", "sprechen sie deutsch",
        ]
        
        # English language switch patterns (user wants English responses)
        ENGLISH_SWITCH_PATTERNS = [
            # Direct commands
            "speak in english", "speak english", "switch to english",
            "respond in english", "answer in english", "reply in english",
            "talk in english", "use english", "change to english",
            # Repeat/continue in English
            "repeat in english", "say that in english", "in english please",
            "english please", "auf englisch", "auf englisch bitte",
            "sprich englisch", "antworte auf englisch", "antworten auf englisch",
            "bitte auf englisch", "können sie englisch", "sprechen sie englisch",
            "in englisch", "englisch bitte", "wechsel zu englisch",
            # Can you speak English?
            "can you speak english", "do you speak english",
            "kannst du englisch",
        ]
        
        # Check for German switch
        for pattern in GERMAN_SWITCH_PATTERNS:
            if pattern in text_lower:
                return "de"
        
        # Check for English switch
        for pattern in ENGLISH_SWITCH_PATTERNS:
            if pattern in text_lower:
                return "en"
        
        return None

    def _resolve_host_config(self, host: str) -> Dict[str, Any]:
        """
        Resolve RAG URL and Voice ID based on the incoming Host header.
        """
        # User defined defaults
        default_agent = (self.config.agent.id or self.config.organization.agent_id or os.getenv("AGENT_ID", "agent")).strip() or "agent"
        # Priority: Use CARTESIA_VOICE_ID env var, then config.yaml, then hardcoded fallback
        default_voice = os.getenv("CARTESIA_VOICE_ID")
        
        if not default_voice and self.config.services.tts.voices.get("en"):
            default_voice = self.config.services.tts.voices["en"].voice_id
            
        if not default_voice or default_voice == "default":
            default_voice = "a0e99841-438c-4a64-b679-ae501e7d6091" # Standard Cartesia English
        
        config = {
            "agent_name": default_agent,
            "rag_url": self.config.services.rag.url,
            "voice_id": default_voice
        }

        if not host:
            return config

        # Clean host (remove port if present)
        clean_host = host.split(":")[0].lower()

        # Extract agent name from subdomain if a suffix is configured.
        # Example with AGENT_HOST_SUFFIX=davinciai.eu:
        # partner.davinciai.eu -> partner
        agent_name = default_agent
        host_suffix = (os.getenv("AGENT_HOST_SUFFIX", "") or "").strip().lower().lstrip(".")
        if host_suffix and clean_host.endswith(f".{host_suffix}"):
            subdomain = clean_host[: -len(host_suffix) - 1]
            if subdomain and "." not in subdomain:
                agent_name = subdomain
            config["agent_name"] = agent_name

        voice_env_key = f"VOICE_ID_{agent_name.upper().replace('-', '_')}"
        voice_id_override = os.getenv(voice_env_key)
        if voice_id_override:
            config["voice_id"] = voice_id_override
        else:
            config["voice_id"] = default_voice

        logger.info(
            f"🌐 Host Routing | Host: {clean_host} | Host Suffix: {host_suffix or 'N/A'} | "
            f"Agent: {agent_name} | RAG: {config['rag_url']} | Voice: {config['voice_id']}"
        )

        return config

    def _resolve_tenant_voice_id(self, tenant_id: Optional[str], fallback_voice_id: Optional[str] = None, agent_name: Optional[str] = None, agent_id: Optional[str] = None) -> Optional[str]:
        """
        Resolve voice id by tenant using env pattern:
        - <tenant>_voice_id
        - <TENANT>_VOICE_ID
        Fallback to provided voice id, then CARTESIA_VOICE_ID.
        """
        tenant_candidates = []
        if tenant_id:
            tenant_candidates.append(tenant_id.strip().lower())
        if agent_name:
            clean_name = agent_name.lower().replace(" agent", "").strip()
            if clean_name and clean_name not in tenant_candidates:
                tenant_candidates.append(clean_name)
        if agent_id:
            if agent_id.lower() not in tenant_candidates:
                tenant_candidates.append(agent_id.lower())
                
        for tenant in tenant_candidates:
            safe_tenant = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in tenant)
            by_lower = os.getenv(f"{safe_tenant}_voice_id")
            by_upper = os.getenv(f"{safe_tenant.upper()}_VOICE_ID")
            if by_lower:
                return by_lower
            if by_upper:
                return by_upper

        return fallback_voice_id or os.getenv("DEFAULT_VOICE_ID") or os.getenv("CARTESIA_VOICE_ID")

    async def _persist_session_runtime_config(self, session: OrchestratorSession):
        """Persist dynamic runtime session routing config in Redis for stability/debugging."""
        if not self.redis_client:
            return
        try:
            redis_key = f"orchestra_daytona:session_config:{session.session_id}"
            await self.redis_client.hset(redis_key, mapping={
                "tenant_id": str(session.tenant_id or ""),
                "agent_name": str(session.agent_name or ""),
                "agent_id": str(session.agent_id or ""),
                "voice_id": str(session.voice_id_override or ""),
                "rag_url": str(session.rag_url or ""),
                "updated_at": str(time.time())
            })
            await self.redis_client.expire(redis_key, int(getattr(self.config.session, "ttl_seconds", 3600)))
        except Exception as e:
            logger.warning(f"[{session.session_id}] ⚠️ Failed to persist session runtime config: {e}")

    async def handle_connection(self,
                               websocket: WebSocket,
                               session_id: Optional[str] = None):
        """Main WebSocket connection handler"""
        # Helper to get session ID from query params
        query_params = dict(websocket.query_params)
        session_id_from_query = query_params.get("session_id")
        user_id = query_params.get("user_id")
        
        if session_id_from_query:
            session_id = session_id_from_query

        # Generate secure session ID if not provided
        if not session_id:
            session_id = secrets.token_urlsafe(16)
        
        logger.info(f"🔌 Connection: {websocket.client} | Session: {session_id} | Identity: {user_id or 'anonymous'}")
        
        # PROCOM FIX: Prevent Service Hijacking
        # The STT service (and potentially others) might try to connect back to the Orchestrator
        # using the session ID. We must NOT let them overwrite the User's Browser WebSocket.
        source = query_params.get("source")
        if source in ["stt_groq", "stt_groq_whisper", "stt_sarvam"]:
            logger.warning(f"🔍 [{session_id}] Internal service '{source}' connected. Preventing session hijack.")
            await websocket.accept()
            # Accept but DO NOT assign to the session. Drain messages until disconnect
            # instead of sleeping forever (which creates zombie connections).
            try:
                while True:
                    # Actually read from the WS so we detect disconnects immediately
                    msg = await asyncio.wait_for(websocket.receive(), timeout=120.0)
                    if msg.get("type") == "websocket.disconnect":
                        break
            except (asyncio.TimeoutError, WebSocketDisconnect, Exception) as e:
                logger.debug(f"[{session_id}] Internal service '{source}' disconnected: {type(e).__name__}")
            finally:
                try:
                    await websocket.close()
                except Exception:
                    pass
            return
        
        await websocket.accept()
        
        # Create or reuse session
        if session_id and session_id in self.sessions:
            session = self.sessions[session_id]
            session.websocket = websocket
            # Update user_id if provided on reconnect
            if user_id:
                session.user_id = user_id
            logger.info(f"[{session_id}] Reconnected to existing session (User: {session.user_id})")
        else:
            session = await self._create_session(websocket, session_id, user_id)
            session_id = session.session_id
            logger.info(f"[{session_id}] ✅ New session created")
        
        # Resolved host config first
        host_header = websocket.headers.get("host", "")
        host_config = self._resolve_host_config(host_header)
        
        # Priority: Query Param > Host Config > Default Config
        if query_params.get("tenant_id"):
            session.tenant_id = str(query_params.get("tenant_id"))
        session.agent_name = query_params.get("agent_name") or host_config["agent_name"]
        session.rag_url = query_params.get("rag_url") or host_config["rag_url"]
        base_voice = query_params.get("voice_id") or host_config["voice_id"]
        session.voice_id_override = self._resolve_tenant_voice_id(session.tenant_id, base_voice, session.agent_name, session.agent_id)
        
        if query_params.get("rag_url"):
            logger.info(f"[{session_id}] 🚀 DYNAMIC RAG OVERRIDE: {session.rag_url}")
        if query_params.get("voice_id"):
            logger.info(f"[{session_id}] 🎤 DYNAMIC VOICE OVERRIDE: {session.voice_id_override}")

        logger.info(
            f"[{session_id}] 🌐 Final Session Config: tenant={session.tenant_id} "
            f"RAG={session.rag_url}, Voice={session.voice_id_override}"
        )
        await self._persist_session_runtime_config(session)

        session.stt_client = STTClient(self.config.services.stt, skip_ssl=self.config.server.skip_ssl_verify)
        session.tts_client = TTSClient(self.config.services.tts, skip_ssl=self.config.server.skip_ssl_verify)

        # Connection logic consolidated in internal handler
        await self._handle_connection_internal(session)

    async def _handle_connection_internal(self, session: OrchestratorSession):
        """
        Internal connection logic shared by browser and phone handlers.
        Handles service connections, message loops, and cleanup.
        """
        session_id = session.session_id
        websocket = session.websocket

        # Connect services in background
        session.stt_receive_task = asyncio.create_task(self._stt_receive_loop(session))
        session.tts_receive_task = asyncio.create_task(self._tts_receive_loop(session))
        
        # Wait for services to be ready (up to 5 seconds)
        try:
            await asyncio.wait_for(asyncio.gather(
                session.stt_connected_event.wait(),
                session.tts_connected_event.wait()
            ), timeout=5.0)
            logger.info(f"[{session_id}] ✅ Backend services connected (STT & TTS)")
        except asyncio.TimeoutError:
            logger.warning(f"[{session_id}] ⚠️ One or more backend services timed out during initial connection. Proceeding in degraded mode.")

        # Check if client disconnected while we were waiting
        if session.is_closed:
            logger.info(f"[{session_id}] Client disconnected during service handshake, aborting.")
            return

        # Send session ready notification with service metadata (only if not a phone session)
        if not isinstance(session, PhoneOrchestratorSession):
            await self._send_json(websocket, {
                "type": "session_ready",
                "session_id": session_id,
                "timestamp": time.time(),
                "services": {
                    "stt_url": self.config.services.stt.url,
                    "tts_url": self.config.services.tts.url,
                    "rag_url": session.rag_url or self.config.services.rag.url
                },
                "config": {
                    "stt_language_default": self.config.languages.default,
                    "tts_language_default": self.config.languages.default,
                    "supported_languages": self.config.languages.supported
                }
            }, session)
        else:
            # For phone sessions, automatically start the session (triggers intro)
            await self._handle_start_session(session)
        
        # Start timeout monitor
        timeout_task = asyncio.create_task(self._timeout_monitor(session))
        
        # Start keep-alive loop to prevent connection timeout
        # Using a dedicated method for better testing and error handling
        keep_alive_task = asyncio.create_task(self._keep_alive_loop(session))
        
        try:
            # Main message loop
            while not session.is_closed:
                try:
                    # Use generic receive() to handle both JSON and BINARY efficiently
                    message = await websocket.receive()
                    
                    if message["type"] == "websocket.receive":
                        if "text" in message:
                            try:
                                data = json.loads(message["text"])
                                if self._validate_message(data):
                                    await self._route_message(session, data)
                            except json.JSONDecodeError:
                                logger.warning(f"[{session_id}] Received invalid JSON text")
                        
                        elif "bytes" in message:
                            # Handle binary audio data
                            await self._handle_binary_audio(session, message["bytes"])
                    
                    elif message["type"] == "websocket.disconnect":
                        logger.info(f"[{session_id}] Client disconnected (browser closed WebSocket)")
                        break
                        
                except Exception as e:
                    if not session.is_closed:
                        logger.error(f"[{session_id}] Error in main loop: {e}", exc_info=True)
                    break
        
        except WebSocketDisconnect:
            logger.info(f"[{session_id}] Client disconnected (browser closed WebSocket)")
            session.is_closed = True
        except Exception as e:
            error_msg = str(e)
            session.is_closed = True
            
            # Suppress common race condition errors during disconnect
            if "accept" in error_msg.lower() or "not connected" in error_msg.lower() or "closed" in error_msg.lower():
                logger.info(f"[{session_id}] WebSocket closed gracefully: {error_msg}")
            else:
                logger.error(f"[{session_id}] WebSocket error: {error_msg}", exc_info=True)
                # Log connection state for debugging
                logger.warning(f"[{session_id}] ⚠️ Connection closed unexpectedly - check browser/client logs")
        finally:
            # Mark session as closed to stop all ongoing operations
            session.is_closed = True
            
            # Cancel all background tasks
            tasks_to_cancel = [
                session.stt_receive_task,
                session.tts_receive_task,
                timeout_task,
                keep_alive_task,
                session.pipeline_task,
                session.tts_task,
                session.filler_task,
                session.final_accumulation_timer,
                session.audio_playback_server_timer,
                session.transcript_timeout_task
            ]
            
            for task in tasks_to_cancel:
                if task and not task.done():
                    task.cancel()
            
            # Wait for tasks to complete cancellation
            for task in tasks_to_cancel:
                if task:
                    try:
                        await asyncio.wait_for(task, timeout=1.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                    except Exception:
                        pass
            
            await self._cleanup_session(session)

    async def handle_audio_stream(self, websocket: WebSocket, session_id: Optional[str] = None):
        """
        Dedicated audio streaming WebSocket handler.
        Binary-only channel for TTS audio to eliminate contention with control messages.
        """
        # Resolve session_id from query params if not provided
        query_params = dict(websocket.query_params)
        if not session_id:
            session_id = query_params.get("session_id")

        if not session_id or session_id not in self.sessions:
            await websocket.close(code=4004, reason="Invalid or missing session_id")
            return

        session = self.sessions[session_id]

        # Only allow audio WS for interactive mode sessions
        if session.interaction_mode == "turbo":
            await websocket.close(code=4003, reason="Audio stream not available in turbo mode")
            return

        await websocket.accept()
        logger.info(f"[{session_id}] 🔊 Audio stream WebSocket connected")

        # Register on session
        session.audio_websocket = websocket
        session.audio_ws_connected = True

        # Send handshake
        try:
            await websocket.send_json({
                "type": "audio_stream_ready",
                "session_id": session_id
            })
        except Exception as e:
            logger.error(f"[{session_id}] Audio stream handshake failed: {e}")
            session.audio_ws_connected = False
            session.audio_websocket = None
            return

        # Keep-alive loop: listen for client messages (e.g. audio_interrupt)
        try:
            while not session.is_closed:
                try:
                    message = await websocket.receive()

                    if message["type"] == "websocket.disconnect":
                        break

                    if message["type"] == "websocket.receive":
                        # Handle text messages (JSON commands like audio_interrupt)
                        if "text" in message:
                            try:
                                data = json.loads(message["text"])
                                msg_type = data.get("type")
                                if msg_type == "audio_interrupt":
                                    logger.info(f"[{session_id}] 🔊 Audio interrupt via audio stream")
                                    await self._handle_interrupt(session, data)
                                elif msg_type == "ping":
                                    await websocket.send_json({"type": "pong"})
                            except json.JSONDecodeError:
                                pass
                        # Binary messages from client on audio WS are ignored
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger.warning(f"[{session_id}] Audio stream receive error: {e}")
                    break
        finally:
            # Cleanup
            session.audio_ws_connected = False
            session.audio_websocket = None
            logger.info(f"[{session_id}] 🔊 Audio stream WebSocket disconnected")

    async def _create_session(self, websocket: WebSocket, session_id: Optional[str] = None, user_id: Optional[str] = None) -> OrchestratorSession:
        """Create new session"""
        if session_id is None:
            session_id = f"tara_{uuid.uuid4().hex[:12]}"
        
        state_mgr = StateManager(session_id, self.redis_client)
        await state_mgr.initialize()
        
        session = OrchestratorSession(
            session_id=session_id,
            websocket=websocket,
            state_manager=state_mgr,
            user_id=user_id,
            agent_name=self.config.agent.name,
            agent_id=self.config.agent.id,
            tenant_id=self.config.agent.tenant_id
        )
        session.metadata["session_type"] = "webcall"
        
        self.sessions[session_id] = session
        return session
    
    async def _route_message(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Route incoming message to appropriate handler"""
        # Support both 'type' (standard) and 'action' (new format) identifiers
        msg_type = msg.get("type") or msg.get("action")
        
        if msg_type != "audio_chunk" and msg_type != "pong":
            logger.info(f"[{session.session_id}] 📥 Incoming Frontend Message: {msg_type}")

        if msg_type == "audio_chunk":
            await self._handle_audio_chunk(session, msg)
        elif msg_type == "text_input":  # Mock STT mode
            await self._handle_text_input(session, msg)
        elif msg_type == "playback_done":
            await self._handle_playback_done(session, msg)
        elif msg_type == "playback_start":
            await self._handle_playback_start(session, msg)
        elif msg_type == "playback_heartbeat":
            await self._handle_playback_heartbeat(session, msg)
        elif msg_type == "playback_confirmed_end":
            await self._handle_playback_done(session, msg)  # Same handler as playback_done
        elif msg_type == "interrupt":
            await self._handle_interrupt(session, msg)
        elif msg_type == "start_session":
            await self._handle_start_session(session, msg)
        elif msg_type == "end_session":
            await self._handle_end_session(session)
        elif msg_type == "session_config":
            await self._handle_session_config(session, msg)
        elif msg_type == "dom_update":
            await self._handle_dom_update(session, msg)
        elif msg_type == "dom_delta":
            await self._handle_dom_delta(session, msg)
        elif msg_type == "execution_complete":
            await self._handle_execution_complete(session, msg)
        elif msg_type == "change_language":
            await self._handle_change_language(session, msg)
        elif msg_type == "speaker_mute":
            await self._handle_speaker_mute(session, msg)
        elif msg_type == "request_asset":
            await self._handle_request_asset(session, msg)
        elif msg_type == "pong":
            # Track last pong for dead connection detection
            session.last_pong_time = time.time()
        elif msg_type == "request_history":
            # ── PHOENIX PROTOCOL: Cross-Domain Session Restoration ──
            # Frontend just woke up on a new domain and is asking for its memories
            await self._handle_request_history(session, msg)
        else:
            logger.warning(f"[{session.session_id}] Unknown message type: {msg_type}")
    
    async def _handle_audio_chunk(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Handle incoming audio chunk from browser"""
        # Check if session is closed
        if session.is_closed:
            return
        
        state_mgr = session.state_manager
        if state_mgr.state == State.SPEAKING and not self._is_bargin_enabled():
            logger.debug(f"[{session.session_id}] ⏸️ Ignoring audio chunk during SPEAKING (BARGIN_FEATURE=false)")
            return
        
        session.last_activity = time.time()
        session.audio_chunks_received += 1
        
        logger.debug(f"[{session.session_id}] 📡 Received audio chunk #{session.audio_chunks_received}, size: {len(msg.get('data', ''))} chars")
        
        try:
            audio_b64 = msg.get("data", "")
            if not audio_b64:
                logger.warning(f"[{session.session_id}] Empty audio data in chunk")
                return
            
            audio_bytes = base64.b64decode(audio_b64)
            logger.debug(f"[{session.session_id}] 🔊 Decoded audio bytes: {len(audio_bytes)} bytes")
            
            # Send to STT service
            if session.stt_client and session.stt_client.ws:
                await session.stt_client.send_audio(audio_bytes)
                logger.debug(f"[{session.session_id}] 📤 Sent audio to STT service")
            else:
                logger.warning(f"[{session.session_id}] STT client not connected, cannot send audio")
        
        except Exception as e:
            # Log transport errors at debug level to avoid spam
            if "closing transport" in str(e).lower():
                logger.debug(f"[{session.session_id}] Audio chunk transport closed")
            else:
                logger.error(f"[{session.session_id}] ❌ Audio chunk error: {e}", exc_info=True)
    
    async def _handle_binary_audio(self, session: OrchestratorSession, audio_bytes: bytes):
        """Handle raw binary audio data - ALWAYS forward to STT regardless of state"""
        state_mgr = session.state_manager
        if state_mgr.state == State.SPEAKING and not self._is_bargin_enabled():
            logger.debug(f"[{session.session_id}] ⏸️ Ignoring binary audio during SPEAKING (BARGIN_FEATURE=false)")
            return
        
        session.last_activity = time.time()
        session.audio_chunks_received += 1
        
        try:
            if session.stt_client and session.stt_client.ws:
                # CRITICAL: Yield BEFORE sending to give receive loop priority
                # This ensures transcripts can be processed even during continuous audio streaming
                await asyncio.sleep(0)
                
                await session.stt_client.send_audio(audio_bytes)
                
                # Log every 100th chunk to show audio is flowing
                if session.audio_chunks_received % 100 == 1:
                    logger.debug(f"[{session.session_id}] 📤 Audio chunk #{session.audio_chunks_received} sent to STT ({len(audio_bytes)} bytes)")
                
                # CRITICAL FIX: Yield AFTER sending to ensure receive loop gets CPU time
                # This prevents continuous audio forwarding from monopolizing the event loop
                # The receive loop needs regular opportunities to process incoming transcripts
                await asyncio.sleep(0)
            else:
                # Log warning but don't spam
                if session.audio_chunks_received % 50 == 1:
                    logger.warning(f"[{session.session_id}] ⚠️ STT client not connected, audio chunk #{session.audio_chunks_received} not sent")
        except Exception as e:
            # Log transport errors at debug level to avoid spam
            if "closing transport" in str(e).lower() or "connection reset" in str(e).lower():
                logger.debug(f"[{session.session_id}] STT transport closed during binary send")
            else:
                logger.error(f"[{session.session_id}] ❌ Binary audio error: {e}", exc_info=True)
    
    async def _handle_text_input(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """
        Handle text input from mock STT mode.
        Bypasses STT service and processes text directly through RAG pipeline.
        """
        if session.is_closed:
            return

        text = msg.get("text", "").strip()
        if not text:
            logger.warning(f"[{session.session_id}] Empty text input in mock mode")
            return

        logger.info(f"[{session.session_id}] 📝 Mock STT input: '{text}'")
        session.last_activity = time.time()

        state_mgr = session.state_manager

        # Check state
        if state_mgr.state not in [State.IDLE, State.LISTENING]:
            logger.warning(f"[{session.session_id}] Ignoring text input in state: {state_mgr.state}")
            return

        # Add to history
        session.history_manager.add_user_turn(text, metadata={"source": "mock_stt"})

        # Transition to THINKING
        thinking_side_effects = await state_mgr.transition(State.THINKING, trigger="text_input")
        output_language = self._get_output_language(session.current_language, session)
        await self._execute_side_effects(session, thinking_side_effects, output_language)
        await self._broadcast_state(session, State.THINKING)

        # Process through RAG (same as audio mode)
        try:
            await self._process_user_input(session, text, output_language)
        except Exception as e:
            logger.error(f"[{session.session_id}] Text input processing error: {e}", exc_info=True)

    async def _stt_receive_loop(self, session: OrchestratorSession):
        """Receive STT transcripts and events from STT service with auto-reconnection"""
        if not session.stt_client:
            return
        
        while not session.is_closed:
            try:
                # Check connection status
                if not session.stt_client.ws or session.stt_client.ws.closed:
                    # Connection might be established in handle_connection, double check
                    if session.stt_client.ws and not session.stt_client.ws.closed:
                        # Already connected, proceed
                        pass
                    else:
                        logger.info(f"[{session.session_id}] 🔌 Reconnecting to STT service...")
                        connected = await session.stt_client.connect(session.session_id)
                        if not connected:
                            logger.warning(f"[{session.session_id}] ❌ Failed to reconnect to STT service, retrying in 2s...")
                            session.stt_connected_event.clear()
                            await asyncio.sleep(2.0)
                            continue
                
                # Signal connection success
                session.stt_connected_event.set()
                
                # Consume messages until connection drops
                async for data in session.stt_client.receive_transcript():
                    if session.is_closed:
                        break
                    
                    # Connection is definitely active if we're receiving
                    session.stt_connected_event.set()
                    
                    # CRITICAL: Yield after receiving each message to ensure fair scheduling
                    # This prevents the receive loop from blocking the audio send path
                    await asyncio.sleep(0)
                        
                    # Route events and transcripts to appropriate handlers
                    msg_type = data.get("type", "transcript")
                    if msg_type == "event":
                        await self._handle_stt_event(session, data)
                    else:
                        await self._handle_stt_result(session, data)
                    
                    # CRITICAL: Yield after processing to ensure audio path can continue
                    await asyncio.sleep(0)
                
                # If generator finishes, connection dropped or session closed
                if not session.is_closed:
                    logger.warning(f"[{session.session_id}] ⚠️ STT connection dropped, attempting reconnection...")
                    session.stt_connected_event.clear()
                    await asyncio.sleep(1.0)
                    
            except asyncio.CancelledError:
                logger.info(f"[{session.session_id}] STT receive loop cancelled")
                break
            except Exception as e:
                logger.error(f"[{session.session_id}] STT receive error: {e}", exc_info=True)
                session.stt_connected_event.clear()
                await asyncio.sleep(2.0)

    async def _tts_receive_loop(self, session: OrchestratorSession):
        """Receive audio chunks from TTS service with auto-reconnection"""
        if not session.tts_client:
            return
        
        reconnect_delay = 1.0  # Start with 1s, backoff on consecutive failures
        max_reconnect_delay = 10.0
        
        while not session.is_closed:
            try:
                # Check connection status
                if not session.tts_client.ws or session.tts_client.ws.closed:
                    # Guard: Don't reconnect if session is being torn down
                    if session.is_closed:
                        break
                    logger.info(f"[{session.session_id}] 🔌 Reconnecting to TTS service...")
                    connected = await session.tts_client.connect(session.session_id)
                    if not connected:
                        logger.warning(f"[{session.session_id}] ❌ Failed to reconnect to TTS service, retrying in {reconnect_delay:.1f}s...")
                        session.tts_connected_event.clear()
                        await asyncio.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)
                        continue
                
                # Reset backoff on successful connection
                reconnect_delay = 1.0
                
                # Signal connection success
                session.tts_connected_event.set()
                
                # Consume messages and put into queue
                async for data in session.tts_client.receive_audio():
                    if session.is_closed:
                        break
                    
                    # Connection is active
                    session.tts_connected_event.set()
                    
                    # Yield to allow other tasks to run
                    await asyncio.sleep(0)
                    
                    # Store in queue for _stream_tts_to_browser
                    try:
                        session.tts_audio_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        # Drop oldest if queue is full (prevent memory buildup)
                        try:
                            session.tts_audio_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        await session.tts_audio_queue.put(data)

                    # Record first audio chunk time for TTFC
                    if "speech_end" in session.current_turn_timers and "first_audio" not in session.current_turn_timers:
                        session.current_turn_timers["first_audio"] = time.time()
                        ttfc = (session.current_turn_timers["first_audio"] - session.current_turn_timers["speech_end"]) * 1000
                        logger.info(f"[{session.session_id}] 🔊 TTFC: {ttfc:.1f}ms (Turn: {session.current_turn_timers.get('user_text', 'unknown')[:30]}...)")
                        
                        # UPDATE TURN METRICS (fix for 0.0 average in session report)
                        if session.turn_metrics:
                            last_metrics = session.turn_metrics[-1]
                            if last_metrics.get("user_text") == session.current_turn_timers.get("user_text"):
                                last_metrics["ttfc_ms"] = ttfc
                                logger.debug(f"[{session.session_id}] 📊 Updated turn_metrics with TTFC: {ttfc:.1f}ms")
                
                if not session.is_closed:
                    logger.warning(f"[{session.session_id}] ⚠️ TTS connection dropped, attempting reconnection...")
                    session.tts_connected_event.clear()
                    await asyncio.sleep(reconnect_delay)
                    
            except asyncio.CancelledError:
                logger.info(f"[{session.session_id}] TTS receive loop cancelled")
                break
            except Exception as e:
                logger.error(f"[{session.session_id}] TTS receive error: {e}")
                session.tts_connected_event.clear()
                await asyncio.sleep(2.0)

    async def _handle_stt_result(self, session: OrchestratorSession, data: Dict[str, Any]):
        """Handle STT transcription result"""
        # Check if session is closed
        if session.is_closed:
            return
        state_mgr = session.state_manager
        if state_mgr.state == State.SPEAKING and not self._is_bargin_enabled():
            logger.debug(f"[{session.session_id}] ⏸️ Ignoring STT result during SPEAKING (BARGIN_FEATURE=false)")
            return

        text = data.get("text", "")
        is_final = data.get("is_final", False)

        if not text or not text.strip():
            # CRITICAL: If we receive an empty FINAL transcript while in THINKING (waiting for text),
            # we MUST transition back to LISTENING, otherwise we stay stuck in THINKING.
            if is_final and session.state_manager.state == State.THINKING and session.pending_transcript:
                logger.info(f"[{session.session_id}] 🔄 Empty FINAL transcript received while THINKING - returning to LISTENING")
                session.pending_transcript = False
                session.partial_transcript = ""
                if session.transcript_timeout_task and not session.transcript_timeout_task.done():
                    session.transcript_timeout_task.cancel()
                
                await session.state_manager.transition(State.LISTENING, trigger="empty_final_transcript")
                await self._broadcast_state(session, State.LISTENING)
            return
        
        session.last_activity = time.time()
        session.timeout_count = 0
        
        # Detect language logic
        # PRIORITY 1: STT Metadata (e.g. Sarvam 'language_code') - Used for Translation Mode
        # This handles cases where audio is Hindi, but text is translated English.
        # We need to know it was Hindi to respond in Hindi.
        detected_lang = data.get("language_code")
        
        # PRIORITY 2: Direct language field
        if not detected_lang:
            detected_lang = data.get("language")
        
        # PRIORITY 3: Text-based detection (Fallback)
        if not detected_lang:
            # Only detect if enough text, else default to session/config default
            if len(text) > 5:
                detected_lang = detect_language(text, self.config.languages.supported)

        # If still nothing, fallback to session default or english
        if not detected_lang:
            detected_lang = session.current_language or "en"
            
        session.current_language = detected_lang
        session.state_manager.context.current_language = detected_lang
        
        if is_final:
            logger.info(f"[{session.session_id}] 📝 STT FINAL [{detected_lang}]: {text}")
        else:
            logger.debug(f"[{session.session_id}] 📝 STT Fragment [{detected_lang}]: {text}")
        
        # Start timing for the turn when we get the final transcript
        if is_final:
            session.current_turn_timers = {
                "speech_end": time.time(),
                "user_text": text
            }
        
        # VALIDATED BARGE-IN: Check STT transcript to confirm real speech before interrupting
        if state_mgr.state == State.SPEAKING or session.barge_in_pending:
            stripped = text.strip().rstrip(".!?,")
            no_speech_prob = data.get("no_speech_prob")

            # Filter out filler words / noise artifacts that STT commonly produces
            NOISE_PHRASES = {
                "mm-hmm", "mmhmm", "mm hmm", "uh-huh", "uh huh", "uhuh",
                "hmm", "hm", "um", "uh", "ah", "oh", "mhm", "mm",
                "yeah", "yep", "yup", "ok", "okay", "right",
                "bye", "bye-bye", "goodbye",
                "thank you", "thanks",
            }
            is_filler = stripped.lower() in NOISE_PHRASES

            # Valid barge-in: meaningful FINAL text (low-noise) that is not filler.
            # Keep this permissive enough for short but valid utterances like "stop", "wait", "no".
            is_valid = (
                not is_filler
                and len(stripped) >= 3
                and is_final
                and (no_speech_prob is None or no_speech_prob < 0.7)
            )

            if is_valid:
                logger.info(f"[{session.session_id}] 🛑 VALID barge-in ({'FINAL' if is_final else 'Fragment'}): '{text}' (nsp={no_speech_prob})")
                session.barge_in_pending = False
                await self._handle_interrupt(session, {"type": "interrupt", "trigger": "stt_validated"})
                # After interrupt, continue to process the final transcript in LISTENING state
                # but return here if it was a fragment to avoid duplicate processing later
                if not is_final:
                    return
            else:
                logger.info(f"[{session.session_id}] 🚫 Noise barge-in rejected: '{stripped}' (nsp={no_speech_prob})")
                return  # Drop noise transcript entirely (both partial and final)

        # Store partial transcript for early processing
        if not is_final:
            session.partial_transcript = text
        
        # Send transcript to browser (skip if session closed)
        # Forward ALL metadata from STT service (latency, logprob, etc.)
        payload = {
            "type": "transcript",
            "text": text,
            "is_final": is_final,
            "language": detected_lang,
            "timestamp": time.time(),
            **{k: v for k, v in data.items() if k not in ["type", "text", "is_final", "language"]}
        }
        
        if not await self._send_json(session.websocket, payload, session):
            return  # Client disconnected, skip further processing
        
        if is_final:
            state_mgr = session.state_manager
            if state_mgr.state == State.THINKING and session.pending_transcript:
                session.pending_transcript = False
                
                # Cancel transcript timeout task (transcript arrived)
                if session.transcript_timeout_task and not session.transcript_timeout_task.done():
                    session.transcript_timeout_task.cancel()
                    try:
                        await asyncio.wait_for(session.transcript_timeout_task, timeout=0.1)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                
                # Cancel any existing pipeline if processing with partial text
                if session.pipeline_task and not session.pipeline_task.done():
                    logger.info(f"[{session.session_id}] ⏸️ Cancelling partial processing, restarting with final transcript")
                    session.pipeline_task.cancel()
                    try:
                        await asyncio.wait_for(session.pipeline_task, timeout=0.1)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                
                # Process final transcript immediately
                try:
                    await self._process_user_input(session, text, detected_lang)
                except Exception as e:
                    logger.error(f"[{session.session_id}] ❌ Error processing final transcript in THINKING state: {e}", exc_info=True)
                return
            
            # EXISTING: Accumulate FINAL transcripts when in LISTENING state
            # if state_mgr.state == State.LISTENING:
            #     # Accumulate FINAL transcripts that arrive within a short window
            #     # This handles cases where VAD splits a single utterance into multiple FINAL segments
            #     logger.info(f"[{session.session_id}] 📝 Accumulating FINAL transcript in LISTENING state: '{text}'")
            #     await self._accumulate_final_transcript(session, text, detected_lang)
            if state_mgr.state == State.LISTENING:
                await self._process_user_input(session, text, detected_lang)
            else:
                logger.warning(f"[{session.session_id}] ⚠️ FINAL transcript received in {state_mgr.state.value} state but pending_transcript={session.pending_transcript} - may be missed!")
                # Fallback: if we're in THINKING but pending_transcript is False, still try to process
                # This handles edge cases where the flag got reset somehow
                if state_mgr.state == State.THINKING:
                    logger.info(f"[{session.session_id}] 🔄 Fallback: Processing FINAL transcript in THINKING state (pending_transcript was False)")
                    try:
                        await self._process_user_input(session, text, detected_lang)
                    except Exception as e:
                        logger.error(f"[{session.session_id}] ❌ Error in fallback processing: {e}", exc_info=True)

    async def _accumulate_final_transcript(self, session: OrchestratorSession, text: str, language: str):
        """
        Accumulate FINAL transcripts that arrive within a short window (500ms).
        
        This handles cases where VAD splits a single utterance into multiple FINAL segments.
        We combine them and process as one query to avoid multiple RAG calls.
        """
        # Cancel any existing accumulation timer
        if session.final_accumulation_timer and not session.final_accumulation_timer.done():
            session.final_accumulation_timer.cancel()
            try:
                await session.final_accumulation_timer
            except asyncio.CancelledError:
                pass
        
        # Accumulate the text
        if session.accumulated_final_text:
            session.accumulated_final_text += " " + text.strip()
        else:
            session.accumulated_final_text = text.strip()
        
        logger.info(f"[{session.session_id}] 📝 Accumulated FINAL transcript: '{session.accumulated_final_text}'")
        
        # Start a timer to wait for more FINAL segments
        async def process_accumulated_final():
            try:
                await asyncio.sleep(session.final_accumulation_timeout)
                # If we get here, no more FINAL segments arrived - process accumulated text
                # Double-check state before processing (might have changed during accumulation window)
                state_mgr = session.state_manager
                if session.accumulated_final_text and not session.is_closed and state_mgr.state == State.LISTENING:
                    logger.info(f"[{session.session_id}] 🚀 Processing accumulated FINAL transcript: '{session.accumulated_final_text}'")
                    try:
                        await self._process_user_input(session, session.accumulated_final_text, language)
                        session.accumulated_final_text = ""  # Clear accumulated text
                    except Exception as e:
                        logger.error(f"[{session.session_id}] ❌ Error processing accumulated FINAL transcript: {e}", exc_info=True)
                        session.accumulated_final_text = ""  # Clear on error too
                elif session.is_closed:
                    logger.debug(f"[{session.session_id}] ⏸️ Skipping processing - session closed")
                elif state_mgr.state != State.LISTENING:
                    logger.debug(f"[{session.session_id}] ⏸️ Skipping processing - state changed to {state_mgr.state.value} during accumulation")
                    session.accumulated_final_text = ""  # Clear accumulated text
                elif not session.accumulated_final_text:
                    logger.debug(f"[{session.session_id}] ⏸️ Skipping processing - no accumulated text")
            except asyncio.CancelledError:
                # New FINAL segment arrived, don't process yet
                logger.debug(f"[{session.session_id}] ⏸️ Accumulation timer cancelled - new FINAL segment arrived")
            except Exception as e:
                logger.error(f"[{session.session_id}] ❌ Error in accumulation timer: {e}", exc_info=True)
        
        session.final_accumulation_timer = asyncio.create_task(process_accumulated_final())
        logger.debug(f"[{session.session_id}] ⏱️ Started accumulation timer (500ms) for: '{text}' (state: {session.state_manager.state.value})")
    
    async def _handle_stt_event(self, session: OrchestratorSession, data: Dict[str, Any]):
        """
        Handle STT VAD events (speech_start, speech_end, transcript_rejected).
        
        This enables low-latency transitions by reacting to speech_end immediately,
        before the final transcript arrives from the STT API.
        """
        if session.is_closed:
            return
        
        event_type = data.get("event_type")
        signal_type = data.get("signal_type", "").upper()
        
        if signal_type == "SPEECH_END":
            state_mgr = session.state_manager
            if state_mgr.state == State.SPEAKING and not self._is_bargin_enabled():
                session.barge_in_pending = False
                logger.debug(f"[{session.session_id}] ⏸️ Ignoring SPEECH_END during SPEAKING (BARGIN_FEATURE=false)")
                return

            # DEFERRED BARGE-IN: If SPEAKING, don't hard-interrupt — stay in SPEAKING,
            # pause browser playback, and wait for STT to validate real speech vs noise.
            if state_mgr.state == State.SPEAKING:
                logger.info(f"[{session.session_id}] ⏸️ Potential barge-in (SPEECH_END) - deferring, waiting for STT validation")
                session.barge_in_pending = True
                await self._send_json(session.websocket, {"type": "playback_stop", "timestamp": time.time()}, session)

                # Start a timeout: if no valid STT transcript arrives, recover from noise
                output_language = self._get_output_language(session.current_language, session)

                async def barge_in_validation_timeout():
                    try:
                        await asyncio.sleep(5.0)
                        # Only recover if we're STILL in an active speaking interruption window.
                        # If playback already completed or state moved on, do nothing.
                        if session.barge_in_pending and not session.is_closed and state_mgr.state == State.SPEAKING:
                            session.barge_in_pending = False
                            logger.info(f"[{session.session_id}] 🔄 Noise recovery - no valid transcript after barge-in, returning to LISTENING")
                            await self._recover_from_noise_interrupt(session)
                        else:
                            session.barge_in_pending = False
                    except asyncio.CancelledError:
                        pass  # STT transcript arrived and validated/rejected

                # Cancel any existing timeout, start new one
                if session.transcript_timeout_task and not session.transcript_timeout_task.done():
                    session.transcript_timeout_task.cancel()
                session.transcript_timeout_task = asyncio.create_task(barge_in_validation_timeout())
                return  # Stay in SPEAKING — don't fall through to THINKING transition

            # NORMAL PATH: LISTENING → THINKING (user finished speaking, wait for transcript)
            if state_mgr.state == State.LISTENING:
                # Mark that we're waiting for final transcript
                session.pending_transcript = True

                # IMMEDIATE TRANSITION to THINKING
                side_effects = await state_mgr.transition(State.THINKING, trigger="speech_end", data={})
                await self._broadcast_state(session, State.THINKING)

                output_language = self._get_output_language(session.current_language, session)
                await self._execute_side_effects(session, side_effects, output_language)

                # Start timeout task: if transcript doesn't arrive within 5 seconds, transition back to LISTENING
                async def transcript_timeout():
                    try:
                        await asyncio.sleep(5.0)  # 5 second timeout
                        if session.pending_transcript and state_mgr.state == State.THINKING:
                            logger.warning(f"[{session.session_id}] ⏰ Transcript timeout - no transcript received after 5s, transitioning back to LISTENING")
                            session.pending_transcript = False
                            session.partial_transcript = ""

                            # Cancel tasks
                            if session.pipeline_task and not session.pipeline_task.done():
                                session.pipeline_task.cancel()
                            if session.filler_task and not session.filler_task.done():
                                session.filler_task.cancel()

                            # Transition back to LISTENING
                            listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="transcript_timeout")
                            await self._execute_side_effects(session, listening_side_effects, output_language)
                            await self._broadcast_state(session, State.LISTENING)
                    except asyncio.CancelledError:
                        pass  # Transcript arrived, timeout cancelled

                # Store timeout task (will be cancelled when transcript arrives)
                session.transcript_timeout_task = asyncio.create_task(transcript_timeout())
        
        elif signal_type == "SPEECH_START":
            # INTERRUPTION: Abort mission if user starts speaking
            if session.mode == "visual-copilot" and session.is_mission_active:
                logger.info(f"[{session.session_id}] ⚡ User interrupted mission.")
                session.is_mission_active = False
                session.current_goal = None

            # BARGE-IN DETECTION: Defer interrupt on speech start (wait for STT validation)
            state_mgr = session.state_manager
            if state_mgr.state == State.SPEAKING and not self._is_bargin_enabled():
                session.barge_in_pending = False
                logger.debug(f"[{session.session_id}] ⏸️ Ignoring SPEECH_START during SPEAKING (BARGIN_FEATURE=false)")
                return
            if state_mgr.state == State.SPEAKING:
                logger.info(f"[{session.session_id}] ⏸️ Potential barge-in (SPEECH_START) - deferring, waiting for STT validation")
                session.barge_in_pending = True
                await self._send_json(session.websocket, {"type": "playback_stop", "timestamp": time.time()}, session)
            elif state_mgr.state == State.THINKING:
                if session.barge_in_pending:
                    logger.debug(f"[{session.session_id}] 🎤 Speech start during pending barge-in validation - waiting for STT")
                else:
                    # If we were thinking/waiting for a transcript and user starts speaking again,
                    # return to LISTENING to capture the new turn properly.
                    logger.info(f"[{session.session_id}] 🎤 New speech started during THINKING - returning to LISTENING")
                    if session.transcript_timeout_task and not session.transcript_timeout_task.done():
                        session.transcript_timeout_task.cancel()

                    await state_mgr.transition(State.LISTENING, trigger="barge_in_during_thinking")
                    await self._broadcast_state(session, State.LISTENING)
            
            # Reset pending transcript flag and clear partial transcript
            session.pending_transcript = False
            session.partial_transcript = ""
            logger.debug(f"[{session.session_id}] 🎤 Speech started - resetting transcript tracking")
        
        elif signal_type == "TRANSCRIPT_REJECTED" or event_type == "transcript_rejected":
            # Handle transcript rejection - transition back to LISTENING if we're waiting
            state_mgr = session.state_manager
            reason = data.get("reason", "unknown")
            rejected_text = data.get("rejected_text", "")
            
            logger.warning(f"[{session.session_id}] 🚫 Transcript rejected: '{rejected_text}' (reason: {reason})")
            
            # If we're in THINKING waiting for transcript, transition back to LISTENING
            if state_mgr.state == State.THINKING and session.pending_transcript:
                logger.info(f"[{session.session_id}] 🔄 Transcript rejected - Transitioning back to LISTENING")
                
                # Cancel transcript timeout task
                if session.transcript_timeout_task and not session.transcript_timeout_task.done():
                    session.transcript_timeout_task.cancel()
                    try:
                        await asyncio.wait_for(session.transcript_timeout_task, timeout=0.1)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                
                # Cancel any tasks
                if session.pipeline_task and not session.pipeline_task.done():
                    session.pipeline_task.cancel()
                    try:
                        await asyncio.wait_for(session.pipeline_task, timeout=0.1)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                if session.filler_task and not session.filler_task.done():
                    session.filler_task.cancel()
                    try:
                        await asyncio.wait_for(session.filler_task, timeout=0.1)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                
                # Reset flags
                session.pending_transcript = False
                session.partial_transcript = ""
                
                # Transition back to LISTENING
                output_language = self._get_output_language(session.current_language, session)
                listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="transcript_rejected")
                await self._execute_side_effects(session, listening_side_effects, output_language)
                await self._broadcast_state(session, State.LISTENING)
    
    async def _execute_side_effects(self, session: OrchestratorSession, side_effects: list, language: str):
        """
        Execute side effects returned from state transition.
        
        Args:
            session: Orchestrator session
            side_effects: List of side effect names (e.g., ["play_immediate_filler"])
            language: Current language code
        """
        for effect in side_effects:
            if effect == "play_immediate_filler":
                # Check if fillers are enabled in config
                if not self.config.performance.enable_fillers:
                    logger.debug(f"[{session.session_id}] 🔇 Suppressing immediate filler (disabled in config)")
                    continue

                # Play immediate filler IMMEDIATELY when THINKING state is entered (no delay)
                async def play_immediate_filler_now():
                    try:
                        # Check state immediately (no delay)
                        if session.is_closed or session.state_manager.state != State.THINKING:
                            return  # Session closed or state changed

                        if self.dialogue_manager:
                            # Use output language that respects session language override
                            output_language = self._get_output_language(language, session)
                            immediate_filler = self.dialogue_manager.get_immediate_filler(output_language)
                            if immediate_filler:
                                logger.info(f"[{session.session_id}] 💭 Playing immediate filler (immediately on THINKING): {immediate_filler.text[:50]}...")
                                try:
                                    # Use current task as cancellation_task to identify this as a filler
                                    # This prevents fillers from spawning server-side playback timers
                                    current_task = asyncio.current_task()
                                    if immediate_filler.has_audio():
                                        logger.debug(f"[{session.session_id}] 💿 Using pre-generated immediate filler: {immediate_filler.audio_path}")
                                        await self._stream_audio_file(session, immediate_filler.audio_path, cancellation_task=current_task)
                                    elif immediate_filler.text:
                                        logger.debug(f"[{session.session_id}] 🔊 Generating immediate filler via TTS: {immediate_filler.text[:50]}...")
                                        await self._stream_tts_to_browser(session, immediate_filler.text, output_language, cancellation_task=current_task)
                                except Exception as e:
                                    logger.debug(f"[{session.session_id}] Immediate filler error (non-critical): {e}")
                    except asyncio.CancelledError:
                        logger.debug(f"[{session.session_id}] Immediate filler cancelled")

                # Cancel any existing filler task before starting immediate filler
                if session.filler_task and not session.filler_task.done():
                    session.filler_task.cancel()
                    try:
                        await asyncio.wait_for(session.filler_task, timeout=0.1)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass

                # Start the immediate filler task (no delay)
                session.filler_task = asyncio.create_task(play_immediate_filler_now())
                session.immediate_filler_played = True  # Mark immediate filler as played
            
            elif effect == "cancel_filler":
                # Cancel any running filler task
                if session.filler_task and not session.filler_task.done():
                    session.filler_task.cancel()
                    try:
                        await session.filler_task
                    except asyncio.CancelledError:
                        pass
                    logger.debug(f"[{session.session_id}] ⏸️ Cancelled filler (side effect)")
            
            elif effect == "cancel_tts":
                # Cancel any running TTS task
                if session.tts_task and not session.tts_task.done():
                    session.tts_task.cancel()
                    try:
                        await session.tts_task
                    except asyncio.CancelledError:
                        pass
                    logger.debug(f"[{session.session_id}] ⏸️ Cancelled TTS (side effect)")

            elif effect == "play_post_response_prompt":
                # Play post-response prompt after a delay (e.g., "Do you need anything else?")
                # Only play for transitions from SPEAKING to LISTENING (completed responses), not error recovery
                transition_trigger = getattr(session.state_manager, 'last_transition_trigger', None)
                if transition_trigger in ["playback_done", "playback_done_server_timer", "playback_watchdog_timeout", "playback_done_auto"]:
                    async def play_post_response_prompt():
                        try:
                            # Wait 2 seconds before playing the prompt
                            await asyncio.sleep(2.0)

                            # Check if still in LISTENING state (user hasn't interrupted)
                            if session.is_closed or session.state_manager.state != State.LISTENING:
                                logger.debug(f"[{session.session_id}] ⏸️ Skipping post-response prompt - state changed to {session.state_manager.state.value}")
                                return

                            if self.dialogue_manager:
                                post_response_asset = self.dialogue_manager.get_post_response_prompt(language)
                                if post_response_asset:
                                    logger.info(f"[{session.session_id}] 💬 Playing post-response prompt: {post_response_asset.text[:50]}...")

                                    # Transition to SPEAKING for the prompt
                                    speaking_side_effects = await session.state_manager.transition(
                                        State.SPEAKING,
                                        trigger="post_response_prompt",
                                        data={"response": post_response_asset.text}
                                    )
                                    await self._execute_side_effects(session, speaking_side_effects, language)
                                    await self._broadcast_state(session, State.SPEAKING)

                                    # Play the prompt (prefer audio file if available, otherwise TTS)
                                    if post_response_asset.has_audio():
                                        logger.info(f"[{session.session_id}] 💿 Using pre-generated audio for post-response: {post_response_asset.audio_path}")
                                        await self._stream_audio_file(session, post_response_asset.audio_path, cancellation_task=asyncio.current_task())
                                    else:
                                        logger.info(f"[{session.session_id}] 🔊 Generating post-response via TTS: {post_response_asset.text[:50]}...")
                                        await self._stream_tts_to_browser(session, post_response_asset.text, language, cancellation_task=asyncio.current_task())

                                    # Transition back to LISTENING after prompt completes
                                    listening_side_effects = await session.state_manager.transition(State.LISTENING, trigger="post_response_complete")
                                    await self._execute_side_effects(session, listening_side_effects, language)
                                    await self._broadcast_state(session, State.LISTENING)

                        except asyncio.CancelledError:
                            logger.debug(f"[{session.session_id}] Post-response prompt cancelled")
                        except Exception as e:
                            logger.error(f"[{session.session_id}] Post-response prompt error: {e}")

                    # Start the post-response prompt task
                    asyncio.create_task(play_post_response_prompt())
                else:
                    logger.debug(f"[{session.session_id}] Skipping post-response prompt - trigger was '{transition_trigger}' (not a completed response)")
    
    async def _process_user_input(self, session: OrchestratorSession, text: str, language: str):
        """Process final STT result (Wrapper with semaphore)"""
        async with session.task_semaphore:
            await self._process_user_input_impl(session, text, language)

    async def _process_user_input_impl(self, session: OrchestratorSession, text: str, language: str):
        """Process final STT result"""
        # Check if session is closed
        if session.is_closed:
            logger.debug(f"[{session.session_id}] Session closed, skipping user input processing")
            return
        
        state_mgr = session.state_manager
        
        if not text.strip():
            logger.warning(f"[{session.session_id}] ⚠️ Empty text in _process_user_input, skipping")
            return

        # Get conversation history BEFORE adding current turn (excludes current question)
        history_context = session.history_manager.get_context_window(max_turns=5)
        logger.debug(f"[{session.session_id}] 📜 History context ({len(history_context)} chars): {history_context[:100]}{'...' if len(history_context) > 100 else ''}")

        # Add user turn to conversation history
        session.history_manager.add_user_turn(text, metadata={"language": language})

        # Save last query for noise recovery (re-trigger after false barge-in)
        session.last_query_text = text
        session.last_query_language = language

        # NEW: Check for escalation
        if session.history_manager.should_escalate():
            logger.warning(
                f"[{session.session_id}] Session should be escalated: "
                f"{session.history_manager.interaction_count} interactions "
                f"(max: {session.history_manager.max_turns})"
            )
            # Optional: Set escalation flag
            session.history_manager.is_escalated = True

        # Update last_activity when processing starts (important for timeout monitor)
        session.last_activity = time.time()
        session.timeout_count = 0  # Reset timeout count on new input
        session.immediate_filler_played = False  # Reset filler flag for new turn

        # Clear pending transcript flags (we're processing the final transcript now)
        session.pending_transcript = False
        session.partial_transcript = ""

        # Determine output language based on stream_out setting (with session override check)
        output_language = self._get_output_language(language, session)
        logger.debug(f"[{session.session_id}] 🌐 Input language: {language}, Output language: {output_language}")
        
        # Check for exit keywords (use detected language for keyword matching)
        if self.dialogue_manager.check_exit_keywords(text, language):
            logger.info(f"[{session.session_id}] 🚪 EXIT DETECTED: User said '{text}'")
            await self._handle_end_session(session, output_language)
            return
        
        # =====================================================================
        # LANGUAGE SWITCH DETECTION: Check if user wants to change language
        # =====================================================================
        text_lower = text.lower().strip()
        detected_lang_switch = self._detect_language_switch_command(text_lower)
        
        if detected_lang_switch:
            new_lang = detected_lang_switch
            old_lang = session.stream_out_override or self.config.languages.stream_out
            
            # Update session language override
            session.stream_out_override = new_lang
            output_language = new_lang  # Update for this request too
            
            logger.info(f"[{session.session_id}] 🌐 LANGUAGE SWITCH DETECTED: '{text}' → switching from {old_lang} to {new_lang}")
            
            # Notify client of language change
            await self._send_json(session.websocket, {
                "type": "language_changed",
                "language": new_lang,
                "message": f"Language switched to {new_lang.upper()}"
            }, session)
            
            # Continue processing - the RAG will respond in the new language
        
        # =====================================================================
        # FSM ROUTING: Check for appointment booking or active FSM session
        # =====================================================================
        APPOINTMENT_TRIGGERS = [
            # Booking phrases
            "book appointment", "book an appointment", "book a call",
            "schedule appointment", "schedule an appointment", "schedule a call",
            "schedule meeting", "schedule a meeting",
            # Expert/human contact
            "talk to expert", "talk to an expert", "talk to a human",
            "speak to someone", "speak with someone", "speak to an expert",
            "contact expert", "need an expert", "get in touch",
            # Callback requests
            "callback", "call me back", "call back",
            # Direct requests
            "make an appointment", "set up a meeting", "arrange a call"
        ]
        
        # Check if FSM is active OR if user wants to start appointment flow
        if session.fsm_active and session.appointment_fsm:
            # ---- ACTIVE FSM: Route input via RAG FSM router ----
            logger.info(f"[{session.session_id}] 📅 FSM ACTIVE: Routing via RAG FSM router")
            
            # Build FSM context for router
            fsm_context = {
                "active": True,
                "pending_field": self._get_fsm_pending_field(session.appointment_fsm),
                "collected_data": session.appointment_fsm.data.to_dict() if session.appointment_fsm.data else {},
                "retry_counts": session.appointment_fsm.retry_counts,
                "schema": self._get_fsm_schema()
            }
            
            # Call RAG FSM router
            rag_client = RAGClient(self.config.services.rag, skip_ssl=self.config.server.skip_ssl_verify)
            route_result = await rag_client.route_fsm_turn(
                user_text=text,
                session_id=session.session_id,
                tenant_id=session.tenant_id or "tara",
                language=output_language,
                fsm_context=fsm_context
            )
            
            action = route_result.get("action", "invalid_retry")
            cancelled = route_result.get("cancelled", False)
            normalized_value = route_result.get("normalized_value")
            resume_prompt = route_result.get("resume_prompt")
            confidence = route_result.get("confidence", 0.0)
            
            logger.info(f"[{session.session_id}] 🔀 FSM ROUTE RESULT: action={action}, confidence={confidence:.2f}, cancelled={cancelled}")
            
            # Branch on action
            if action == "cancel" or cancelled:
                # Cancel FSM immediately
                session.fsm_active = False
                session.appointment_fsm = None
                logger.info(f"[{session.session_id}] 📅 FSM CANCELLED via router")
                
                cancel_response = "No problem! If you'd like to book an appointment later, just let me know. Feel free to ask me anything else!"
                await self._stream_fsm_response(session, cancel_response, output_language)
                return
            
            elif action in ("collect_field", "confirm_field"):
                # Feed normalized value into FSM
                field = route_result.get("field")
                logger.info(f"[{session.session_id}] 📝 FSM FIELD ANSWER: field={field}, value='{normalized_value[:30] if normalized_value else None}...'")
                if field:
                    self._reset_fsm_detour_count(session, field)
                
                # Process the normalized value through FSM
                fsm_result = session.appointment_fsm.process_input(normalized_value or text)
                
                # Check if FSM is complete or cancelled
                if fsm_result.get("complete") or fsm_result.get("cancelled"):
                    session.fsm_active = False
                    session.appointment_fsm = None
                    status = 'COMPLETE' if fsm_result.get('complete') else 'CANCELLED'
                    logger.info(f"[{session.session_id}] 📅 FSM {status}: Returning to RAG mode")
                    
                    if fsm_result.get("complete") and fsm_result.get("data"):
                        appointment_data = fsm_result.get("data")
                        logger.info(f"[{session.session_id}] 📧 Sending appointment_complete for email confirmation")
                        await self._send_json(session.websocket, {
                            "type": "appointment_complete",
                            "appointment_data": appointment_data,
                            "timestamp": time.time()
                        }, session)
                
                fsm_response = fsm_result.get("response", "")
                if fsm_response:
                    await self._stream_fsm_response(session, fsm_response, output_language)
                return
            
            elif action == "detour_rag":
                # Detour to RAG for general question, then resume FSM
                pending_field = fsm_context.get("pending_field") or "unknown"
                if not self.config.fsm.allow_rag_detour:
                    logger.info(f"[{session.session_id}] 🚫 FSM DETOUR BLOCKED: allow_rag_detour=false")
                    fsm_result = session.appointment_fsm.process_input(text)
                    fsm_response = fsm_result.get("response", "")
                    if fsm_response:
                        await self._stream_fsm_response(session, fsm_response, output_language)
                    return
                if not self._can_take_fsm_detour(session, pending_field):
                    logger.info(
                        f"[{session.session_id}] 🚫 FSM DETOUR LIMIT reached for field={pending_field} "
                        f"(max={self.config.fsm.max_detours_per_field})"
                    )
                    fsm_result = session.appointment_fsm.process_input(text)
                    fsm_response = fsm_result.get("response", "")
                    if fsm_response:
                        await self._stream_fsm_response(session, fsm_response, output_language)
                    return
                self._increment_fsm_detour_count(session, pending_field)
                logger.info(f"[{session.session_id}] 🔄 FSM DETOUR: Answering via RAG, then resuming")
                
                # Process via normal RAG pipeline
                await self._process_rag_detour_and_resume(session, text, output_language, resume_prompt)
                return
            
            elif action == "invalid_retry":
                # Invalid input - use FSM retry logic
                logger.warning(f"[{session.session_id}] ⚠️ FSM INVALID RETRY: {route_result.get('reason', '')}")
                
                fsm_result = session.appointment_fsm.process_input(text)
                fsm_response = fsm_result.get("response", "")
                if fsm_response:
                    await self._stream_fsm_response(session, fsm_response, output_language)
                return
        
        elif any(trigger in text.lower() for trigger in APPOINTMENT_TRIGGERS):
            # ---- START FSM: User wants to book appointment ----
            logger.info(f"[{session.session_id}] 📅 APPOINTMENT TRIGGER DETECTED: Starting FSM")
            session.metadata["fsm_detour_counts"] = {}
            session.appointment_fsm = SimpleAppointmentFSM(schema=self._get_fsm_schema())
            session.fsm_active = True
            
            # Get initial FSM response (greeting)
            fsm_result = session.appointment_fsm.process_input("")  # Empty input triggers INIT
            fsm_response = fsm_result.get("response", "")
            
            if fsm_response:
                await self._stream_fsm_response(session, fsm_response, output_language)
            return
        # =====================================================================
        
        # Transition to THINKING and get side effects
        # Note: If already in THINKING (from early speech_end transition), skip transition but still process
        current_state = state_mgr.state
        if current_state != State.THINKING:
            logger.debug(f"[{session.session_id}] 🔄 THINKING: Processing user input '{text[:50]}...' (transitioning from {current_state.value})")
            side_effects = await state_mgr.transition(State.THINKING, trigger="stt_final", data={"language": language})
            await self._broadcast_state(session, State.THINKING)
            
            # Cancel any ongoing tasks
            if session.tts_task and not session.tts_task.done():
                session.tts_task.cancel()
            if session.pipeline_task and not session.pipeline_task.done():
                session.pipeline_task.cancel()
            if session.filler_task and not session.filler_task.done():
                session.filler_task.cancel()
            
            # Execute side effects (including immediate filler)
            await self._execute_side_effects(session, side_effects, output_language)
        else:
            logger.debug(f"[{session.session_id}] 🔄 THINKING: Processing user input '{text[:50]}...' (already in THINKING from early transition)")
            # Already in THINKING - side effects (immediate filler) already executed during early transition
            # Just cancel any conflicting tasks
            if session.pipeline_task and not session.pipeline_task.done():
                session.pipeline_task.cancel()
                try:
                    await asyncio.wait_for(session.pipeline_task, timeout=0.1)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        
        try:
            # Start pipeline processing (RAG/LLM) with global timeout protection
            GLOBAL_PIPELINE_TIMEOUT = 30.0  # 30 second max for entire pipeline
            logger.debug(f"[{session.session_id}] 🚀 Creating pipeline task for query: '{text}' (timeout={GLOBAL_PIPELINE_TIMEOUT}s)")
            
            async def pipeline_with_timeout():
                try:
                    if session.mode == "visual-copilot":
                        orchestration_coro = self._start_mission(session, text)
                    else:
                        orchestration_coro = self._stream_response_and_tts(session, text, language, history_context)
                    
                    await asyncio.wait_for(
                        orchestration_coro,
                        timeout=GLOBAL_PIPELINE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[{session.session_id}] ⏰ GLOBAL PIPELINE TIMEOUT ({GLOBAL_PIPELINE_TIMEOUT}s) - forcing recovery")
                    # Send error to browser
                    await self._send_json(session.websocket, {
                        "type": "error",
                        "error": "Request timed out - please try again",
                        "timestamp": time.time()
                    }, session)
                    # Force transition back to LISTENING
                    if state_mgr.state != State.LISTENING:
                        listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="pipeline_timeout")
                        await self._execute_side_effects(session, listening_side_effects, output_language)
                        await self._broadcast_state(session, State.LISTENING)
            
            session.pipeline_task = asyncio.create_task(pipeline_with_timeout())
            logger.debug(f"[{session.session_id}] ✅ Pipeline task created with timeout protection")
            
            # 3) Play latency filler AFTER a delay (only if still in THINKING state)
            # Use output language for fillers (already calculated above)
            if self.config.performance.enable_fillers and self.dialogue_manager:
                latency_filler = self.dialogue_manager.get_latency_filler(output_language)
                
                if latency_filler:
                    # Extract delay from trigger field (e.g., "delay_ms:1500" -> 1500ms)
                    delay_ms = 1500  # Default delay
                    if latency_filler.trigger:
                        trigger_str = latency_filler.trigger.lower()
                        if "delay_ms:" in trigger_str:
                            try:
                                delay_ms = int(trigger_str.split("delay_ms:")[1].strip())
                            except (ValueError, IndexError):
                                delay_ms = 1500
                    
                    # Schedule filler to play after delay (only if still in THINKING)
                    async def delayed_filler():
                        try:
                            # Wait for the specified delay
                            await asyncio.sleep(delay_ms / 1000.0)
                            
                            # CRITICAL: Only play if still in THINKING state
                            if state_mgr.state != State.THINKING:
                                logger.debug(f"[{session.session_id}] ⏸️ Skipping latency filler - state changed to {state_mgr.state.value}")
                                return
                            
                            # Skip if immediate filler was just played (prevent double filler)
                            if session.immediate_filler_played:
                                logger.debug(f"[{session.session_id}] ⏸️ Skipping latency filler - immediate filler already played")
                                return
                            
                            # Verify we're still in THINKING before playing
                            if state_mgr.state == State.THINKING:
                                logger.info(f"[{session.session_id}] 💭 Playing latency filler in THINKING state (after {delay_ms}ms delay)")
                                
                                try:
                                    # Check state before playback
                                    if state_mgr.state != State.THINKING:
                                        logger.debug(f"[{session.session_id}] ⏸️ Filler cancelled - state changed before playback")
                                        return
                                    
                                    if latency_filler.has_audio():
                                        logger.info(f"[{session.session_id}] 💿 Using pre-generated latency filler: {latency_filler.audio_path}")
                                        await self._stream_audio_file(session, latency_filler.audio_path, cancellation_task=session.filler_task)
                                    elif latency_filler.text:
                                        logger.info(f"[{session.session_id}] 🔊 Generating latency filler via TTS: {latency_filler.text[:50]}...")
                                        await self._stream_tts_to_browser_with_cancellation(session, latency_filler.text, output_language, session.filler_task)
                                    
                                    # Verify we're still in THINKING after filler completes
                                    if state_mgr.state == State.THINKING:
                                        logger.debug(f"[{session.session_id}] 💭 Filler completed, still in THINKING (waiting for RAG response)")
                                    else:
                                        logger.debug(f"[{session.session_id}] 💭 Filler completed, state changed to {state_mgr.state.value}")
                                except asyncio.CancelledError:
                                    logger.debug(f"[{session.session_id}] ⏸️ Filler interrupted during playback")
                                    raise
                            else:
                                logger.debug(f"[{session.session_id}] ⏸️ Filler cancelled - state is {state_mgr.state.value}, not THINKING")
                        except asyncio.CancelledError:
                            logger.debug(f"[{session.session_id}] ⏸️ Filler task cancelled")
                        except Exception as e:
                            logger.debug(f"[{session.session_id}] Filler playback error (non-critical): {e}")
                    
                    session.filler_task = asyncio.create_task(delayed_filler())
            
            # Wait for main pipeline to complete
            await session.pipeline_task
            
            # Cancel filler if it's still running (response is ready, no need for filler)
            if session.filler_task and not session.filler_task.done():
                session.filler_task.cancel()
                try:
                    await session.filler_task
                except asyncio.CancelledError:
                    pass
                logger.debug(f"[{session.session_id}] ⏸️ Cancelled latency filler - response ready")
        
        except asyncio.CancelledError:
            logger.info(f"[{session.session_id}] Processing cancelled - recovering to LISTENING state")
            # Cancel filler if still running
            if session.filler_task and not session.filler_task.done():
                session.filler_task.cancel()
                try:
                    await session.filler_task
                except asyncio.CancelledError:
                    pass

            # CRITICAL: Recover to LISTENING state so STT can resume accepting input
            if state_mgr.state != State.LISTENING:
                logger.info(f"[{session.session_id}] 🔄 Recovering to LISTENING state after pipeline cancellation")
                recovery_side_effects = await state_mgr.transition(State.LISTENING, trigger="pipeline_cancelled")
                await self._execute_side_effects(session, recovery_side_effects, output_language)
                await self._broadcast_state(session, State.LISTENING)
        except Exception as e:
            logger.error(f"[{session.session_id}] Processing error: {e}", exc_info=True)
            # Cancel filler if still running
            if session.filler_task and not session.filler_task.done():
                session.filler_task.cancel()
                try:
                    await session.filler_task
                except asyncio.CancelledError:
                    pass
            
            # Reset transcript tracking flags on error
            session.pending_transcript = False
            session.partial_transcript = ""
            
            # Cancel transcript timeout task if running
            if session.transcript_timeout_task and not session.transcript_timeout_task.done():
                session.transcript_timeout_task.cancel()
                try:
                    await asyncio.wait_for(session.transcript_timeout_task, timeout=0.1)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            
            # ATTEMPT FALLBACK: Speak error message to user
            try:
                error_text = "I'm sorry, I encountered an error. could you please say that again?"
                if language == "de":
                    error_text = "Entschuldigung, es ist ein Fehler aufgetreten. Könnten Sie das bitte wiederholen?"
                
                logger.info(f"[{session.session_id}] 🔊 Speaking service error message: '{error_text}'")
                await self._stream_tts_to_browser(session, error_text, language)
            except Exception as e2:
                logger.error(f"[{session.session_id}] Failed to speak error message: {e2}")

            error_side_effects = await state_mgr.transition(State.LISTENING, trigger="error")
            await self._execute_side_effects(session, error_side_effects, output_language)
            await self._broadcast_state(session, State.LISTENING)
    
    async def _stream_fsm_response(self, session: OrchestratorSession, response_text: str, language: str):
        """
        Stream FSM response to browser and TTS.
        
        This handles appointment booking responses separately from RAG responses.
        Uses _stream_tts_to_browser (queue-based) to avoid concurrent receive_audio() calls.
        """
        if session.is_closed:
            return
        
        state_mgr = session.state_manager
        output_language = self._get_output_language(language, session)

        logger.info(f"[{session.session_id}] 📅 FSM Response: '{response_text[:100]}...'")
        
        try:
            # Transition to THINKING
            if state_mgr.state != State.THINKING:
                side_effects = await state_mgr.transition(State.THINKING, trigger="fsm_response")
                await self._broadcast_state(session, State.THINKING)
            
            # Send full response text to browser
            await self._send_json(session.websocket, {
                "type": "agent_response",
                "text": response_text,
                "language": output_language,
                "is_streaming": False,
                "is_fsm": True,
                "timestamp": time.time()
            }, session)
            
            # Add to history
            session.history_manager.add_agent_turn(response_text, metadata={"source": "fsm"})
            
            # Use _stream_tts_to_browser which correctly reads from tts_audio_queue
            # (populated by _tts_receive_loop — the ONLY consumer of receive_audio())
            # This avoids the "Concurrent call to receive()" error.
            await self._stream_tts_to_browser(session, response_text, output_language)
            
            # Transition to LISTENING
            if state_mgr.state != State.LISTENING:
                listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="fsm_complete")
                await self._broadcast_state(session, State.LISTENING)
                
        except Exception as e:
            logger.error(f"[{session.session_id}] FSM response error: {e}", exc_info=True)
            # Ensure we return to LISTENING state
            if state_mgr.state != State.LISTENING:
                await state_mgr.transition(State.LISTENING, trigger="fsm_error")
                await self._broadcast_state(session, State.LISTENING)
    
    async def _stream_response_and_tts(self, session: OrchestratorSession, user_text: str, language: str, history_context: str = ""):
        """Stream RAG response and TTS audio"""
        # Verify state before starting
        if not session.verify_state(State.THINKING, "TTS streaming"):
            logger.warning(f"[{session.session_id}] Invalid state for TTS streaming, aborting")
            return

        logger.debug(f"[{session.session_id}] 🎯 STARTING _stream_response_and_tts for query: '{user_text}'")
        state_mgr = session.state_manager
        response_text = ""
        output_language = self._get_output_language(language, session)  # Get output language with session override
        transitioned_to_speaking = False
        
        # CRITICAL: Reset audio duration tracking for this new response
        session.tts_total_bytes = 0
        self._clear_playback_tracking(session)
        
        # Also cancel any existing playback timer that might cut off this stream
        if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
            session.audio_playback_server_timer.cancel()
            logger.debug(f"[{session.session_id}] ⏸️ Cancelled existing playback timer for new RAG stream")

        try:
            # Connect to TTS service for streaming (avoid double connection)
            # TTS connection is handled by _stt_receive_loop or on-demand, don't reconnect here
            # if not session.tts_client.ws or session.tts_client.ws.closed:
            #     await session.tts_client.connect(session.session_id)
            
            # Ensure TTS connection exists before streaming
            if not session.tts_client.ws or session.tts_client.ws.closed:
                logger.warning(f"[{session.session_id}] TTS WebSocket not connected, attempting connection...")
                connected = await session.tts_client.connect(session.session_id)
                if not connected:
                    logger.error(f"[{session.session_id}] Failed to connect to TTS service - aborting stream")
                    # Transition back to LISTENING state gracefully
                    if state_mgr.state != State.LISTENING:
                        listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="tts_connection_failed")
                        await self._execute_side_effects(session, listening_side_effects, output_language)
                        await self._broadcast_state(session, State.LISTENING)
                    return

            # Get form data from history manager
            form_data = session.history_manager.form_data if session.history_manager.form_data else None

            # Stream from pipeline and forward to TTS (STAY IN THINKING STATE)
            # Use output_language (which respects session.stream_out_override) for RAG response language
            async for token_data in self.pipeline.process_query(
                query=user_text,
                session_id=session.session_id,
                user_id=session.user_id,  # Pass user_id for Hive Mind
                agent_name=session.agent_name,
                language=output_language,  # Use output language for RAG response
                history_context=history_context,
                form_data=form_data,
                rag_url=session.rag_url,
                tenant_id=session.tenant_id
            ):
                # Check if session is closed (client disconnected)
                if session.is_closed:
                    logger.info(f"[{session.session_id}] ⏸️ Session closed, stopping RAG streaming")
                    break

                token = token_data.get("token", "")

                # Skip empty tokens (RAG sends empty token to signal completion)
                if not token:
                    # Capture LLM usage metadata from the final chunk
                    llm_usage = token_data.get("llm_usage", {})
                    if llm_usage:
                        session.total_prompt_tokens += llm_usage.get("prompt_tokens", 0)
                        session.total_completion_tokens += llm_usage.get("completion_tokens", 0)
                        session.llm_model = llm_usage.get("model", session.llm_model)
                        logger.info(f"[{session.session_id}] 📊 LLM usage this turn: {llm_usage.get('prompt_tokens', 0)}p/{llm_usage.get('completion_tokens', 0)}c | Cumulative: {session.total_prompt_tokens}p/{session.total_completion_tokens}c ({session.llm_model})")
                    logger.debug(f"[{session.session_id}] ⏸️ Skipping empty RAG token (is_final: {token_data.get('is_final', False)})")
                    continue

                # Record first token time for TTFT
                if "first_token" not in session.current_turn_timers:
                    session.current_turn_timers["first_token"] = time.time()
                    ttft = (session.current_turn_timers["first_token"] - session.current_turn_timers.get("speech_end", time.time())) * 1000
                    logger.info(f"[{session.session_id}] ⚡ TTFT: {ttft:.1f}ms")

                logger.debug(f"[{session.session_id}] 📝 RAG token: '{token}' (len={len(token)})")

                # Send non-empty tokens to TTS
                response_text += token

                # Send text chunk to browser (skip if closed)
                await self._send_json(session.websocket, {
                    "type": "agent_response",
                    "text": token,
                    "language": output_language,
                    "is_streaming": True,
                    "timestamp": time.time()
                }, session)

                # Stream chunk to TTS-LABS
                logger.debug(f"[{session.session_id}] 🎵 Sending to TTS: '{token}' (Voice: {session.voice_id_override})")
                
                # CRITICAL: If this is the first token, cancel any existing fillers aggressively
                if not transitioned_to_speaking:
                    if session.filler_task and not session.filler_task.done():
                        session.filler_task.cancel()
                        logger.debug(f"[{session.session_id}] ⏸️ Cancelled filler task on first text token")
                
                await session.tts_client.stream_chunk(token, output_language, emotion="helpful", voice_id=session.voice_id_override, pronunciation_dict_id=self.config.agent.pronunciation_dict_id)

            # Cancel latency fillers since RAG response is complete and TTS is about to start
            if session.filler_task and not session.filler_task.done():
                session.filler_task.cancel()
                try:
                    await asyncio.wait_for(session.filler_task, timeout=0.5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                logger.debug(f"[{session.session_id}] ⏸️ Cancelled latency filler - RAG response ready for TTS")

            # Track TTS streamed chars for this turn
            session.tts_streamed_chars += len(response_text)
            tts_stream_start = time.time()

            # Signal end of text stream to TTS (triggers EOS to ElevenLabs)
            if not session.is_closed:
                logger.info(f"[{session.session_id}] 🔚 Sending stream_end to TTS (response: {len(response_text)} chars | Cumulative TTS chars: {session.tts_streamed_chars})")
                await session.tts_client.stream_end()
            else:
                logger.info(f"[{session.session_id}] ⏸️ Session closed, skipping stream_end")
                return

            # Signal text complete
            await self._send_json(session.websocket, {
                "type": "agent_response",
                "text": "",
                "language": output_language,
                "is_streaming": False,
                "is_complete": True,
                "full_text": response_text,
                "timestamp": time.time()
            }, session)

            # Archive turn metrics
            if "speech_end" in session.current_turn_timers:
                timers = session.current_turn_timers
                now = time.time()
                metrics = {
                    "timestamp": timers["speech_end"],
                    "user_text": timers.get("user_text", ""),
                    "agent_text": response_text,
                    "ttft_ms": (timers["first_token"] - timers["speech_end"]) * 1000 if "first_token" in timers else None,
                    "ttfc_ms": (timers["first_audio"] - timers["speech_end"]) * 1000 if "first_audio" in timers else None,
                    "total_turn_ms": (now - timers["speech_end"]) * 1000
                }
                session.turn_metrics.append(metrics)
                # Log with safety check for None values
                ttft_str = f"{metrics['ttft_ms']:.0f}ms" if metrics['ttft_ms'] is not None else "N/A"
                ttfc_str = f"{metrics['ttfc_ms']:.0f}ms" if metrics['ttfc_ms'] is not None else "N/A"
                logger.info(f"[{session.session_id}] 📊 Turn complete: TTFT={ttft_str}, TTFC={ttfc_str}")

            # Add agent response to conversation history
            if response_text:
                session.history_manager.add_agent_turn(response_text, metadata={
                    "language": output_language,
                    "streaming": True
                })

            # Receive and forward TTS audio chunks with timeout protection
            chunk_counter = 0
            tts_receive_timeout = 25.0  # Increased timeout
            tts_start_time = time.time()
            
            # Clear any stale data from tts_audio_queue before starting fresh
            while not session.tts_audio_queue.empty():
                try:
                    session.tts_audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            try:
                # Consume from the background-filled queue
                while not session.is_closed:
                    # Check timeout - prevent infinite blocking
                    if time.time() - tts_start_time > tts_receive_timeout:
                        logger.warning(f"[{session.session_id}] ⏰ TTS queue receive timeout ({tts_receive_timeout}s) - breaking out of audio loop")
                        break
                        
                    try:
                        # Wait for audio data from background task queue
                        data = await asyncio.wait_for(
                            session.tts_audio_queue.get(), 
                            timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        # Check if session is closed or connection lost
                        if session.is_closed:
                            break
                        continue

                    msg_type = data.get("type")
                    
                    # Capture and store format configuration from TTS service
                    if msg_type == "connected":
                        session.tts_sample_rate = data.get("sample_rate", session.tts_sample_rate)
                        session.tts_format = data.get("format", session.tts_format)
                        logger.info(f"[{session.session_id}] 📨 TTS connected: {session.tts_sample_rate}Hz, {session.tts_format}")
                        continue
                    
                    # Skip other non-audio messages
                    if msg_type in ("prewarmed", "sentence_start", "sentence_playing", "pong", "timeout"):
                        logger.debug(f"[{session.session_id}] 📨 TTS control message: {msg_type}")
                        continue
                    
                    if msg_type == "audio":
                        # INTERRUPTION LOGIC: Stop if state is no longer THINKING or SPEAKING
                        # (State might be THINKING during initial buffering before transitioned_to_speaking)
                        if state_mgr.state not in (State.SPEAKING, State.THINKING):
                             logger.info(f"[{session.session_id}] 🛑 Stopping TTS receive loop - state changed to {state_mgr.state.value}")
                             break

                        audio_data = data.get("data", "")
                        if audio_data:
                            # Transition to SPEAKING on first audio chunk
                            if not transitioned_to_speaking:
                                # CRITICAL: Flush any existing filler audio from buffer
                                await self._enqueue_foreground_audio(session, b"", flush=True)
                                
                                logger.info(f"[{session.session_id}] 🔄 SPEAKING: First audio chunk received from TTS-LABS")
                                speaking_side_effects = await state_mgr.transition(State.SPEAKING, trigger="audio_streaming", data={"language": language})
                                await self._execute_side_effects(session, speaking_side_effects, output_language)
                                await self._broadcast_state(session, State.SPEAKING)
                                transitioned_to_speaking = True
                                
                                session.audio_playback_turn_id += 1
                                session.audio_playback_start_time = time.time()
                                if not hasattr(session, 'tts_total_bytes'):
                                    session.tts_total_bytes = 0
                                logger.debug(f"[{session.session_id}] 📊 Tracking TTS playback start")

                                if session.filler_task and not session.filler_task.done():
                                    session.filler_task.cancel()
                                    try:
                                        await asyncio.wait_for(session.filler_task, timeout=0.5)
                                    except (asyncio.CancelledError, asyncio.TimeoutError):
                                        pass

                            if data.get("is_binary"):
                                audio_bytes = audio_data
                            else:
                                audio_bytes = base64.b64decode(audio_data)

                            # Update format info (prefer explicit chunk info, fallback to session settings, then global default)
                            sample_rate = data.get("sample_rate") or session.tts_sample_rate or 44100
                            encoding = data.get("format") or data.get("encoding") or session.tts_format or "pcm_f32le"

                            # Decode audio and buffer
                            await self._enqueue_foreground_audio(session, audio_bytes, sample_rate=sample_rate, encoding=encoding)
                            
                            if not hasattr(session, 'tts_total_bytes'):
                                session.tts_total_bytes = 0
                            session.tts_total_bytes += len(audio_bytes)
                            
                            bytes_per_sample = 4 if "f32" in encoding else 2
                            bytes_per_sec = sample_rate * bytes_per_sample
                            
                            duration_sec = session.tts_total_bytes / bytes_per_sec
                            session.audio_playback_duration = max(duration_sec, 1.0)
                            
                            chunk_counter += 1
                    elif msg_type in ("complete", "stream_complete", "sentence_complete"):
                        logger.debug(f"[{session.session_id}] ✅ TTS stream complete")
                        break
                    elif msg_type == "error":
                        logger.error(f"[{session.session_id}] ❌ TTS error: {data.get('message', 'unknown')}")
                        break
            except Exception as tts_err:
                logger.error(f"[{session.session_id}] ❌ TTS streaming error: {tts_err}")
            
            # Track cumulative TTS time
            try:
                tts_elapsed = (time.time() - tts_stream_start) * 1000
                session.tts_total_time_ms += tts_elapsed
                logger.info(f"[{session.session_id}] 🔊 TTS streaming: {tts_elapsed:.0f}ms this turn | Cumulative: {session.tts_total_time_ms:.0f}ms")
            except NameError:
                pass
            
            # CRITICAL: Handle TTS failure - if no audio received, transition back to LISTENING
            if chunk_counter == 0 and not transitioned_to_speaking:
                logger.error(f"[{session.session_id}] ❌ TTS FAILED - no audio received!")
                logger.error(f"[{session.session_id}]    Response text length: {len(response_text)} chars")
                logger.error(f"[{session.session_id}]    Chunks sent to TTS: {response_text.count(' ') + 1 if response_text else 0} estimated")
                logger.error(f"[{session.session_id}]    Audio chunks received: {chunk_counter}")
                logger.error(f"[{session.session_id}]    TTS connection status: {'connected' if session.tts_client.ws and not session.tts_client.ws.closed else 'disconnected'}")
                
                # Send detailed error notification to browser
                await self._send_json(session.websocket, {
                    "type": "error",
                    "error": "TTS service unavailable - please try again",
                    "error_code": "TTS_NO_AUDIO",
                    "details": {
                        "response_length": len(response_text),
                        "audio_chunks_received": chunk_counter,
                        "tts_connected": session.tts_client.ws is not None and not session.tts_client.ws.closed
                    },
                    "timestamp": time.time()
                }, session)
                
                # Try to play error audio file if available (for better UX)
                if self.dialogue_manager:
                    # Check for error/timeout audio files (reuse timeout prompt as error fallback)
                    error_asset = self.dialogue_manager.get_timeout_prompt(output_language)
                    if error_asset and error_asset.has_audio():
                        logger.info(f"[{session.session_id}] 🔊 Playing error audio: {error_asset.audio_path}")
                        try:
                            # Transition to SPEAKING for error audio
                            if state_mgr.state != State.SPEAKING:
                                speaking_side_effects = await state_mgr.transition(State.SPEAKING, trigger="tts_error_fallback")
                                await self._execute_side_effects(session, speaking_side_effects, output_language)
                                await self._broadcast_state(session, State.SPEAKING)
                            
                            await self._stream_audio_file(session, error_asset.audio_path)
                            
                            # Transition back to LISTENING after error audio
                            listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="error_audio_complete")
                            await self._execute_side_effects(session, listening_side_effects, output_language)
                            await self._broadcast_state(session, State.LISTENING)
                        except Exception as audio_err:
                            logger.error(f"[{session.session_id}] ❌ Failed to play error audio: {audio_err}")
                            # Fall through to direct LISTENING transition
                
                # Transition back to LISTENING (if not already done by error audio)
                if state_mgr.state != State.LISTENING:
                    listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="tts_failed")
                    await self._execute_side_effects(session, listening_side_effects, output_language)
                    await self._broadcast_state(session, State.LISTENING)
                
                return  # Exit early - don't continue processing

            # Flush remaining buffer and signal audio completion (only if still connected)
            if not session.is_closed:
                # Flush any remaining buffered audio (using session settings)
                await self._enqueue_foreground_audio(session, b"", flush=True, sample_rate=session.tts_sample_rate, encoding=session.tts_format)
                
                # Signal audio completion
                if not await self._send_json(session.websocket, {
                    "type": "audio_chunk",
                    "chunk_id": f"{session.session_id}_chunk_{chunk_counter}",
                    "is_final": True,
                    "playback_turn_id": session.audio_playback_turn_id,
                    "timestamp": time.time()
                }, session):
                    logger.info(f"[{session.session_id}] ⏸️ Failed to send final audio chunk - client disconnected")
                
                # Start server-side timer (PRIMARY mechanism for accurate timing)
                if session.audio_playback_duration and session.audio_playback_start_time:
                    # Calculate REMAINING duration (current time minus start time)
                    elapsed = time.time() - session.audio_playback_start_time
                    remaining_sec = max(session.audio_playback_duration - elapsed, 0)
                    
                    logger.debug(f"[{session.session_id}] ⏳ Starting server-side timer for remaining playback: {remaining_sec:.2f}s (elapsed: {elapsed:.2f}s)")
                    await self._start_server_side_playback_timer(session, remaining_sec)
                elif session.audio_playback_duration:
                    # Fallback: start timer with full duration if start time missing
                    await self._start_server_side_playback_timer(session, session.audio_playback_duration)
                else:
                    # Fallback: if duration not tracked, use 1.5 second wait (shorter)
                    logger.warning(f"[{session.session_id}] ⚠️ No duration tracked, using 1.5s fallback")
                    await asyncio.sleep(1.5)
                    if state_mgr.state == State.SPEAKING:
                        logger.info(f"[{session.session_id}] 🔄 Auto-transitioning to LISTENING (fallback)")
                        listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="playback_done_auto")
                        await self._execute_side_effects(session, listening_side_effects, output_language)
                        await self._broadcast_state(session, State.LISTENING)
                
                # Post-response prompt will be played as a side effect of LISTENING state transition
        
        except asyncio.CancelledError:
            logger.info(f"[{session.session_id}] Response streaming cancelled")
            raise

    async def _handle_session_config(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Handle session configuration changes (e.g., switching to visual-copilot)"""
        config_payload = msg.get("config") if isinstance(msg.get("config"), dict) else {}
        effective_msg = {**config_payload, **msg}
        mode = effective_msg.get("mode")

        # Read interaction_mode (interactive/turbo) from client
        interaction_mode = effective_msg.get("interaction_mode", "interactive")
        if interaction_mode in ("interactive", "turbo"):
            session.interaction_mode = interaction_mode
            # In turbo mode, skip TTS audio generation entirely
            if interaction_mode == "turbo":
                session.tts_mode = "text"
                session.speaker_muted = True
            logger.info(f"[{session.session_id}] 🎛️ Interaction mode: {interaction_mode}")

        # Dynamic per-session identity/service routing metadata
        if effective_msg.get("tenant_id"):
            session.tenant_id = str(effective_msg.get("tenant_id"))
        if effective_msg.get("user_id"):
            session.user_id = str(effective_msg.get("user_id"))
        if effective_msg.get("agent_name"):
            session.agent_name = str(effective_msg.get("agent_name"))
        if effective_msg.get("agent_id"):
            session.agent_id = str(effective_msg.get("agent_id"))
        if effective_msg.get("language"):
            session.current_language = str(effective_msg.get("language"))
        if effective_msg.get("stt_mode"):
            session.stt_mode = str(effective_msg.get("stt_mode"))
        if effective_msg.get("tts_mode"):
            session.tts_mode = str(effective_msg.get("tts_mode"))
        if effective_msg.get("session_type"):
            session.metadata["session_type"] = str(effective_msg.get("session_type"))

        # Dynamic tenant voice routing: once tenant is known, enforce tenant-specific voice.
        # Explicit voice in payload still wins for that session.
        requested_voice = effective_msg.get("voice_id")
        session.voice_id_override = self._resolve_tenant_voice_id(
            session.tenant_id,
            str(requested_voice) if requested_voice else session.voice_id_override,
            session.agent_name,
            session.agent_id
        )
        await self._persist_session_runtime_config(session)

        if mode in ["voice", "visual-copilot"]:
            prev_mode = session.mode
            session.mode = mode
            logger.info(
                f"[{session.session_id}] 🛠️ Session mode changed from {prev_mode} to: {mode} | "
                f"tenant={session.tenant_id} agent={session.agent_name} stt={session.stt_mode} tts={session.tts_mode}"
            )
            
            await self._send_json(session.websocket, {
                "type": "session_config_ack",
                "mode": mode,
                "tenant_id": session.tenant_id,
                "agent_name": session.agent_name,
                "agent_id": session.agent_id,
                "session_type": session.metadata.get("session_type", "webcall"),
                "voice_id": session.voice_id_override,
                "stt_mode": session.stt_mode,
                "tts_mode": session.tts_mode,
                "timestamp": time.time()
            }, session)

            # Proactively trigger Intro if switching to visual-copilot for the first time
            is_resume = effective_msg.get("session_id") is not None
            pending_goal = effective_msg.get("pending_goal")
            
            if mode == "visual-copilot" and is_resume and pending_goal:
                # ── STICKY AGENT RESUME ────────────────────────────────────────
                # Widget survived a full page navigation and wants to continue its mission.
                # Skip intro, speak a brief resume message, and re-trigger the mission loop.
                logger.info(f"[{session.session_id}] 🔁 STICKY AGENT: Resuming mission across navigation: '{pending_goal[:60]}'")
                
                # Update URL
                if effective_msg.get("current_url"):
                    session.current_url = effective_msg.get("current_url")
                
                # Brief resume narration
                async def sticky_resume():
                    await asyncio.sleep(1.0)  # Wait for DOM to arrive
                    if session.is_closed:
                        return
                    
                    # Transition to THINKING for mission processing
                    if session.state_manager.state in (State.IDLE, State.LISTENING):
                        await session.state_manager.transition(State.THINKING, trigger="sticky_resume")
                        await self._broadcast_state(session, State.THINKING)
                    
                    # Brief narration
                    resume_speech = "Page loaded. Continuing where we left off."
                    await self._stream_tts_to_browser(session, resume_speech, session.current_language)
                    
                    # Re-start mission with the saved goal
                    await self._start_mission(session, pending_goal)
                
                asyncio.create_task(sticky_resume())
            
            elif mode == "visual-copilot" and prev_mode != "visual-copilot" and not is_resume:
                logger.info(f"[{session.session_id}] 👋 Triggering Visual Co-Pilot Intro | Resume: {is_resume}")
                
                # Check if elements were sent together with the config
                elements = effective_msg.get("elements") or effective_msg.get("visible_elements") or effective_msg.get("payload")
                if isinstance(elements, list):
                    session.dom_context = elements
                    logger.info(f"[{session.session_id}] 👁️ DOM elements found in session_config: {len(elements)} items")

                # Ensure we move through valid states for the intro
                # Go directly to SPEAKING (skip THINKING/Processing) so user can't interrupt
                if session.state_manager.state == State.IDLE:
                    await session.state_manager.transition(State.LISTENING, trigger="auto_start")

                if session.state_manager.state == State.LISTENING:
                    await session.state_manager.transition(State.SPEAKING, trigger="intro_trigger")
                    await self._broadcast_state(session, State.SPEAKING)
                
                # Trigger a generic visual greeting AFTER a short delay to allow DOM sync
                # Trigger a generic visual greeting AFTER a short delay to allow DOM sync
                async def delayed_intro():
                    await asyncio.sleep(0.5) # 500ms delay for DOM sync
                    if not session.is_closed and session.mode == "visual-copilot":
                        # Ensure backend services are connected before proceeding (matching _handle_start_session)
                        if not session.stt_connected_event.is_set() or not session.tts_connected_event.is_set():
                            logger.info(f"[{session.session_id}] ⏳ Waiting for persistent service connections before visual intro...")
                            try:
                                await asyncio.wait_for(asyncio.gather(
                                    session.stt_connected_event.wait(),
                                    session.tts_connected_event.wait()
                                ), timeout=3.0)
                            except asyncio.TimeoutError:
                                logger.warning(f"[{session.session_id}] ⚠️ Proceeding with visual intro despite connection lag")

                        if session.is_closed: return

                        # Ensure we are in SPEAKING state for intro (skip THINKING/Processing)
                        if session.state_manager.state != State.SPEAKING:
                            if session.state_manager.state == State.LISTENING:
                                await session.state_manager.transition(State.SPEAKING, trigger="intro_trigger_delayed")
                            await self._broadcast_state(session, State.SPEAKING)
                        
                        # DYNAMIC INTRO: Quickly identify website from DOM
                        website_name = "this website"
                        if session.dom_context and len(session.dom_context) > 0:
                            # Fast scan for website identifiers (title, logo text, h1, meta)
                            for el in session.dom_context[:30]:  # Check first 30 elements only
                                el_type = el.get('type', '').lower()
                                el_text = el.get('text', '').strip()
                                
                                # Priority 1: Page title or h1
                                if el_type in ['title', 'h1'] and el_text and len(el_text) < 50:
                                    # Clean up common patterns
                                    website_name = el_text.split('|')[0].split('-')[0].strip()
                                    break
                                
                                # Priority 2: Logo or brand text
                                if 'logo' in el.get('id', '').lower() or 'brand' in el.get('id', '').lower():
                                    if el_text and len(el_text) < 30:
                                        website_name = el_text
                                        break
                            
                            # Fallback: Try to extract from URL
                            if website_name == "this website" and session.current_url:
                                try:
                                    from urllib.parse import urlparse
                                    domain = urlparse(session.current_url).netloc
                                    # Remove www. and common TLDs
                                    domain = domain.replace('www.', '').split('.')[0]
                                    if domain and len(domain) > 2:
                                        website_name = domain.capitalize()
                                except:
                                    pass
                        
                        # Generate dynamic intro
                        if session.current_language == "de":
                            intro_text = f"Hallo! Ich bin TARA, Ihr visueller Co-Browsing-Pilot. Ich sehe, wir sind auf {website_name}. Wie kann ich Ihnen helfen?"
                        else:
                            intro_text = f"Hello! I am TARA, your visual co-browsing pilot. I can see we're on {website_name}. How can I assist you?"
                            
                        logger.info(f"[{session.session_id}] 🔊 Speaking dynamic intro: '{intro_text[:80]}...'")
                        await self._stream_tts_to_browser(session, intro_text, session.current_language)
                        
                        # Return to LISTENING so user can now talk
                        if not session.is_closed:
                            # Avoid double transition if timer already set it
                            if session.state_manager.state != State.LISTENING:
                                await session.state_manager.transition(State.LISTENING, trigger="intro_complete")
                                await self._broadcast_state(session, State.LISTENING)
                            
                            # Reset timeout timer so we don't timeout immediately after long intro
                            session.last_activity = time.time()
                
                asyncio.create_task(delayed_intro())

    async def _handle_dom_update(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Update the cached DOM context for visual mode"""
        elements = msg.get("elements") or msg.get("visible_elements") or msg.get("payload")
        
        # Update current URL if provided
        if msg.get("url"):
            session.current_url = msg.get("url")
            
        # IMMEDIATE MAP MODE CHECK (First load only)
        if self.config.services.rag.enable_map and not getattr(session, 'map_status_checked', False) and session.current_url:
            session.map_status_checked = True
            try:
                async with httpx.AsyncClient() as client:
                    # 1. Check Domain Status
                    resp = await client.post(
                        f"{session.rag_url}/api/v1/check_domain_status",
                        json={"url": session.current_url, "client_id": "tara"},
                        timeout=3.0
                    )
                    if resp.status_code == 200:
                        status = resp.json()
                        mode = status.get("mode", "explorer")
                        session.metadata["map_mode"] = mode
                        
                        if mode == "mapped":
                            logger.info(f"[{session.session_id}] 🗺️ MAP MODE ACTIVE: {status.get('reason')}")
                            # 2. Fetch Hints Immediately
                            hints_resp = await client.post(
                                f"{session.rag_url}/api/v1/get_map_hints",
                                json={"goal": "sitemap", "client_id": "tara"},
                                timeout=3.0
                            )
                            if hints_resp.status_code == 200 and hints_resp.json().get("hints"):
                                hint = hints_resp.json().get("hints")
                                logger.info(f"[{session.session_id}] 🗺️ GPS Acquired: {hint[:100]}...")
                        else:
                            logger.info(f"[{session.session_id}] 🧭 EXPLORER MODE: New domain, HiveMind disabled.")
            except Exception as e:
                logger.warning(f"[{session.session_id}] Map status check failed: {e}")

        if isinstance(elements, list):
            # Snapshot current DOM before overwriting (for fast validation)
            if session.dom_context:
                session.last_dom_context = session.dom_context
            session.dom_context = elements
            # Log detailed summary for proof that scanning/sending is working
            preview = [f"{e.get('type')}:{e.get('id', 'no-id')}:{e.get('text', '')[:20]}" for e in elements]
            logger.info(f"[{session.session_id}] 👁️ DOM Update Received ({len(elements)} items): {preview}")
        else:
            logger.warning(f"[{session.session_id}] ⚠️ Received invalid DOM update format: {type(elements)}")

        # Store v5 semantic data from widget (active states, tables, page title)
        session._active_states = msg.get("active_states")
        session._data_tables = msg.get("data_tables")
        session._page_title = msg.get("title", "")

        # Reset timeout timer on DOM update (keep session alive during navigation)
        session.last_activity = time.time()

    async def _handle_request_history(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """
        Phoenix Protocol: Cross-Domain Session Restoration.

        The frontend woke up on a new domain after a cross_domain_navigate command.
        It sends the old session_id so we can reconnect it to its memory and resume
        the mission. We reply with a history_restore message containing the last N turns.
        """
        requested_session_id = msg.get("session_id", "")
        mission_goal = msg.get("mission_goal", "")

        # Prefer the requested session (old domain) — fall back to current
        target_session = session
        if requested_session_id and requested_session_id != session.session_id:
            old_session = self.sessions.get(requested_session_id)
            if old_session and old_session.history_manager:
                target_session = old_session
                logger.info(
                    f"[{session.session_id}] 🔥 Phoenix: Bridging from old session "
                    f"{requested_session_id} → new session"
                )
                # Import the history into the new session so future turns are connected
                session.history_manager = old_session.history_manager
            else:
                logger.info(
                    f"[{session.session_id}] 🔥 Phoenix: Old session not found "
                    f"({requested_session_id}), using current session history"
                )

        # Fetch the last 4 turns (user + assistant interleaved)
        turns = []
        if target_session.history_manager:
            recent = target_session.history_manager.get_recent_turns(count=4)
            for t in recent:
                turns.append({"role": t.role, "text": t.text})

        # Restore mission goal if we have one stored
        restored_goal = mission_goal
        if not restored_goal and hasattr(target_session, '_current_goal'):
            restored_goal = str(target_session._current_goal)

        logger.info(
            f"[{session.session_id}] 🔥 Phoenix: Sending {len(turns)} history turns "
            f"+ goal='{restored_goal}'"
        )

        await self._send_json(session.websocket, {
            "type": "history_restore",
            "turns": turns,
            "mission_goal": restored_goal,
            "restored_from": requested_session_id or session.session_id
        }, session)

    async def _handle_dom_delta(self, session: OrchestratorSession, msg: Dict[str, Any]):

        """
        Handle incremental DOM delta from TaraSensor (Ultimate Architecture).
        
        Delta message format:
        {
            "type": "dom_delta",
            "delta_type": "full_scan" | "update" | "add" | "remove",
            "nodes": [...],  # For full_scan/add/update
            "changes": [...], # For update
            "removed_ids": [],
            "url": "https://example.com",
            "timestamp": 1234567890
        }
        
        This is more efficient than full DOM snapshots as it only sends changes.
        """
        delta_type = msg.get("delta_type", "update")
        
        # Update URL if provided
        if msg.get("url"):
            session.current_url = msg.get("url")
        
        # Process based on delta type
        if delta_type == "full_scan":
            # Full scan - replace entire DOM context
            nodes = msg.get("nodes", [])
            if isinstance(nodes, list):
                session.last_dom_context = session.dom_context if session.dom_context else []
                session.dom_context = nodes
                
                # Store semantic data if provided
                session._active_states = msg.get("active_states")
                session._data_tables = msg.get("data_tables")
                session._page_title = msg.get("title", "")
                
                logger.info(
                    f"[{session.session_id}] 👁️ DOM Delta: Full scan ({len(nodes)} nodes)"
                )
        
        elif delta_type == "update":
            # Incremental update - apply changes
            changes = msg.get("changes", [])
            if not isinstance(changes, list):
                logger.warning(f"[{session.session_id}] ⚠️ Invalid delta changes format")
                return
            
            added_count = 0
            updated_count = 0
            removed_count = 0
            
            # Build node map for quick lookup
            node_map = {node.get("id"): node for node in session.dom_context}
            
            for change in changes:
                change_type = change.get("type")
                
                if change_type == "add":
                    node = change.get("node")
                    if node and isinstance(node, dict):
                        node_id = node.get("id")
                        if node_id:
                            node_map[node_id] = node
                            added_count += 1
                
                elif change_type == "update":
                    node = change.get("node")
                    if node and isinstance(node, dict):
                        node_id = node.get("id")
                        if node_id and node_id in node_map:
                            node_map[node_id] = node
                            updated_count += 1
                
                elif change_type == "remove":
                    node_id = change.get("id")
                    if node_id and node_id in node_map:
                        del node_map[node_id]
                        removed_count += 1
            
            # Convert back to list
            session.dom_context = list(node_map.values())
            
            logger.debug(
                f"[{session.session_id}] 👁️ DOM Delta: Update "
                f"(+{added_count} ~{updated_count} -{removed_count})"
            )
        
        elif delta_type == "add":
            # Single add (legacy format)
            nodes = msg.get("nodes", [])
            if isinstance(nodes, list) and session.dom_context:
                node_map = {node.get("id"): node for node in session.dom_context}
                for node in nodes:
                    if isinstance(node, dict) and node.get("id"):
                        node_map[node["id"]] = node
                session.dom_context = list(node_map.values())
                logger.debug(f"[{session.session_id}] 👁️ DOM Delta: Add ({len(nodes)} nodes)")
        
        elif delta_type == "remove":
            # Single remove (legacy format)
            removed_ids = msg.get("removed_ids", [])
            if isinstance(removed_ids, list) and session.dom_context:
                node_map = {node.get("id"): node for node in session.dom_context}
                for node_id in removed_ids:
                    node_map.pop(node_id, None)
                session.dom_context = list(node_map.values())
                logger.debug(f"[{session.session_id}] 👁️ DOM Delta: Remove ({len(removed_ids)} nodes)")
        
        # Reset timeout timer (keep session alive during navigation)
        session.last_activity = time.time()

    async def _handle_execution_complete(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Handle completion signal for visual actions"""
        session.is_executing_action = False
        session.last_activity = time.time() # Reset timeout
        logger.info(f"[{session.session_id}] 🤖 Execution complete.")

        # Snapshot current DOM before overwriting (for fast validation)
        if session.dom_context:
            session.last_dom_context = session.dom_context

        # Update DOM context if provided in the completion message
        elements = msg.get("dom_context") or msg.get("elements")
        if isinstance(elements, list):
            session.dom_context = elements
            logger.info(f"[{session.session_id}] 👁️ Updated DOM context from execution_complete ({len(elements)} items)")

        # Store v5 semantic data from widget (active states, tables, page title)
        session._active_states = msg.get("active_states")
        session._data_tables = msg.get("data_tables")
        session._page_title = msg.get("title", "")

        # Update URL from execution outcome (CRITICAL for sub-goal advancement)
        outcome = msg.get("outcome", {})
        if outcome.get("current_url"):
            old_url = session.current_url
            session.current_url = outcome["current_url"]
            if old_url != session.current_url:
                logger.info(f"[{session.session_id}] 🔗 URL changed: {old_url} → {session.current_url}")

        # MISSION MODE: Re-trigger planner
        if session.mode == "visual-copilot" and session.is_mission_active:
            # TURBO MODE STABILIZATION
            # Give the browser a moment to breathe/render after execution
            if getattr(session, 'interaction_mode', 'interactive') == 'turbo':
                # Small 500ms delay to prevent race conditions with DOM updates
                await asyncio.sleep(0.5)
            
            logger.info(f"[{session.session_id}] 🔄 Re-triggering planner for next step...")
            await self._execute_next_step(session)
        else:
            # REGULAR MODE: Return to listening
            await session.state_manager.transition(State.LISTENING, trigger="execution_done")
            await self._broadcast_state(session, State.LISTENING)


    async def _stream_visual_orchestration(self, session: OrchestratorSession, user_text: str, language: str, history_context: str = ""):
        """Dual-Stream Visual Orchestration (Parallel Voice & Action)"""
        if not session.verify_state(State.THINKING, "Visual orchestration"):
            return

        logger.info(f"[{session.session_id}] 🚀 STARTING Dual-Stream Orchestration for Visual Co-Pilot")
        state_mgr = session.state_manager
        output_language = self._get_output_language(language, session)
        
        # Reset tracking
        session.tts_total_bytes = 0
        session.is_executing_action = True # Lock for initial action

        # Clear any stale data from tts_audio_queue before starting fresh
        while not session.tts_audio_queue.empty():
            try:
                session.tts_audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        async def forward_audio():
            """Background task to forward audio chunks to browser as they arrive in the queue"""
            logger.debug(f"[{session.session_id}] 🔊 Audio forwarding task started")
            sample_rate = session.tts_sample_rate or 44100
            encoding = session.tts_format or "pcm_f32le"
            
            while not session.is_closed:
                try:
                    # Wait for audio data from the TTS receive loop (populated via session.tts_audio_queue)
                    data = await asyncio.wait_for(session.tts_audio_queue.get(), timeout=1.0)
                    
                    msg_type = data.get("type")
                    if msg_type == "audio":
                        audio_data = data.get("data")
                        is_binary = data.get("is_binary", False)
                        
                        if audio_data:
                            audio_bytes = audio_data if is_binary else base64.b64decode(audio_data)
                            
                            # Use the standardized buffering and forwarding method
                            # This handles base64 encoding and frame alignment
                            await self._enqueue_foreground_audio(
                                session, 
                                audio_bytes, 
                                sample_rate=data.get("sample_rate", sample_rate),
                                encoding=data.get("format", encoding)
                            )
                    
                    elif msg_type == "metadata":
                        # Update local params for subsequent binary chunks
                        sample_rate = data.get("sample_rate", sample_rate)
                        encoding = data.get("format") or data.get("encoding") or encoding
                        
                        # Metadata is safe to send as JSON
                        await self._send_json(session.websocket, data, session)
                    
                    # Handle end of stream signal if needed
                    if data.get("is_final") or msg_type == "complete":
                        # Flush the foreground buffer
                        await self._enqueue_foreground_audio(session, b"", flush=True)
                        break
                except asyncio.TimeoutError:
                    if session.is_closed: break
                    continue
                except Exception as e:
                    logger.error(f"[{session.session_id}] Error in visual audio forwarder: {e}")
                    break

        audio_forward_task = asyncio.create_task(forward_audio())

        try:
            # Ensure TTS connection exists before streaming
            if not session.tts_client.ws or session.tts_client.ws.closed:
                await session.tts_client.connect(session.session_id)
            
            # Start the dual-stream generator from RAG
            async for chunk in self.pipeline.rag_client.visual_orchestrate(
                query=user_text,
                session_id=session.session_id,
                dom_context=session.dom_context,
                history_context=history_context,
                language=output_language,
                base_url=session.rag_url,
                tenant_id=session.agent_name
            ):
                if session.is_closed:
                    break
                
                msg_type = chunk.get("type")
                
                if msg_type == "voice":
                    content = chunk.get("content", "")
                    if content:
                        # Capture transition to SPEAKING on first voice chunk
                        if state_mgr.state != State.SPEAKING:
                            await state_mgr.transition(State.SPEAKING, trigger="visual_voice_start")
                            await self._broadcast_state(session, State.SPEAKING)

                        # Stream text to TTS service (audio results will be picked up by forward_audio task)
                        await session.tts_client.stream_chunk(content, language=output_language, voice_id=session.voice_id_override, pronunciation_dict_id=self.config.agent.pronunciation_dict_id)
                
                elif msg_type == "action":
                    payload = chunk.get("payload")
                    if payload:
                        logger.info(f"[{session.session_id}] 🤖 Action generated: {payload.get('type')}")
                        await self._send_json(session.websocket, {
                            "type": "command",
                            "payload": payload
                        }, session)
                        
                        # Update state to EXECUTING (inform client to mute mic/turn orb purple)
                        await self._send_json(session.websocket, {
                            "type": "state_update",
                            "state": "executing"
                        }, session)
            
            # Finalize TTS stream
            await session.tts_client.stream_end()
            
            # Wait for action completion (with timeout)
            if session.is_executing_action:
                logger.info(f"[{session.session_id}] ⏳ Waiting for visual action to complete...")
                try:
                    # Wait for up to 10 seconds for the action to finish
                    # The _handle_execution_complete method will set is_executing_action = False
                    start_wait = time.time()
                    while session.is_executing_action and (time.time() - start_wait) < 10.0:
                        if session.is_closed: break
                        await asyncio.sleep(0.1)
                    
                    if session.is_executing_action:
                        logger.warning(f"[{session.session_id}] ⚠️ Action execution timed out (flag still true)")
                        session.is_executing_action = False
                    else:
                        logger.info(f"[{session.session_id}] ✅ Visual action confirmed complete")
                        
                except Exception as e:
                    logger.error(f"Error waiting for action: {e}")

            # Wait a bit for final audio chunks to be processed by our forwarder
            await asyncio.sleep(0.5)
            
            # Start server-side timer to transition to LISTENING exactly when audio ends
            # Only do this if we tracked a valid start time and duration
            if session.audio_playback_start_time and session.audio_playback_duration:
                elapsed = time.time() - session.audio_playback_start_time
                remaining_sec = max(session.audio_playback_duration - elapsed, 0)
                logger.info(f"[{session.session_id}] ⏳ Visual Orchestration done. Waiting remaining audio time: {remaining_sec:.2f}s")
                await self._start_server_side_playback_timer(session, remaining_sec)
            else:
                # Fallback if audio tracking failed: just go to listening now
                logger.info(f"[{session.session_id}] 🔄 Audio tracking missing, transitioning to LISTENING immediately")
                if state_mgr.state == State.SPEAKING or state_mgr.state == State.EXECUTING:
                    await state_mgr.transition(State.LISTENING, trigger="visual_orchestration_complete")
                    await self._broadcast_state(session, State.LISTENING)

            audio_forward_task.cancel()

        except Exception as e:
            logger.error(f"[{session.session_id}] ❌ Visual orchestration error: {e}", exc_info=True)
            session.is_executing_action = False
            if not audio_forward_task.done():
                audio_forward_task.cancel()
    
    async def _stream_tts_to_browser(self, session: OrchestratorSession, text: str, language: str, cancellation_task: Optional[asyncio.Task] = None):
        """Stream TTS audio/text to browser with optional cancellation support"""
        
        # TURBO MODE: Send text directly for instant display (skipping TTS generation)
        if getattr(session, 'interaction_mode', 'interactive') == 'turbo':
            logger.debug(f"[{session.session_id}] 🚀 TURBO MODE: Sending text '{text[:40]}...' (skipping TTS)")
            await self._send_json(session.websocket, {
                "type": "turbo_speech",
                "text": text,
                "timestamp": time.time()
            }, session)
            return

        # Check TTS mode - route to text streaming if in mock mode
        if hasattr(session, 'tts_mode') and session.tts_mode == 'text':
            await self._stream_text_response(session, text)
            return

        chunk_counter = 0

        # Check if session is already closed
        if session.is_closed:
            logger.debug(f"[{session.session_id}] Session closed, skipping TTS streaming")
            return
        
        try:
            # Ensure no concurrent TTS operations - cancel any existing TTS task
            if session.tts_task and not session.tts_task.done():
                logger.debug(f"[{session.session_id}] ⏸️ Cancelling existing TTS task before starting new one")
                session.tts_task.cancel()
                try:
                    await asyncio.wait_for(session.tts_task, timeout=0.5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            
            # Connect to TTS service (reconnect if needed)
            if not session.tts_client.ws or session.tts_client.ws.closed:
                await session.tts_client.connect(session.session_id)
            
            # CRITICAL: Reset tracking for THIS specific stream to ensure accurate duration
            # Also cancel any existing playback timer that might cut off this stream
            session.tts_total_bytes = 0
            self._clear_playback_tracking(session)
            if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
                session.audio_playback_server_timer.cancel()
                logger.debug(f"[{session.session_id}] ⏸️ Cancelled existing playback timer for new TTS stream")
            
            # Clear any stale data from tts_audio_queue before starting fresh
            while not session.tts_audio_queue.empty():
                try:
                    session.tts_audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            
            # Synthesize text
            await session.tts_client.synthesize(text, language, emotion="helpful", voice_id=session.voice_id_override)
            
            # Default values (standard for our STT/TTS stack)
            sample_rate = session.tts_sample_rate or 44100
            encoding = session.tts_format or "pcm_f32le" # Default to f32 for HD Cartesia
            
            # Receive and forward audio chunks from the background queue
            while not session.is_closed:
                try:
                    # Use a timeout to allow periodic checks for session closure or cancellation
                    data = await asyncio.wait_for(session.tts_audio_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if session.is_closed:
                        break
                    continue
                
                # Check for cancellation (for fillers)
                if cancellation_task and cancellation_task.done():
                    logger.debug(f"[{session.session_id}] TTS cancelled by filler task")
                    break
                
                msg_type = data.get("type")
                
                # Handle Metadata (crucial for setting correct sample rate/format)
                if msg_type == "metadata":
                    sample_rate = data.get("sample_rate", sample_rate)
                    encoding = data.get("format") or data.get("encoding") or encoding
                    logger.debug(f"[{session.session_id}] 🔊 TTS Metadata updated: {sample_rate}Hz, {encoding}")
                    continue

                if msg_type == "audio":
                    # INTERRUPTION LOGIC: Stop if state clearly indicates user interrupted
                    # Allow LISTENING state because the state can transition mid-stream
                    # (e.g., server-side timer fires while audio is still being sent)
                    state_mgr = session.state_manager
                    if state_mgr.state not in (State.SPEAKING, State.THINKING, State.LISTENING):
                         logger.info(f"[{session.session_id}] 🛑 Stopping TTS receive loop - state changed to {state_mgr.state.value}")
                         break
                         
                    audio_data = data.get("data", "")
                    if audio_data:
                        # Extract dynamic format info if present in this specific chunk
                        chunk_sample_rate = data.get("sample_rate", sample_rate)
                        chunk_encoding = data.get("format") or data.get("encoding") or encoding
                        
                        # Track playback start on first chunk (for TTS streaming)
                        if not cancellation_task and session.audio_playback_start_time is None:
                            # CRITICAL: Flush any existing filler audio from buffer
                            await self._enqueue_foreground_audio(session, b"", flush=True, sample_rate=chunk_sample_rate, encoding=chunk_encoding)
                            
                            session.audio_playback_turn_id += 1
                            session.audio_playback_start_time = time.time()
                            logger.debug(f"[{session.session_id}] 📊 Tracking TTS playback start")
                        
                        if data.get("is_binary"):
                            audio_bytes = audio_data
                        else:
                            audio_bytes = base64.b64decode(audio_data)

                        # Decode audio and buffer into aligned frames
                        await self._enqueue_foreground_audio(session, audio_bytes, sample_rate=chunk_sample_rate, encoding=chunk_encoding)
                        
                        # Accumulate bytes for accurate duration
                        if not cancellation_task:
                            if not hasattr(session, 'tts_total_bytes'):
                                session.tts_total_bytes = 0
                            session.tts_total_bytes += len(audio_bytes)
                            
                            # Update duration based on sample rate and format
                            bytes_per_sample = 4 if "f32" in chunk_encoding else 2
                            bytes_per_sec = chunk_sample_rate * bytes_per_sample
                            duration_sec = session.tts_total_bytes / bytes_per_sec
                            session.audio_playback_duration = max(duration_sec, 1.0)
                        
                        chunk_counter += 1
                elif msg_type == "complete":
                    break
            
            # Flush remaining buffer and signal completion (only if still connected)
            if not session.is_closed:
                # Flush any remaining buffered audio
                await self._enqueue_foreground_audio(session, b"", flush=True, sample_rate=sample_rate, encoding=encoding)
                
                # Signal completion
                await self._send_json(session.websocket, {
                    "type": "audio_chunk",
                    "chunk_id": f"{session.session_id}_chunk_{chunk_counter}",
                    "is_final": True,
                    "playback_turn_id": session.audio_playback_turn_id,
                    "timestamp": time.time()
                }, session)
                
                # Start server-side timer (PRIMARY mechanism for accurate timing)
                # Skip this for fillers in THINKING state (they have cancellation_task)
                if not cancellation_task and session.audio_playback_duration and session.audio_playback_start_time:
                    # Calculate REMAINING duration
                    elapsed = time.time() - session.audio_playback_start_time
                    remaining_sec = max(session.audio_playback_duration - elapsed, 0)
                    
                    logger.debug(f"[{session.session_id}] ⏳ Starting server-side timer for remaining playback: {remaining_sec:.2f}s (elapsed: {elapsed:.2f}s)")
                    await self._start_server_side_playback_timer(session, remaining_sec)
                elif not cancellation_task and session.audio_playback_duration:
                    # Fallback: start timer with full duration if start time missing
                    await self._start_server_side_playback_timer(session, session.audio_playback_duration)
                elif not cancellation_task:
                    # SAFETY NET: If duration calculation failed, force a short timer to ensure we return to LISTENING
                    logger.warning(f"[{session.session_id}] ⚠️ TTS duration missing - starting fallback timer (1.0s) to prevent state lock")
                    await self._start_server_side_playback_timer(session, 1.0)
        
        except asyncio.CancelledError:
            logger.debug(f"[{session.session_id}] TTS streaming cancelled")
            raise
        except Exception as e:
            logger.error(f"[{session.session_id}] TTS streaming error: {e}")

    async def _stream_text_response(self, session: OrchestratorSession, text: str):
        """Stream text response for mock TTS mode"""
        # Split into chunks (word-by-word or sentence-by-sentence)
        chunks = text.split()  # Simple word splitting

        for i, chunk in enumerate(chunks):
            if session.is_closed:
                break

            await self._send_json(session.websocket, {
                "type": "agent_text_chunk",
                "text": chunk + " ",
                "chunk_index": i,
                "timestamp": time.time()
            }, session)

            # Small delay for natural streaming feel
            await asyncio.sleep(0.05)

        # Send completion
        await self._send_json(session.websocket, {
            "type": "agent_text_complete",
            "full_text": text,
            "timestamp": time.time()
        }, session)

        # Add to history
        session.history_manager.add_agent_turn(text, metadata={"source": "mock_tts"})

        # Transition back to LISTENING
        listening_side_effects = await session.state_manager.transition(
            State.LISTENING,
            trigger="text_response_complete"
        )
        output_language = self._get_output_language(session.current_language, session)
        await self._execute_side_effects(session, listening_side_effects, output_language)
        await self._broadcast_state(session, State.LISTENING)

    async def _stream_tts_to_browser_with_cancellation(self, session: OrchestratorSession, text: str, language: str, cancellation_task: Optional[asyncio.Task] = None):
        """Stream TTS audio to browser with cancellation support for fillers"""
        await self._stream_tts_to_browser(session, text, language, cancellation_task)
    
    async def _stream_audio_file(self, session: OrchestratorSession, audio_path: str, cancellation_task: Optional[asyncio.Task] = None):
        """
        Stream pre-generated audio file to browser.
        
        Args:
            session: Orchestrator session
            audio_path: Path to WAV audio file
            cancellation_task: Optional task to check for cancellation (for fillers)
        """
        # Check if session is already closed
        if session.is_closed:
            logger.debug(f"[{session.session_id}] Session closed, skipping audio file streaming")
            return
        
        try:
            # CRITICAL: Cancel any existing playback timer at the start of a new stream
            if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
                session.audio_playback_server_timer.cancel()
                logger.debug(f"[{session.session_id}] ⏸️ Cancelled existing playback timer for new audio file stream")
            if not cancellation_task:
                self._clear_playback_tracking(session)

            audio_file = Path(audio_path)
            if not audio_file.exists():
                logger.warning(f"[{session.session_id}] Audio file not found: {audio_path}")
                return
            
            # Read WAV file
            with wave.open(str(audio_file), 'rb') as wf:
                sample_rate = wf.getframerate()
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                n_frames = wf.getnframes()

                # Calculate duration
                duration_sec = n_frames / sample_rate
                logger.info(f"[{session.session_id}] 🎵 Audio file duration: {duration_sec:.1f}s ({n_frames} frames @ {sample_rate}Hz)")

                # Check if audio file is too short (less than 0.1 second) - might be corrupted
                # Note: Fillers are intentionally short (0.5-1s), so we allow them
                if duration_sec < 0.1:
                    logger.warning(f"[{session.session_id}] ⚠️ Audio file too short ({duration_sec:.1f}s), skipping playback")
                    return

                # Track playback start time and duration (for accurate playback_done validation)
                # Skip tracking for fillers in THINKING state (they have cancellation_task)
                if not cancellation_task:
                    session.audio_playback_turn_id += 1
                    session.audio_playback_start_time = time.time()
                    session.audio_playback_duration = duration_sec
                    logger.debug(f"[{session.session_id}] 📊 Tracking audio playback: start_time={session.audio_playback_start_time}, duration={duration_sec:.2f}s")

                # Read all audio data
                audio_bytes = wf.readframes(n_frames)
            
            # Convert to numpy array for processing
            if sampwidth == 2:  # 16-bit
                dtype = np.int16
            elif sampwidth == 4:  # 32-bit
                dtype = np.int32
            else:
                logger.error(f"[{session.session_id}] Unsupported sample width: {sampwidth}")
                return
            
            audio_array = np.frombuffer(audio_bytes, dtype=dtype)
            
            # Convert stereo to mono if needed
            if n_channels > 1:
                audio_array = audio_array.reshape(-1, n_channels)
                audio_array = audio_array.mean(axis=1).astype(dtype)
            
            # Use larger chunks (100ms) to prevent crackling and ensure smooth playback
            # Smaller chunks cause audio artifacts due to browser buffering
            chunk_duration_ms = 100  # 100ms chunks for smooth playback
            chunk_size_samples = int(sample_rate * chunk_duration_ms / 1000)
            chunk_duration_sec = chunk_duration_ms / 1000.0
            
            chunk_counter = 0
            total_samples = len(audio_array)
            
            for i in range(0, total_samples, chunk_size_samples):
                # Check if session is closed
                if session.is_closed:
                    logger.debug(f"[{session.session_id}] Session closed, stopping audio file streaming")
                    break
                
                # Check for cancellation before each chunk
                if cancellation_task and cancellation_task.done():
                    logger.debug(f"[{session.session_id}] Audio file streaming cancelled")
                    break
                
                # Check if state changed (interrupt occurred)
                state_mgr = session.state_manager
                target_state = State.THINKING if cancellation_task else State.SPEAKING
                if state_mgr.state != target_state:
                    logger.debug(f"[{session.session_id}] ⏸️ Audio file streaming stopped (state changed from {target_state.value} to {state_mgr.state.value})")
                    break
                
                # Get chunk (pad last chunk if needed to maintain consistent size)
                chunk_samples = audio_array[i:i + chunk_size_samples]
                
                # Pad last chunk if it's smaller than expected (prevents crackling)
                if len(chunk_samples) < chunk_size_samples:
                    padding = np.zeros(chunk_size_samples - len(chunk_samples), dtype=dtype)
                    chunk_samples = np.concatenate([chunk_samples, padding])
                
                # Convert back to bytes based on session format
                if session.tts_format == "pcm_f32le":
                    # Convert to float32 normalized to [-1.0, 1.0]
                    if dtype == np.int16:
                        chunk_samples_f32 = chunk_samples.astype(np.float32) / 32768.0
                    elif dtype == np.int32:
                        chunk_samples_f32 = chunk_samples.astype(np.float32) / 2147483648.0
                    else:
                        chunk_samples_f32 = chunk_samples.astype(np.float32)
                    chunk_bytes = chunk_samples_f32.tobytes()
                    encoding = "pcm_f32le"
                else:
                    # Default to pcm_s16le
                    if dtype == np.int16:
                        chunk_bytes = chunk_samples.tobytes()
                    else:
                        # Convert to int16 if needed
                        chunk_samples_int16 = (chunk_samples / (2**(sampwidth*8-1)) * 32767).astype(np.int16)
                        chunk_bytes = chunk_samples_int16.tobytes()
                    encoding = "pcm_s16le"
                
                # Encode to base64
                audio_b64 = base64.b64encode(chunk_bytes).decode('utf-8')
                
                # Send chunk (stop if client disconnected)
                if not await self._send_json(session.websocket, {
                    "type": "audio_chunk",
                    "data": audio_b64,
                    "sample_rate": sample_rate,
                    "format": encoding,
                    "chunk_id": f"{session.session_id}_filler_{chunk_counter}",
                    "is_final": False,  # Not final yet
                    "playback_turn_id": session.audio_playback_turn_id,
                    "timestamp": time.time(),
                    "duration_ms": chunk_duration_ms
                }, session):
                    break
                chunk_counter += 1
                
                # Sleep for exact chunk duration to maintain proper timing and prevent crackling
                await asyncio.sleep(chunk_duration_sec)
            
            # Signal completion (only if still connected)
            if not session.is_closed:
                await self._send_json(session.websocket, {
                    "type": "audio_chunk",
                    "chunk_id": f"{session.session_id}_file_chunk_{chunk_counter}",
                    "is_final": True,
                    "playback_turn_id": session.audio_playback_turn_id,
                    "timestamp": time.time()
                }, session)

                # Start server-side timer (PRIMARY mechanism for accurate timing)
                # Skip this for fillers in THINKING state (they have cancellation_task)
                if not cancellation_task and session.audio_playback_duration and session.audio_playback_start_time:
                    # Calculate REMAINING duration
                    elapsed = time.time() - session.audio_playback_start_time
                    remaining_sec = max(session.audio_playback_duration - elapsed, 0)
                    
                    logger.debug(f"[{session.session_id}] ⏳ Starting server-side timer for remaining playback: {remaining_sec:.2f}s (elapsed: {elapsed:.2f}s)")
                    await self._start_server_side_playback_timer(session, remaining_sec)
                elif not cancellation_task and session.audio_playback_duration:
                    # Fallback: start timer with full duration if start time missing
                    await self._start_server_side_playback_timer(session, session.audio_playback_duration)

            logger.debug(f"[{session.session_id}] ✅ Streamed audio file: {audio_path} ({chunk_counter} chunks)")
        
        except asyncio.CancelledError:
            logger.debug(f"[{session.session_id}] Audio file streaming cancelled")
            raise
        except Exception as e:
            logger.error(f"[{session.session_id}] Audio file streaming error: {e}", exc_info=True)
    
    async def _handle_playback_start(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """
        Handle browser playback_start message with predicted duration.
        
        Client sends this when audio starts playing, providing predictedEndMs.
        Server keeps a watchdog timer as fallback in case playback_done is missed.
        """
        msg_turn_id = msg.get("playback_turn_id") or msg.get("turn_id")
        if msg_turn_id is not None:
            try:
                msg_turn_id = int(msg_turn_id)
            except (TypeError, ValueError):
                msg_turn_id = None

        if msg_turn_id and msg_turn_id != session.audio_playback_turn_id:
            logger.debug(
                f"[{session.session_id}] ⏸️ Ignoring stale playback_start "
                f"(msg turn={msg_turn_id}, active turn={session.audio_playback_turn_id})"
            )
            return

        duration_ms = msg.get("durationMs", 0)
        predicted_end_ms = msg.get("predictedEndMs", 0)
        
        if duration_ms > 0:
            duration_sec = duration_ms / 1000.0
            session.audio_playback_duration = duration_sec
            
            if predicted_end_ms > 0:
                # Convert client timestamp to server time (approximate)
                session.audio_playback_predicted_end = time.time() + duration_sec
            else:
                # Fallback: use duration from start time
                if session.audio_playback_start_time:
                    session.audio_playback_predicted_end = session.audio_playback_start_time + duration_sec
                else:
                    session.audio_playback_start_time = time.time()
                    session.audio_playback_predicted_end = time.time() + duration_sec
            
            logger.info(
                f"[{session.session_id}] 📊 Playback START: duration={duration_sec:.2f}s, "
                f"predicted_end={session.audio_playback_predicted_end:.2f}"
            )
            
            # Start server-side watchdog timer (fallback mechanism)
            await self._start_server_side_playback_timer(session, duration_sec)
    
    async def _handle_playback_heartbeat(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """
        Handle browser playback heartbeat during audio playback.
        
        Used for monitoring and debugging, but server-side timing is primary.
        """
        progress_ms = msg.get("progressMs", 0)
        remaining_ms = msg.get("remainingMs", 0)
        logger.debug(
            f"[{session.session_id}] 💓 Playback heartbeat: progress={progress_ms}ms, "
            f"remaining={remaining_ms}ms"
        )
    
    async def _start_server_side_playback_timer(self, session: OrchestratorSession, duration_sec: float):
        """
        Start watchdog timer for playback completion.
        
        Browser playback_done remains the primary signal.
        This timer only recovers state if playback_done is not received.
        """
        playback_turn_id = session.audio_playback_turn_id

        # Cancel any existing timer
        if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
            session.audio_playback_server_timer.cancel()
            try:
                await asyncio.wait_for(session.audio_playback_server_timer, timeout=0.1)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        
        async def server_timer():
            try:
                # Watchdog delay: stay conservative to avoid premature LISTENING.
                watchdog_delay = max(duration_sec + 1.2, (duration_sec * 1.15) + 0.35)
                await asyncio.sleep(watchdog_delay)
                
                # Check if still the same playback turn, still in SPEAKING, and session not closed
                state_mgr = session.state_manager
                if (
                    playback_turn_id == session.audio_playback_turn_id
                    and state_mgr.state == State.SPEAKING
                    and not session.is_closed
                ):
                    # Reset activity timer so we don't timeout immediately after speaking
                    session.last_activity = time.time()
                    
                    logger.warning(
                        f"[{session.session_id}] ⏰ Playback watchdog fired after {watchdog_delay:.2f}s "
                        f"(turn={playback_turn_id}) — recovering to LISTENING"
                    )
                    output_language = self._get_output_language(session.current_language, session)

                    # Clear barge-in state before transitioning (prevents stale noise recovery)
                    session.barge_in_pending = False
                    if session.transcript_timeout_task and not session.transcript_timeout_task.done():
                        session.transcript_timeout_task.cancel()

                    listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="playback_watchdog_timeout")
                    await self._execute_side_effects(session, listening_side_effects, output_language)
                    await self._broadcast_state(session, State.LISTENING)

                    # Clear playback tracking
                    self._clear_playback_tracking(session)
            except asyncio.CancelledError:
                logger.debug(f"[{session.session_id}] Server-side playback timer cancelled")
            except Exception as e:
                logger.error(f"[{session.session_id}] Server-side playback timer error: {e}", exc_info=True)
        
        session.audio_playback_server_timer = asyncio.create_task(server_timer())
    
    async def _handle_playback_done(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """
        Handle browser confirming playback completion (PRIMARY mechanism).
        
        This handles both 'playback_done' and 'playback_confirmed_end' messages.
        """
        state_mgr = session.state_manager
        session.last_activity = time.time()

        if state_mgr.state != State.SPEAKING:
            logger.debug(f"[{session.session_id}] Playback done in non-SPEAKING state (ignored)")
            return

        msg_turn_id = msg.get("playback_turn_id") or msg.get("turn_id")
        if msg_turn_id is not None:
            try:
                msg_turn_id = int(msg_turn_id)
            except (TypeError, ValueError):
                msg_turn_id = None

        if msg_turn_id and msg_turn_id != session.audio_playback_turn_id:
            logger.debug(
                f"[{session.session_id}] ⏸️ Ignoring stale playback_done "
                f"(msg turn={msg_turn_id}, active turn={session.audio_playback_turn_id})"
            )
            return

        client_ts = msg.get("timestamp")
        if session.audio_playback_start_time and client_ts:
            try:
                client_ts_f = float(client_ts)
                # If client-reported completion predates current playback start,
                # this is from an older stream.
                if client_ts_f + 0.15 < session.audio_playback_start_time:
                    logger.debug(
                        f"[{session.session_id}] ⏸️ Ignoring stale playback_done by timestamp "
                        f"(client_ts={client_ts_f:.3f}, start={session.audio_playback_start_time:.3f})"
                    )
                    return
            except (TypeError, ValueError):
                pass

        # Validate playback_done timing - ignore premature messages
        if session.audio_playback_start_time and session.audio_playback_duration:
            elapsed = time.time() - session.audio_playback_start_time
            # Keep a small margin for browser clock drift.
            if elapsed + 0.08 < session.audio_playback_duration:
                # If we are in deferred barge-in validation, trust browser completion
                # and finish this turn to avoid false noise_recovery replays.
                if session.barge_in_pending:
                    logger.info(
                        f"[{session.session_id}] ✅ Accepting playback_done during pending barge-in "
                        f"(elapsed: {elapsed:.2f}s, duration: {session.audio_playback_duration:.2f}s)"
                    )
                else:
                    # Premature playback_done - ignore it (watchdog will handle it)
                    logger.debug(
                        f"[{session.session_id}] ⏸️ Ignoring premature playback_done "
                        f"(elapsed: {elapsed:.2f}s < duration: {session.audio_playback_duration:.2f}s)"
                    )
                    return
        
        # Cancel server-side timer if browser confirms early (but valid)
        if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
            session.audio_playback_server_timer.cancel()
            try:
                await asyncio.wait_for(session.audio_playback_server_timer, timeout=0.1)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            logger.debug(f"[{session.session_id}] ⏸️ Cancelled playback watchdog timer - browser confirmed playback done")
        
        logger.info(f"[{session.session_id}] ✅ Playback DONE (browser confirmed)")

        # Clear playback tracking
        self._clear_playback_tracking(session)

        # Transition back to LISTENING
        logger.info(f"[{session.session_id}] 🔄 LISTENING: Response complete, ready for next input")
        output_language = self._get_output_language(session.current_language, session)

        # Reset transcript tracking flags when returning to LISTENING
        session.pending_transcript = False
        session.partial_transcript = ""
        session.barge_in_pending = False  # Clear any pending barge-in validation

        # Cancel transcript timeout task if running (includes barge-in validation timeout)
        if session.transcript_timeout_task and not session.transcript_timeout_task.done():
            session.transcript_timeout_task.cancel()
            try:
                await asyncio.wait_for(session.transcript_timeout_task, timeout=0.1)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        
        playback_side_effects = await state_mgr.transition(State.LISTENING, trigger="playback_done")
        await self._execute_side_effects(session, playback_side_effects, output_language)
        await self._broadcast_state(session, State.LISTENING)
    
    async def _handle_interrupt(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Handle user interrupt"""
        state_mgr = session.state_manager
        
        # Cancel latency filler task if running (prevents interrupt errors)
        if session.filler_task and not session.filler_task.done():
            session.filler_task.cancel()
            try:
                await session.filler_task
            except asyncio.CancelledError:
                pass
            logger.debug(f"[{session.session_id}] ⏸️ Cancelled latency filler due to interrupt")
        
        # Cancel ongoing tasks
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        if session.pipeline_task and not session.pipeline_task.done():
            session.pipeline_task.cancel()
        
        # CRITICAL: Always cancel audio playback timer on interrupt
        if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
            session.audio_playback_server_timer.cancel()
            logger.debug(f"[{session.session_id}] ⏸️ Cancelled playback timer due to interrupt")
        self._clear_playback_tracking(session)

        # Drop any queued TTS audio so stale chunks don't leak into the next turn.
        while not session.tts_audio_queue.empty():
            try:
                session.tts_audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Best-effort: abort TTS stream generation without flushing buffered text.
        # Using stream_end() here can flush stale chunk buffer from old turn.
        try:
            if session.tts_client and session.tts_client.ws and not session.tts_client.ws.closed:
                await session.tts_client.abort_stream()
                logger.info(f"[{session.session_id}] 🛑 Aborted active TTS stream due to interrupt")
        except Exception:
            # Non-fatal; local cancellation + queue flush already handles interruption.
            pass

        # Drain queue again after abort to drop any in-flight tail chunks enqueued during close.
        while not session.tts_audio_queue.empty():
            try:
                session.tts_audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        
        # Notify browser
        await self._send_json(session.websocket, {
            "type": "playback_stop",
            "timestamp": time.time()
        }, session)
        
        # Transition to INTERRUPT (with side effects: cancel_tts, cancel_filler)
        if state_mgr.state == State.SPEAKING:
            # Use output language for side effects
            output_language = self._get_output_language(session.current_language, session)
            interrupt_side_effects = await state_mgr.transition(State.INTERRUPT, trigger="user_interrupt")
            await self._execute_side_effects(session, interrupt_side_effects, output_language)
            await asyncio.sleep(0.05)
            
            # Reset transcript tracking flags when returning to LISTENING after interrupt
            session.pending_transcript = False
            session.partial_transcript = ""
            session.accumulated_final_text = ""
            
            # Cancel transcript timeout task if running
            if session.transcript_timeout_task and not session.transcript_timeout_task.done():
                session.transcript_timeout_task.cancel()
                try:
                    await asyncio.wait_for(session.transcript_timeout_task, timeout=0.1)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            
            # Guard against concurrent recovery paths that already moved to LISTENING.
            if state_mgr.state == State.INTERRUPT:
                listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="interrupt_complete")
                await self._execute_side_effects(session, listening_side_effects, output_language)
            elif state_mgr.state != State.LISTENING:
                listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="interrupt_recover")
                await self._execute_side_effects(session, listening_side_effects, output_language)
        
        await self._broadcast_state(session, State.LISTENING)

    async def _recover_from_noise_interrupt(self, session: OrchestratorSession):
        """Recover from false/noise barge-in by returning to LISTENING only."""
        sid = session.session_id
        state_mgr = session.state_manager

        # Guard: skip if session closed or barge-in was already resolved
        if session.is_closed:
            logger.debug(f"[{sid}] Noise recovery skipped - session closed")
            return

        # Cancel any pending tasks from the interrupted state
        for task in [session.pipeline_task, session.tts_task, session.filler_task]:
            if task and not task.done():
                task.cancel()
        if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
            session.audio_playback_server_timer.cancel()
        self._clear_playback_tracking(session)
        # Drop any buffered outbound audio frames to avoid stale overlap on recovery.
        session.fg_buffer.clear()

        # Transition to LISTENING first
        if state_mgr.state not in (State.LISTENING, State.IDLE):
            await state_mgr.transition(State.LISTENING, trigger="noise_recovery")
        await self._broadcast_state(session, State.LISTENING)

        logger.info(f"[{sid}] 🔄 Noise recovery complete - staying in LISTENING (no replay)")

    async def _handle_start_session(self, session: OrchestratorSession, msg: Dict[str, Any] = None):
        """Handle session start with mode configuration and metadata"""
        if not msg:
            msg = {}

        # 1. Extract Extended Metadata (New Format)
        meta = msg.get("meta", {})
        if meta:
            session.agent_id = meta.get("agent_id")
            session.tenant_id = meta.get("tenant_id")
            session.user_id = meta.get("user_id") or session.user_id
            session.agent_name = meta.get("agent_name") or session.agent_name

        # 2. Language Configuration
        session.current_language = msg.get("language") or session.current_language
        session.secondary_language = msg.get("secondary_language")
        
        # 3. Flow & Mode Configuration
        flow_config = msg.get("flow_config", {})
        session.flow_config = flow_config
        
        stt_mode = flow_config.get("stt_mode") or msg.get("stt_mode", "audio")
        tts_mode = flow_config.get("tts_mode") or msg.get("tts_mode", "audio")
        session.stt_mode = stt_mode
        session.tts_mode = tts_mode

        detected_language = session.current_language or self.config.languages.default
        # Determine output language based on stream_out setting
        output_language = self._get_output_language(detected_language, session)

        logger.info(f"[{session.session_id}] 🎬 Starting session - Agent: {session.agent_name} ({session.agent_id}), Tenant: {session.tenant_id}, STT: {stt_mode}, TTS: {tts_mode}")
        logger.info("============================================================")
        logger.info("============================================================")
        logger.info(
            f"[{session.session_id}] SESSION START | TENANT_ID={session.tenant_id} | "
            f"AGENT={session.agent_name}({session.agent_id}) | STT={stt_mode} | TTS={tts_mode}"
        )
        logger.info("============================================================")
        logger.info("============================================================")

        # Connect to STT/TTS service if needed (handled by _stt_receive_loop and _stream_tts_to_browser)
        # We no longer connect here to avoid double connection race with background tasks
        # CRITICAL: Ensure backend services are connected before proceeding with intro
        # The background monitors (stt_receive_loop, tts_receive_loop) are already running.
        # We wait a brief moment here to ensure they established initial persistence.
        if not session.stt_connected_event.is_set() or not session.tts_connected_event.is_set():
            logger.info(f"[{session.session_id}] ⏳ Waiting for persistent service connections before intro...")
            try:
                await asyncio.wait_for(asyncio.gather(
                    session.stt_connected_event.wait(),
                    session.tts_connected_event.wait()
                ), timeout=3.0)
                logger.info(f"[{session.session_id}] ✅ Connections stabilized, proceeding with intro")
            except asyncio.TimeoutError:
                logger.warning(f"[{session.session_id}] ⚠️ Proceeding with intro despite connection instability")

        # 5. Play Introduction (flow_config first, then env-driven intro mapping)
        intro_text = None
        if output_language == session.current_language:
            intro_text = flow_config.get("intro_in_primary_lang")
        elif output_language == session.secondary_language:
            intro_text = flow_config.get("intro_in_secondary_lang")

        if not intro_text:
            intro_text = self._resolve_intro_from_env(session, output_language)

        if intro_text:
            logger.info(f"[{session.session_id}] 🔊 Playing intro ({output_language})")
            # Transition to SPEAKING
            speaking_side_effects = await session.state_manager.transition(State.SPEAKING, trigger="intro_start", data={"response": intro_text})
            await self._execute_side_effects(session, speaking_side_effects, output_language)
            await self._broadcast_state(session, State.SPEAKING)
            session.tts_task = asyncio.create_task(
                self._stream_tts_to_browser(session, intro_text, output_language)
            )

    async def _handle_text_input(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """
        Handle text input from mock STT mode.
        Bypasses STT service and processes text directly through RAG pipeline.
        """
        if session.is_closed:
            return

        text = msg.get("text", "").strip()
        if not text:
            logger.warning(f"[{session.session_id}] Empty text input in mock mode")
            return

        logger.info(f"[{session.session_id}] 📝 Mock STT input: '{text}'")
        session.last_activity = time.time()

        state_mgr = session.state_manager

        # Check state
        if state_mgr.state not in [State.IDLE, State.LISTENING]:
            logger.warning(f"[{session.session_id}] Ignoring text input in state: {state_mgr.state}")
            return

        # Add to history
        session.history_manager.add_user_turn(text, metadata={"source": "mock_stt"})

        # Transition to THINKING
        thinking_side_effects = await state_mgr.transition(State.THINKING, trigger="text_input")
        output_language = self._get_output_language(session.current_language, session)
        await self._execute_side_effects(session, thinking_side_effects, output_language)
        await self._broadcast_state(session, State.THINKING)

        # Process through RAG (same as audio mode)
        try:
            await self._process_user_input(session, text, output_language)
        except Exception as e:
            logger.error(f"[{session.session_id}] Text input processing error: {e}", exc_info=True)

    async def _handle_change_language(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Handle language change request from client"""
        new_language = msg.get("language", "de")

        # Validate language
        if new_language not in self.config.languages.supported:
            logger.warning(f"[{session.session_id}] Invalid language requested: {new_language}, supported: {self.config.languages.supported}")
            # Send error response back to client
            await self._send_ws_message(session, {
                "type": "language_change_error",
                "error": f"Unsupported language: {new_language}",
                "supported_languages": self.config.languages.supported
            })
            return

        # Update the session's language setting
        # Note: This changes the effective stream_out behavior for this session
        session.stream_out_override = new_language

        logger.info(f"[{session.session_id}] 🌐 Language changed to: {new_language}")

        # Send confirmation back to client
        await self._send_ws_message(session, {
            "type": "language_changed",
            "language": new_language,
            "message": f"Filler language changed to {new_language.upper()}"
        })

    async def _handle_speaker_mute(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Handle speaker mute/unmute for turbo mode toggle"""
        is_muted = msg.get("muted", False)
        session.speaker_muted = is_muted
        
        mode_label = "TURBO MODE (actions execute immediately)" if is_muted else "WALKTHROUGH MODE (synchronized with voice)"
        logger.info(f"[{session.session_id}] 🔊 Speaker {'muted' if is_muted else 'unmuted'} - {mode_label}")
        
        # Send confirmation back to client
        await self._send_ws_message(session, {
            "type": "speaker_mute_confirmed",
            "muted": is_muted,
            "mode": "turbo" if is_muted else "walkthrough"
        })

    async def _handle_request_asset(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Serve a static asset (e.g. orb SVG) over WebSocket to bypass cert issues"""
        asset_name = msg.get("asset", "")
        # Whitelist allowed assets to prevent path traversal
        ALLOWED_ASSETS = {"tara-orb.svg"}
        if asset_name not in ALLOWED_ASSETS:
            logger.warning(f"[{session.session_id}] ⚠️ Rejected asset request: {asset_name}")
            return

        static_dir = Path(__file__).parent.parent / "static"
        asset_path = static_dir / asset_name

        if not asset_path.exists():
            logger.warning(f"[{session.session_id}] ⚠️ Asset not found: {asset_path}")
            return

        try:
            content = asset_path.read_text(encoding="utf-8")
            await self._send_json(session.websocket, {
                "type": "asset_data",
                "asset": asset_name,
                "content_type": "image/svg+xml",
                "data": content
            }, session)
            logger.info(f"[{session.session_id}] 📦 Served asset via WS: {asset_name} ({len(content)} bytes)")
        except Exception as e:
            logger.error(f"[{session.session_id}] ❌ Failed to serve asset {asset_name}: {e}")

    async def _handle_end_session(self, session: OrchestratorSession, language: str = None):
        """Handle session end"""
        detected_language = language or session.current_language or self.config.languages.default
        # Determine output language based on stream_out setting
        output_language = self._get_output_language(detected_language)
        
        # Cancel any ongoing tasks
        if session.filler_task and not session.filler_task.done():
            session.filler_task.cancel()
            try:
                await session.filler_task
            except asyncio.CancelledError:
                pass
        
        # 1. Try to generate a dynamic exit via RAG
        # 1. Try to generate a dynamic exit via RAG
        history_context = session.history_manager.get_context_window()
        dynamic_exit_text = await self.pipeline.rag_client.generate_exit(
            history_context=history_context,
            language=output_language
        )
        
        exit_asset = None
        exit_text = None
        use_pregenerated = False
        
        if dynamic_exit_text:
            logger.info(f"[{session.session_id}] 🎤 Generated dynamic exit: {dynamic_exit_text[:50]}...")
            exit_text = dynamic_exit_text
        else:
            # Fallback to static dialogue assets
            exit_asset = self.dialogue_manager.get_exit(output_language)
            if exit_asset:
                exit_text = exit_asset.text
                use_pregenerated = exit_asset.has_audio()
        
        if exit_text:
            # Cancel any ongoing TTS/tasks before playing exit
            if session.tts_task and not session.tts_task.done():
                session.tts_task.cancel()
                try:
                    await session.tts_task
                except asyncio.CancelledError:
                    pass
            
            # Transition to SPEAKING only if not already speaking (with side effects: cancel_filler)
            if session.state_manager.state != State.SPEAKING:
                speaking_side_effects = await session.state_manager.transition(State.SPEAKING, trigger="exit_start")
                await self._execute_side_effects(session, speaking_side_effects, output_language)
                await self._broadcast_state(session, State.SPEAKING)
            else:
                logger.info(f"[{session.session_id}] 🔄 Already in SPEAKING state, playing exit audio without state transition")
            
            # Play audio
            if use_pregenerated and exit_asset:
                logger.info(f"[{session.session_id}] 💿 Using pre-generated exit: {exit_asset.audio_path}")
                await self._stream_audio_file(session, exit_asset.audio_path)
            else:
                logger.info(f"[{session.session_id}] 🔊 Playing exit via TTS: {exit_text[:50]}...")
                await self._stream_tts_to_browser(session, exit_text, output_language)
            
            # Wait for exit audio to finish generating/streaming
            if session.tts_task and not session.tts_task.done():
                try:
                    await session.tts_task
                except asyncio.CancelledError:
                    pass

            # Wait for actual playback to finish on the client side
            # The server playback timer or client playback_done event will transition state to LISTENING
            wait_cycles = 0
            while session.state_manager.state == State.SPEAKING and not session.is_closed and wait_cycles < 200: # 20 seconds max
                await asyncio.sleep(0.1)
                wait_cycles += 1
                
            # If we exited the loop and are still in SPEAKING, force the transition

            # Transition to LISTENING after exit audio completes (fallback if browser doesn't send playback_done)
            if session.state_manager.state == State.SPEAKING:
                logger.info(f"[{session.session_id}] 🔄 LISTENING: Exit audio complete, session ending")
                exit_listening_side_effects = await session.state_manager.transition(State.LISTENING, trigger="exit_complete")
                await self._execute_side_effects(session, exit_listening_side_effects, output_language)
                await self._broadcast_state(session, State.LISTENING)
        else:
            logger.warning(f"[{session.session_id}] ⚠️ No exit asset found, closing session immediately")

        # Close WebSocket (only if not already closed)
        if not session.is_closed:
            try:
                await session.websocket.close()
            except Exception as e:
                logger.debug(f"[{session.session_id}] WebSocket already closed: {e}")
        session.is_closed = True
    
    async def _keep_alive_loop(self, session: OrchestratorSession):
        """
        Send application-level heartbeats (pings) to keep connection open.
        This prevents load balancers (Traefik, AWS ELB) from closing idle connections.
        Also detects dead connections via consecutive send failures.
        """
        logger.info(f"[{session.session_id}] ❤️ KeepAlive loop started")
        consecutive_failures = 0
        max_failures = 3  # Close session after 3 consecutive failed pings (60s dead)
        
        while not session.is_closed:
            try:
                await asyncio.sleep(20.0)  # Send every 20 seconds (well below 60s standard timeout)
                
                if session.is_closed:
                    break
                    
                # Send explicit ping message
                success = await self._send_json(session.websocket, {
                    "type": "ping",
                    "timestamp": time.time()
                }, session)
                
                if success:
                    consecutive_failures = 0
                    logger.debug(f"[{session.session_id}] 💓 Keep-alive ping sent")
                else:
                    consecutive_failures += 1
                    logger.warning(f"[{session.session_id}] 💔 Keep-alive ping failed ({consecutive_failures}/{max_failures})")
                    if consecutive_failures >= max_failures:
                        logger.error(f"[{session.session_id}] 💀 Connection dead (3 consecutive ping failures). Closing session.")
                        session.is_closed = True
                        break
                
            except Exception as e:
                if not session.is_closed:
                    logger.warning(f"[{session.session_id}] KeepAlive exception: {e}")
                    consecutive_failures += 1
                    if consecutive_failures >= max_failures:
                        session.is_closed = True
                break

    async def _timeout_monitor(self, session: OrchestratorSession):
        """Monitor for timeout"""
        while True:
            try:
                await asyncio.sleep(1.0)
                
                # Check if session is closed
                if session.is_closed:
                    break
                
                # Only check timeouts when in LISTENING state
                state_mgr = session.state_manager
                if state_mgr.state != State.LISTENING:
                    continue  # Skip entirely when not listening
                
                elapsed = time.time() - session.last_activity
                if elapsed > self.config.session.timeout_seconds:
                    session.timeout_count += 1
                    session.last_activity = time.time()  # Reset to prevent rapid increments
                    
                    if session.timeout_count <= self.config.session.max_timeout_prompts:
                        
                        # Determine output language based on stream_out setting
                        output_language = self._get_output_language(session.current_language, session)
                        timeout_asset = self.dialogue_manager.get_timeout_prompt(output_language)
                        
                        if timeout_asset:
                            # Cancel any ongoing TTS before starting new one
                            if session.tts_task and not session.tts_task.done():
                                session.tts_task.cancel()
                                try:
                                    await session.tts_task
                                except asyncio.CancelledError:
                                    pass
                            
                            # Transition to SPEAKING (with side effects: cancel_filler)
                            speaking_side_effects = await session.state_manager.transition(State.SPEAKING, trigger="timeout")
                            await self._execute_side_effects(session, speaking_side_effects, output_language)
                            await self._broadcast_state(session, State.SPEAKING)
                            
                            # Prefer audio file if available
                            if timeout_asset.has_audio():
                                logger.info(f"[{session.session_id}] 💿 Using pre-generated timeout: {timeout_asset.audio_path}")
                                session.tts_task = asyncio.create_task(
                                    self._stream_audio_file(session, timeout_asset.audio_path)
                                )
                            else:
                                logger.info(f"[{session.session_id}] 🔊 Generating timeout via TTS: {timeout_asset.text[:50]}...")
                                session.tts_task = asyncio.create_task(
                                    self._stream_tts_to_browser(session, timeout_asset.text, output_language)
                                )
                    else:
                        # Max timeouts reached (after 2 timeouts) - play exit dialogue and end session
                        logger.info(f"[{session.session_id}] ⏰ Max timeouts reached ({session.timeout_count}/{self.config.session.max_timeout_prompts}) - ending session with exit dialogue")
                        await self._handle_end_session(session)
                        break
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{session.session_id}] Timeout monitor error: {e}")
    
    async def _broadcast_state(self, session: OrchestratorSession, state: State):
        """Broadcast state update to browser"""
        await self._send_json(session.websocket, {
            "type": "state_update",
            "state": state.value,
            "timestamp": time.time()
        }, session)
    
    async def _enqueue_foreground_audio(self, session: OrchestratorSession, audio_bytes: bytes, flush: bool = False, sample_rate: int = 44100, encoding: str = "pcm_f32le"):
        """
        Buffer audio into aligned frames (100ms = 4410 samples @ 44.1kHz).

        Routes audio to dedicated audio WebSocket (/stream) when available,
        falling back to control WebSocket if not connected.
        """
        # Early exit if session is dead
        if session.is_closed:
            return
        
        bytes_per_sample = 4 if "f32" in encoding else 2
        frame_size_bytes = int(sample_rate * 0.1) * bytes_per_sample
        
        
        use_audio_ws = session.audio_ws_connected and session.audio_websocket is not None

        # Add new bytes to buffer
        if audio_bytes:
            session.fg_buffer.extend(audio_bytes)

        # Process full frames from buffer
        while len(session.fg_buffer) >= frame_size_bytes:
            # Early exit check inside the loop
            if session.is_closed:
                return
            
            # Extract full frame
            chunk = bytes(session.fg_buffer[:frame_size_bytes])
            del session.fg_buffer[:frame_size_bytes]

            if use_audio_ws:
                # DEDICATED AUDIO WS: Send binary only (no JSON metadata needed)
                try:
                    async with session.audio_send_lock:
                        await session.audio_websocket.send_bytes(chunk)
                except Exception as e:
                    logger.warning(f"[{session.session_id}] Audio WS send failed, falling back to control WS: {e}")
                    session.audio_ws_connected = False
                    use_audio_ws = False
                    # Fallback: send via control WS
                    await self._send_audio_via_control_ws(session, chunk, sample_rate, encoding, session._audio_chunk_counter)
            else:
                # CONTROL WS FALLBACK: Original behavior (binary + JSON metadata)
                await self._send_audio_via_control_ws(session, chunk, sample_rate, encoding, session._audio_chunk_counter)

            session._audio_chunk_counter += 1
            # CRITICAL: Yield to event loop to prevent blocking other tasks (like Pings)
            # during large audio bursts. This prevents "keepalive ping failed" errors.
            await asyncio.sleep(0)

        # Handle flush (pad remainder)
        if flush and len(session.fg_buffer) > 0:
            remaining = len(session.fg_buffer)
            padding = b'\x00' * (frame_size_bytes - remaining)
            chunk = bytes(session.fg_buffer) + padding
            session.fg_buffer.clear()

            if use_audio_ws:
                try:
                    async with session.audio_send_lock:
                        await session.audio_websocket.send_bytes(chunk)
                        # Send end-of-stream signal
                        await session.audio_websocket.send_json({
                            "type": "audio_stream_end",
                            "sample_rate": sample_rate,
                            "format": encoding
                        })
                except Exception as e:
                    logger.warning(f"[{session.session_id}] Audio WS flush failed: {e}")
                    session.audio_ws_connected = False
                    await self._send_audio_via_control_ws(session, chunk, sample_rate, encoding, session._audio_chunk_counter, is_final=True)
            else:
                await self._send_audio_via_control_ws(session, chunk, sample_rate, encoding, session._audio_chunk_counter, is_final=True)
            
            # Reset counter after flush (stream is complete)
            session._audio_chunk_counter = 0

    async def _send_audio_via_control_ws(self, session: OrchestratorSession, chunk: bytes, sample_rate: int, encoding: str, chunk_counter: int, is_final: bool = False):
        """Send audio via the control WebSocket (original behavior, used as fallback)"""
        # Use the _send_bytes helper which properly checks connection state and uses locking
        binary_sent = await self._send_bytes(session.websocket, chunk, session)
        if not binary_sent:
            # Connection is dead, don't try to send JSON metadata either
            return False

        # Only send JSON metadata if binary was delivered successfully
        return await self._send_json(session.websocket, {
            "type": "audio_chunk",
            "sample_rate": sample_rate,
            "format": encoding,
            "chunk_id": f"{session.session_id}_chunk_{chunk_counter}",
            "is_final": is_final,
            "playback_turn_id": session.audio_playback_turn_id,
            "timestamp": time.time(),
            "binary_sent": True
        }, session)
    
    async def _send_json(self, websocket: WebSocket, data: Dict[str, Any], session: Optional[OrchestratorSession] = None) -> bool:
        """
        Send JSON message to browser.
        
        Args:
            websocket: The WebSocket connection
            data: Data to send as JSON
            session: Optional session for connection state check
            
        Returns:
            True if sent successfully, False otherwise
        """
        # Check if session is closed
        if session and session.is_closed:
            logger.debug(f"[{session.session_id}] Skipping send - session is closed")
            return False

        try:
            # Check if websocket is still connected
            if websocket.client_state.name != "CONNECTED":
                logger.debug(f"[{session.session_id if session else 'unknown'}] WebSocket not connected, skipping message: {data.get('type')}")
                return False

            if session:
                async with session.send_lock:
                    await websocket.send_json(data)
            else:
                await websocket.send_json(data)
            return True
        except (RuntimeError, ConnectionError, Exception) as e:
            # Mark session as closed if we get a transport error
            error_str = str(e).lower()
            if session and ("closing transport" in error_str or 
                            "already completed" in error_str or 
                            "accept" in error_str or
                            "not connected" in error_str or
                            isinstance(e, RuntimeError)):
                session.is_closed = True
                logger.debug(f"[{session.session_id}] WebSocket closed, marking session as closed: {type(e).__name__}")
            else:
                logger.error(f"Failed to send message ({type(e).__name__}): {e}")
            return False
    async def _send_bytes(self, websocket: WebSocket, data: bytes, session: Optional[OrchestratorSession] = None) -> bool:
        """
        Send binary data to WebSocket with locking.
        """
        if session and session.is_closed:
            return False

        try:
            if websocket.client_state.name != "CONNECTED":
                return False

            if session:
                async with session.send_lock:
                    await websocket.send_bytes(data)
            else:
                await websocket.send_bytes(data)
            return True
        except Exception as e:
            if session: session.is_closed = True
            logger.debug(f"Failed to send bytes: {e}")
            return False


    async def handle_phone_connection(self, websocket: WebSocket, call_sid: str, session_id: str):
        """
        Handle phone WebSocket connection (similar to handle_connection)

        Differences from browser:
        - Phone audio format (G.711 vs browser PCM)
        - Call-specific session management
        - No UI updates needed
        """

        # Create state manager for phone session
        state_mgr = StateManager(session_id, self.redis_client)
        await state_mgr.initialize()

        # Create phone session
        session = PhoneOrchestratorSession(
            session_id=session_id,
            websocket=websocket,
            state_manager=state_mgr,
            call_sid=call_sid,
            audio_format="g711"
        )
        session.metadata["session_type"] = "telephony"

        try:
            # Use existing connection logic but with phone session
            await self._handle_connection_internal(session)

        except Exception as e:
            logger.error(f"Phone connection error for {call_sid}: {e}")

    async def cleanup_phone_session(self, call_sid: str):
        """Cleanup phone session by call_sid"""
        session_id = f"phone_{call_sid}"

        # Find and cleanup the session
        session = self.sessions.get(session_id)
        if session:
            await self._cleanup_session(session)
            logger.info(f"Cleaned up phone session for call {call_sid}")
        else:
            logger.debug(f"No active session found for call {call_sid}")

    # ════════════════════════════════════════════════════════════════════════════════
    # END PHONE INTEGRATION METHODS
    # ════════════════════════════════════════════════════════════════════════════════

    async def _cleanup_session(self, session: OrchestratorSession):
        """Cleanup session resources (Metadata saving + removal from dict)"""
        """Cleanup session resources (Metadata saving + removal from dict)"""
        
        # 1. 📊 GENERATE SESSION REPORT
        now_ts = time.time()
        report = {
            "session_id": session.session_id,
            "user_id": session.user_id or "anonymous",
            "agent_name": session.agent_name,
            "agent_id": session.agent_id,
            "tenant_id": session.tenant_id,
            "session_type": (
                session.metadata.get("session_type")
                or ("telephony" if isinstance(session, PhoneOrchestratorSession) else "webcall")
            ),
            "start_time": datetime.fromtimestamp(session.created_at).isoformat(),
            "end_time": datetime.fromtimestamp(now_ts).isoformat(),
            "timestamp": datetime.fromtimestamp(now_ts).isoformat(),
            "duration_seconds": now_ts - session.created_at,
            "total_turns": len(session.turn_metrics),
            "avg_ttft_ms": 0.0,
            "avg_ttfc_ms": 0.0,
            # LLM cumulative metrics
            "llm_model": session.llm_model or "unknown",
            "total_prompt_tokens": session.total_prompt_tokens,
            "total_completion_tokens": session.total_completion_tokens,
            "total_llm_tokens": session.total_prompt_tokens + session.total_completion_tokens,
            # TTS cumulative metrics
            "tts_streamed_chars": session.tts_streamed_chars,
            "tts_total_time_ms": round(session.tts_total_time_ms, 1),
            "status": "completed"
        }

        # Calculate averages from metrics
        ttft_values = [m.get("ttft_ms") for m in session.turn_metrics if m.get("ttft_ms") is not None]
        ttfc_values = [m.get("ttfc_ms") for m in session.turn_metrics if m.get("ttfc_ms") is not None]
        
        if ttft_values:
            report["avg_ttft_ms"] = sum(ttft_values) / len(ttft_values)
        if ttfc_values:
            report["avg_ttfc_ms"] = sum(ttfc_values) / len(ttfc_values)
            
        logger.info(f"[{session.session_id}] 📑 SESSION REPORT: {json.dumps(report)}")
        logger.info("============================================================")
        logger.info("============================================================")
        logger.info(
            f"[{session.session_id}] SESSION END | TENANT_ID={report.get('tenant_id')} | "
            f"AGENT={report.get('agent_name')}({report.get('agent_id')}) | "
            f"TYPE={report.get('session_type')} | TURNS={report.get('total_turns')} | DURATION={report.get('duration_seconds'):.2f}s"
        )
        logger.info("============================================================")
        logger.info("============================================================")
        
        # 2. 🧠 DEVINCIAI SENTIMENT & REASONING PIPELINE
        if session.history_manager:
            try:
                # Construct history list (time-aware)
                history_list = session.history_manager.to_dict()
                
                if history_list and len(history_list) > 0:
                    logger.info(f"[{session.session_id}] 🧠 Triggering DavinciAI Sentiment & Reasoning Pipeline...")
                    base_rag_url = (session.rag_url or self.config.services.rag.url).rstrip('/')
                    analysis_url = f"{base_rag_url}/api/v1/analyze_session"
                    
                    try:
                        async with aiohttp.ClientSession() as http_session:
                            payload = {
                                "session_id": session.session_id,
                                "history_context": history_list,
                                "user_id": session.user_id or "anonymous",
                                "tenant_id": session.tenant_id or "tara",
                                "metadata": {
                                    "agent_name": session.agent_name,
                                    "agent_id": session.agent_id,
                                    "session_type": report.get("session_type"),
                                    "source": "orchestrator_cleanup",
                                    "report": {
                                        "session_id": report.get("session_id"),
                                        "tenant_id": report.get("tenant_id"),
                                        "session_type": report.get("session_type"),
                                        "agent_name": report.get("agent_name"),
                                        "agent_id": report.get("agent_id"),
                                        "user_id": report.get("user_id"),
                                        "start_time": report.get("start_time"),
                                        "end_time": report.get("end_time"),
                                        "duration_seconds": report.get("duration_seconds"),
                                        "total_turns": report.get("total_turns"),
                                        "avg_ttft_ms": report.get("avg_ttft_ms"),
                                        "avg_ttfc_ms": report.get("avg_ttfc_ms"),
                                        "llm_model": report.get("llm_model"),
                                        "total_prompt_tokens": report.get("total_prompt_tokens"),
                                        "total_completion_tokens": report.get("total_completion_tokens"),
                                        "total_llm_tokens": report.get("total_llm_tokens"),
                                        "tts_streamed_chars": report.get("tts_streamed_chars"),
                                        "tts_total_time_ms": report.get("tts_total_time_ms"),
                                        "status": report.get("status"),
                                    }
                                }
                            }
                            
                            async with http_session.post(
                                analysis_url,
                                json=payload,
                                timeout=aiohttp.ClientTimeout(total=20.0) # Analysis can take 10-15s
                            ) as resp:
                                if resp.status == 200:
                                    resp_data = await resp.json()
                                    analysis_report = resp_data.get("report", {})
                                    logger.info(f"[{session.session_id}] ✅ Analytics Pipeline success in {analysis_report.get('processing_time')}s")
                                    
                                    # Attach rich analysis to the final report
                                    report["analysis"] = analysis_report
                                    
                                    # Forward key signals to backend metrics
                                    if "business_signals" in analysis_report:
                                        report.update(analysis_report["business_signals"])
                                else:
                                    logger.warning(f"[{session.session_id}] ⚠️ Analytics Pipeline failed: {resp.status}")
                    except asyncio.TimeoutError:
                        logger.warning(f"[{session.session_id}] ⚠️ Analytics Pipeline timeout")
                    except Exception as e:
                        logger.error(f"[{session.session_id}] ❌ Analytics Pipeline error: {e}")
            except Exception as e:
                logger.error(f"[{session.session_id}] ❌ Sentiment Pipeline setup error: {e}")

        # 3. 📡 EXTERNAL METRICS WEBHOOK (BACKEND)
        webhook_url = self.config.services.davinciai_backend_url
        try:
            logger.info(f"[{session.session_id}] 📡 Sending enriched session report to backend...")
            async with aiohttp.ClientSession() as http_session:
                async with http_session.post(
                    webhook_url,
                    json=report,
                    timeout=aiohttp.ClientTimeout(total=10.0)
                ) as resp:
                    if resp.status in [200, 201]:
                        logger.info(f"[{session.session_id}] ✅ Enriched report delivered to backend")
                    else:
                        resp_text = await resp.text()
                        logger.warning(f"[{session.session_id}] ⚠️ Backend rejected report: {resp.status} - {resp_text}")
        except Exception as e:
            logger.warning(f"[{session.session_id}] ⚠️ Could not deliver report to backend: {e}")

        # 4. Cancel tasks
        for task in [session.tts_task, session.pipeline_task, session.filler_task, session.final_accumulation_timer, session.audio_playback_server_timer]:
            if task and not task.done():
                task.cancel()
        
        # Close service connections
        if session.stt_client:
            await session.stt_client.close()
        if session.tts_client:
            await session.tts_client.close()
        
        # Remove from sessions
        self.sessions.pop(session.session_id, None)
        
        logger.info(f"[{session.session_id}] Session cleaned up")

    async def _start_mission(self, session: OrchestratorSession, goal: str):
        """Initialize a multi-step visual mission."""
        session.current_goal = goal
        session.is_mission_active = True
        session.goal_step_count = 0
        session.stagnation_count = 0
        session.last_dom_hash = None
        session.last_action = ""
        session.map_hints = ""  # Will be fetched once
        logger.info(f"[{session.session_id}] 🎯 Mission Started: '{goal}'")
        
        # Notify widget of mission goal (for Sticky Agent navigation persistence)
        await self._send_json(session.websocket, {
            "type": "mission_started",
            "goal": goal,
            "timestamp": time.time()
        }, session)
        
        # Fetch GPS Hints from Qdrant ONCE at mission start
        try:
            session.map_hints = await self._fetch_map_hints(session, goal)
            if session.map_hints:
                logger.info(f"[{session.session_id}] 🗺️ GPS Acquired: {session.map_hints}")
        except Exception as e:
            logger.warning(f"[{session.session_id}] Map hints fetch failed: {e}")
        
        # Kick off the loop - First step will provide context-aware narration
        await self._execute_next_step(session)

    async def _fetch_map_hints(self, session: OrchestratorSession, goal: str) -> str:
        """Fetch navigation hints from Qdrant (called ONCE at mission start)."""
        base_rag_url = (session.rag_url or self.config.services.rag.url).rstrip('/')
        current_url = session.current_url or ""
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{base_rag_url}/api/v1/get_map_hints",
                json={
                    "goal": goal,
                    "client_id": session.tenant_id or "tara",
                    "current_url": current_url
                }
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("hints", "")
        return ""

    async def _end_mission(self, session: OrchestratorSession, summary: str):
        """Complete or abort a mission. Waits for TTS to finish before transitioning."""
        session.current_goal = None
        session.is_mission_active = False
        session.goal_step_count = 0
        session.action_history = []  # Clear history for new missions
        logger.info(f"[{session.session_id}] ✅ Mission Ended: '{summary}'")

        
        # Speak the summary and WAIT for it to complete
        await self._stream_tts_to_browser(session, summary, session.current_language)
        
        # Wait for TTS task to finish before transitioning state
        if session.tts_task and not session.tts_task.done():
            try:
                await session.tts_task
            except asyncio.CancelledError:
                pass
        
        await session.state_manager.transition(State.LISTENING, trigger="mission_complete")
        await self._broadcast_state(session, State.LISTENING)

    async def _execute_next_step(self, session: OrchestratorSession, extra_warning: str = ""):
        """Core OODA loop for mission execution."""
        # Gate: Check if mission still active
        if not session.is_mission_active or not session.current_goal:
            logger.info(f"[{session.session_id}] Mission aborted or complete.")
            return

        # Safety: Step limit
        if session.goal_step_count >= session.max_mission_steps:
            await self._end_mission(session, "I've taken many steps but couldn't finish. Please try manually.")
            return

        # Get latest DOM
        dom = session.dom_context or []

        # Stagnation Detection
        dom_str = json.dumps(dom, sort_keys=True)
        current_hash = hashlib.md5(dom_str.encode()).hexdigest()
        warning = ""
        
        if current_hash == session.last_dom_hash and session.goal_step_count > 0:
            session.stagnation_count += 1
            logger.warning(f"[{session.session_id}] DOM Stagnation #{session.stagnation_count}")
            if session.stagnation_count >= session.max_stagnation:
                await self._end_mission(session, "The screen isn't responding. Please check manually.")
                return
            warning = f"CRITICAL: Your last action had NO EFFECT. The DOM is identical. {extra_warning}"
        elif extra_warning:
            warning = extra_warning
        else:
            session.stagnation_count = 0
        
        session.last_dom_hash = current_hash

        # Call RAG Planner (70B model)
        try:
            plan = await self._call_visual_planner(session, dom, warning)
            logger.info(f"[{session.session_id}] 🧠 Received plan from RAG: {json.dumps(plan, indent=2)}")
        except Exception as e:
            logger.error(f"[{session.session_id}] Planner failed: {e}")
            await self._end_mission(session, "I encountered an error. Please try again.")
            return

        # Validation Retry Backoff: if fast-validator detected a failed action,
        # wait 1.5s to let the page settle before executing the corrective action
        validation_meta = plan.get("_validation")
        if validation_meta and not validation_meta.get("success", True):
            logger.warning(f"[{session.session_id}] ⏳ Validation failed — backoff 1.5s for page settle")
            await asyncio.sleep(1.5)

        # CONFIDENCE CHECK: Halt execution if planner is unsure
        confidence = plan.get("confidence", "high")
        logger.info(f"[{session.session_id}] 🎯 Planner Confidence: {confidence}")
        
        if confidence == "low":
            logger.warning(f"[{session.session_id}] ⚠️ LOW CONFIDENCE detected. Asking user for guidance.")
            # Use speech from planner if available, otherwise fallback
            clarification_msg = plan.get("speech", "I'm not sure where to find that. Could you give me a hint or guide me manually?")
            # _end_mission will speak the msg, so don't call it here to avoid repetition
            await self._end_mission(session, clarification_msg)
            return

        # Handle response
        # Planner may return either:
        #   - action: { ... }                (legacy single action)
        #   - action: [ { ... }, { ... } ]   (bundled multi-action pipeline)
        raw_action_payload = plan.get("action", {})
        if isinstance(raw_action_payload, list):
            action_sequence = [a for a in raw_action_payload if isinstance(a, dict)]
        elif isinstance(raw_action_payload, dict):
            action_sequence = [raw_action_payload]
        else:
            action_sequence = []

        # Representative action for routing/guards/logging:
        # prefer first non-wait step so click+wait bundles are treated as click.
        action_payload = {}
        for _a in action_sequence:
            _t = (_a.get("type") or "").strip().lower()
            if _t and _t != "wait":
                action_payload = _a
                break
        if not action_payload and action_sequence:
            action_payload = action_sequence[0]

        action_type = action_payload.get("type", "none")
        
        # GUARD RAIL: Anti-Loop Protection
        current_target = action_payload.get('target_id') or action_payload.get('id') or action_payload.get('url', 'unknown')
        current_action_signature = f"{action_type}: {current_target}"
        
        if current_action_signature == session.last_action and action_type == 'click':
            logger.warning(f"[{session.session_id}] 🛑 RECURSION GUARD: Prevented duplicate {current_action_signature}. Forcing WAIT.")
            # Track the blocked target so planner doesn't pick it again
            blocked_entry = f"BLOCKED click on {current_target} (already tried, had no effect)"
            if not hasattr(session, 'action_history') or session.action_history is None:
                session.action_history = []
            session.action_history.append(blocked_entry)
            session.goal_step_count += 1  # Count this as a step so we hit the limit sooner
            loop_warning = f"CRITICAL SYSTEM ALERT: RECURSION DETECTED. The previous action 'click {current_target}' FAILED to change the state. DO NOT REPEAT THIS ACTION. Element '{current_target}' is BLOCKED. You MUST select a DIFFERENT element or action. If the current page already shows the needed content, use 'answer' action type instead."
            await asyncio.sleep(2.0)
            await self._execute_next_step(session, extra_warning=loop_warning)
            return

        if action_type == "none":
            summary = action_payload.get("summary", "Task complete.")
            # Use speech from planner if available
            narration = plan.get("speech", summary)
            await self._end_mission(session, narration)
        elif action_type == "clarify":
            logger.info(f"[{session.session_id}] ❓ Clarification needed")
            clarification_speech = plan.get("speech", "Could you clarify which one you mean?")
            
            # Speak the question
            await self._stream_tts_to_browser(session, clarification_speech, session.current_language)
            
            # CRITICAL: Do NOT click. Wait for user to answer.
            # We treat this effectively as a "turn yield" back to the user.
            # The frontend is already listening (VAD), so we just stop the loop here.
            # But we should probably send a signal that we are "done" for now.
            logger.info(f"[{session.session_id}] 🛑 Yielding turn for clarification.")
            return

        elif action_type == "user_input_required":
            logger.info(f"[{session.session_id}] 🛡️ Privacy Guard triggered")
            privacy_speech = plan.get("speech", "Please enter this information yourself for security.")
            
            # Speak the warning
            await self._stream_tts_to_browser(session, privacy_speech, session.current_language)
            
            # Wait for TTS to finish
            if session.tts_task and not session.tts_task.done():
                try: await session.tts_task
                except: pass
            
            # Pause and return, waiting for next user command (e.g. "I'm done")
            return

        elif action_type == "answer":
            # Agent answered a data question from visible DOM - speak the answer and end
            answer_text = plan.get("speech") or action_payload.get("text", "Here's what I found.")
            logger.info(f"[{session.session_id}] 💡 Data Answer: {answer_text[:80]}")
            await self._end_mission(session, answer_text)

        elif action_type == "wait":
            logger.info(f"[{session.session_id}] ⏳ Waiting for page to load...")
            # Narrate the wait
            wait_speech = plan.get("speech", "Just a moment while this loads...")
            await self._stream_tts_to_browser(session, wait_speech, session.current_language)
            
            # Wait for TTS to finish before proceeding
            if session.tts_task and not session.tts_task.done():
                try:
                    await session.tts_task
                except asyncio.CancelledError:
                    pass
            
            await asyncio.sleep(1.0)
            await self._execute_next_step(session)

        else:
            # Execute the action
            session.goal_step_count += 1
            session.is_executing_action = True
            
            # Map target_id for consistency
            actual_target = action_payload.get('target_id') or action_payload.get('id') or action_payload.get('url', 'unknown')
            current_action_sig = f"{action_type}: {actual_target}"
            
            # Track action history (keep last 5)
            session.action_history.append(current_action_sig)
            if len(session.action_history) > 5:
                session.action_history = session.action_history[-5:]
            
            # IMPROVED LOOP DETECTION: Check if this action was already done recently
            # SCROLL EXEMPTION: Scrolling is a viewport shift, not a state mutation.
            # The DOM may not change even though new content becomes visible.
            # Allow up to 5 consecutive scrolls before considering it a loop.
            is_scroll_action = action_type in ("scroll", "scroll_to")
            is_navigation_action = action_type in ("navigate", "cross_domain_navigate")
            
            if is_navigation_action:
                # Navigation actions always change the page — never treat as loops
                pass
            elif is_scroll_action:

                # Count consecutive scrolls in recent history
                consecutive_scrolls = 0
                for sig in reversed(session.action_history):
                    if sig.startswith("scroll"):
                        consecutive_scrolls += 1
                    else:
                        break
                
                if consecutive_scrolls > 5:
                    logger.warning(f"[{session.session_id}] 🔄 SCROLL LOOP: {consecutive_scrolls} consecutive scrolls with no other action")
                    await self._end_mission(session, "I've scrolled through the page but couldn't find what we need. Could you point me in the right direction?")
                    return
            elif True:
                # Check for standard action loops
                # Relax threshold for Turbo Mode to be more resilient to stale DOM reads
                limit = 3 if getattr(session, 'interaction_mode', 'interactive') == 'turbo' else 1
                
                if session.action_history.count(current_action_sig) > limit:
                    logger.warning(f"[{session.session_id}] 🔄 LOOP DETECTED: '{current_action_sig}' repeated > {limit} times: {session.action_history}")
                    await self._end_mission(session, "I seem to be going in circles. Let me stop here so you can guide me.")
                    return
            
            session.last_action = current_action_sig
            logger.info(f"[{session.session_id}] 🎬 Step {session.goal_step_count}: {action_type} on {actual_target}")

            
            # NARRATE BEFORE ACTING (Co-Pilot Mode)
            # Start TTS simultaneously with action for true co-browsing feel
            step_speech = plan.get("speech")
            tts_task = None
            
            # Cancel any lingering TTS from previous step before starting new one
            if session.tts_task and not session.tts_task.done():
                logger.debug(f"[{session.session_id}] ⏹️ Cancelling previous TTS before new step")
                session.tts_task.cancel()
                try:
                    await asyncio.wait_for(session.tts_task, timeout=0.5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            
            if step_speech:
                logger.info(f"[{session.session_id}] 🗣️ Narrating: {step_speech}")
                # Launch TTS but save the task so we can wait for it
                # CRITICAL: Save to session.tts_task so it can be interrupted by new user input
                session.tts_task = asyncio.create_task(self._stream_tts_to_browser(session, step_speech, session.current_language))
                tts_task = session.tts_task
            
            # Handle cross-domain navigation (from Ultimate TARA Hive cross-domain search)
            if action_type == "cross_domain_navigate":
                target_domain = action_payload.get("target_domain", "")
                target_entity = action_payload.get("target_entity", "")
                logger.info(f"[{session.session_id}] 🌐 Cross-Domain Bridge: navigating to '{target_domain}' for '{target_entity}'")

                # Reset stagnation since we're moving to a completely new domain
                session.stagnation_count = 0
                session.last_dom_hash = None

                # ── PHOENIX PROTOCOL: Announce the navigation in voice ──
                # Tell the user what's happening so the page flash feels intentional
                announcement = f"Navigating to {target_domain} to find {target_entity}."
                await self._send_json(session.websocket, {
                    "type": "turbo_speech",
                    "text": announcement
                }, session)

                # Send navigation command with Phoenix metadata so the frontend
                # knows to save context to localStorage before unloading the page
                await self._send_json(session.websocket, {
                    "type": "navigate",
                    "url": f"https://{target_domain}",
                    "phoenix": True,  # Signals the frontend to run the state handoff
                    "session_id": session.session_id,
                    "mission_goal": str(getattr(session, '_current_goal', target_entity))
                }, session)
            # Handle navigate specially (tells frontend to change URL)
            elif action_type == "navigate":
                await self._send_json(session.websocket, {
                    "type": "navigate",
                    "url": action_payload.get("url", "/")
                }, session)

            else:
                # Send bundled actions through `action` for modular frontend path,
                # while preserving legacy compatibility via representative `payload`.
                command_msg = {
                    "type": "command",
                    "payload": action_payload
                }
                if isinstance(raw_action_payload, list):
                    command_msg["action"] = raw_action_payload
                await self._send_json(session.websocket, command_msg, session)
            
            # CONDITIONAL SYNC: Wait for voice to finish ONLY if speaker is NOT muted
            # When muted, go to TURBO MODE (execute immediately)
            if tts_task and not tts_task.done() and not session.speaker_muted:
                logger.debug(f"[{session.session_id}] 🔊 Speaker unmuted - waiting for TTS to complete before next action")
                try:
                    await tts_task
                except asyncio.CancelledError:
                    pass
            elif session.speaker_muted:
                logger.debug(f"[{session.session_id}] 🚀 TURBO MODE: Speaker muted - executing next action immediately")

    async def _call_visual_planner(self, session: OrchestratorSession, dom: list, warning: str) -> dict:
        """
        Dual-Stream Visual Planning:
        1. Fast Sense: Quick scan for immediate TTS (200-400ms)
        2. Stream TTS to user immediately
        3. Full Planning: Deep reasoning with filtered DOM (800-1200ms)
        """
        base_rag_url = (session.rag_url or self.config.services.rag.url).rstrip('/')
        
        # Compute simplified DOM Diff (Context Awareness)
        dom_diff = "First interaction."
        if session.dom_history:
            prev_dom = session.dom_history[-1]
            # Simple heuristic: compare length and text content of first few elements
            prev_sig = "".join([e.get("text", "")[:10] for e in prev_dom[:5]])
            curr_sig = "".join([e.get("text", "")[:10] for e in dom[:5]])
            
            if prev_sig != curr_sig:
               dom_diff = "Significant layout change detected (Navigation or Modal)."
            else:
               dom_diff = "Internal state update (Form fill or Toggle)."
        
        # Update DOM history
        session.dom_history.append(dom[:50]) # Store lightweight snapshot
        if len(session.dom_history) > 3:
            session.dom_history.pop(0)
        
        # Build conversation history context (last 4 user requests)
        conversation_history = ""
        if hasattr(session, 'history_manager') and session.history_manager:
            try:
                recent_turns = session.history_manager.get_recent_turns(limit=4)
                if recent_turns:
                    history_lines = []
                    for turn in recent_turns:
                        user_msg = turn.get('user', '')
                        if user_msg:
                            history_lines.append(f"User: {user_msg}")
                    conversation_history = "\n".join(history_lines)
            except Exception as e:
                logger.debug(f"[{session.session_id}] Could not extract conversation history: {e}")

        # ═══════════════════════════════════════════════════════════════
        # PHASE 1: FAST SENSE (Immediate TTS)
        # ═══════════════════════════════════════════════════════════════
        fast_sense_start = time.time()
        fast_response = None
        fast_speech = ""
        
        # ONLY run fast_sense on the FIRST step of a mission (not on retries/re-triggers)
        # This prevents repeating the same speech like "I see the call history..." 5 times
        is_first_step = session.goal_step_count == 0 and not warning
        
        if is_first_step:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    fast_response_obj = await client.post(
                        f"{base_rag_url}/api/v1/fast_sense",
                        json={
                            "goal": session.current_goal,
                            "dom_context": dom[:150],  # Lighter snapshot for fast processing
                            "session_id": session.session_id
                        }
                    )
                    fast_response_obj.raise_for_status()
                    fast_response = fast_response_obj.json()
                    
                    fast_sense_ms = int((time.time() - fast_sense_start) * 1000)
                    logger.info(f"[{session.session_id}] ⚡ Fast Sense completed in {fast_sense_ms}ms")
                    
                    # Extract speech for immediate TTS
                    fast_speech = fast_response.get("speech", "")
                    if fast_speech:
                        logger.info(f"[{session.session_id}] 🔊 Streaming Fast TTS: \"{fast_speech[:60]}...\"")
                        # Stream TTS immediately (non-blocking)
                        output_language = self._get_output_language(session.current_language, session)
                        session.tts_task = asyncio.create_task(
                            self._stream_tts_to_browser(session, fast_speech, output_language)
                        )
            except Exception as e:
                logger.warning(f"[{session.session_id}] Fast Sense failed (fallback to planning): {e}")
                # Continue to full planning even if Fast Sense fails
        else:
            # Not first step — skip fast_sense entirely to avoid repeating speech
            logger.debug(f"[{session.session_id}] Skipping Fast Sense on step {session.goal_step_count} (not first step)")

        # ═══════════════════════════════════════════════════════════════
        # PHASE 2: FULL PLANNING (Deep Reasoning)
        # ═══════════════════════════════════════════════════════════════
        # v5: Send full DOM to planner — query-focused slicing happens server-side
        filtered_dom = dom[:600]
        if fast_response and fast_response.get("relevant_ids"):
            relevant_ids = set(fast_response["relevant_ids"])
            relevant = [el for el in dom if el.get("id") in relevant_ids]
            rest = [el for el in dom if el.get("id") not in relevant_ids]
            filtered_dom = (relevant + rest)[:600]
            logger.info(f"[{session.session_id}] 🔍 DOM priority sort: {len(relevant)} relevant boosted to front out of {len(dom)} total")

        # v5: Extract semantic metadata from stored context
        active_states = getattr(session, '_active_states', None)
        data_tables = getattr(session, '_data_tables', None)
        page_title = getattr(session, '_page_title', '')

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{base_rag_url}/api/v1/plan_next_step",
                json={
                    "goal": session.current_goal,
                    "dom_context": filtered_dom,
                    "step_number": session.goal_step_count,
                    "warning_message": warning,
                    "current_url": session.current_url,
                    "last_action": session.last_action,
                    "action_history": session.action_history,  # For loop detection
                    "map_hints": session.map_hints,  # Pre-fetched at mission start
                    "client_id": session.tenant_id or "tara",
                    "session_id": session.session_id,
                    "dom_diff": dom_diff, # Context awareness
                    "conversation_history": conversation_history,  # Recent user requests
                    "last_dom_context": session.last_dom_context[:50] if session.last_dom_context else None,
                    "fast_sense_speech": fast_speech if is_first_step and fast_speech else None,
                    "interaction_mode": getattr(session, "interaction_mode", "interactive"),
                    # v5: Semantic Page Graph fields
                    "active_states": active_states,
                    "data_tables": data_tables,
                    "page_title": page_title,
                }
            )

            response.raise_for_status()
            plan = response.json()

            # Inject fast_sense speech into plan metadata
            if fast_response and fast_response.get("speech"):
                plan["_fast_speech"] = fast_response["speech"]

            return plan

    # ═══════════════════════════════════════════════════════════════════════════════
    # FSM Helper Methods (Schema-Driven Appointment Booking)
    # ═══════════════════════════════════════════════════════════════════════════════

    def _get_fsm_pending_field(self, fsm) -> Optional[str]:
        """
        Extract pending field from FSM state.
        Maps FSM state to field name for router context.
        """
        if not fsm or not hasattr(fsm, 'state'):
            return None
        
        state_name = fsm.state.name if hasattr(fsm.state, 'name') else str(fsm.state)
        
        # Map states to fields
        if 'NAME' in state_name:
            return 'name'
        elif 'EMAIL' in state_name:
            return 'email'
        elif 'QUERY' in state_name:
            return 'topic'
        
        return None

    def _get_fsm_schema(self) -> Dict[str, Any]:
        """
        Build FSM schema from orchestrator config.
        Returns schema dict for router validation.
        """
        schema = deepcopy(DEFAULT_V1_SCHEMA)
        raw_schema = getattr(self.config.fsm, "appointment_schema_json", "") or ""
        if raw_schema:
            try:
                parsed = json.loads(raw_schema)
                if isinstance(parsed, dict):
                    parsed_fields = parsed.get("fields", {})
                    if isinstance(parsed_fields, dict):
                        for field_name, field_cfg in parsed_fields.items():
                            if field_name not in schema["fields"] or not isinstance(field_cfg, dict):
                                schema["fields"][field_name] = field_cfg
                            else:
                                schema["fields"][field_name].update(field_cfg)
                    for key in ("cancel_keywords", "max_retries", "fallback_messages", "resume_prompt_template"):
                        if key in parsed and parsed[key]:
                            schema[key] = parsed[key]
            except json.JSONDecodeError as exc:
                logger.warning(f"Failed to parse FSM_APPOINTMENT_SCHEMA_JSON from config: {exc}")

        # Apply explicit FSM config overrides
        if getattr(self.config.fsm, "cancel_keywords", None):
            schema["cancel_keywords"] = self.config.fsm.cancel_keywords
        schema["max_retries"] = int(getattr(self.config.fsm, "max_retries", schema.get("max_retries", 3)))
        return schema

    def _get_fsm_detour_counts(self, session: OrchestratorSession) -> Dict[str, int]:
        counts = session.metadata.get("fsm_detour_counts")
        if not isinstance(counts, dict):
            counts = {}
            session.metadata["fsm_detour_counts"] = counts
        return counts

    def _can_take_fsm_detour(self, session: OrchestratorSession, field_name: str) -> bool:
        limit = max(int(getattr(self.config.fsm, "max_detours_per_field", 0)), 0)
        if limit == 0:
            return True
        counts = self._get_fsm_detour_counts(session)
        return int(counts.get(field_name, 0)) < limit

    def _increment_fsm_detour_count(self, session: OrchestratorSession, field_name: str) -> None:
        counts = self._get_fsm_detour_counts(session)
        counts[field_name] = int(counts.get(field_name, 0)) + 1

    def _reset_fsm_detour_count(self, session: OrchestratorSession, field_name: str) -> None:
        counts = self._get_fsm_detour_counts(session)
        counts[field_name] = 0

    async def _process_rag_detour_and_resume(
        self,
        session: OrchestratorSession,
        user_text: str,
        output_language: str,
        resume_prompt: Optional[str]
    ):
        """
        Process a RAG detour during FSM flow, then resume with pending FSM question.
        
        Flow:
        1. Answer user's question via RAG
        2. Immediately follow up with resume prompt
        """
        try:
            # Get history context from history manager
            history_context = session.history_manager.get_context_window(max_turns=5)
            
            # Stream RAG response for the detour question
            logger.info(f"[{session.session_id}] 🔄 Starting RAG detour for: '{user_text[:50]}...'")
            
            rag_client = RAGClient(self.config.services.rag, skip_ssl=self.config.server.skip_ssl_verify)
            full_response = ""
            
            async for token in rag_client.query_streaming(
                query=user_text,
                session_id=session.session_id,
                user_id=session.user_id,
                agent_name=session.agent_name,
                language=output_language,
                context={"language": output_language},
                history_context=history_context,
                base_url=session.rag_url,
                tenant_id=session.tenant_id
            ):
                if isinstance(token, str):
                    full_response += token
                    logger.debug(f"[{session.session_id}] RAG detour token: '{token}'")
            
            # Send RAG response to TTS
            if full_response:
                logger.info(f"[{session.session_id}] 📤 Streaming RAG detour response ({len(full_response)} chars)")
                await self._stream_fsm_response(session, full_response, output_language)
            
            # Now resume FSM with pending question
            if resume_prompt:
                logger.info(f"[{session.session_id}] 🔄 Resuming FSM: '{resume_prompt[:50]}...'")
                # Small delay to separate detour answer from resume prompt
                await asyncio.sleep(0.3)
                await self._stream_fsm_response(session, resume_prompt, output_language)
            
        except Exception as e:
            logger.error(f"[{session.session_id}] RAG detour error: {e}", exc_info=True)
            # Fallback: just resume FSM
            if resume_prompt:
                await self._stream_fsm_response(session, resume_prompt, output_language)


@dataclass
class PhoneOrchestratorSession(OrchestratorSession):
    """Phone-specific session extending base OrchestratorSession"""

    call_sid: str = ""
    audio_format: str = "g711"  # Phone audio format

    def __post_init__(self):
        super().__post_init__()
        # Phone sessions don't need UI state updates
        self.stt_mode = "audio"
        self.tts_mode = "audio"

    async def send_audio_chunk(self, audio_bytes: bytes):
        """Override to convert PCM → phone format"""

        if self.audio_format == "g711":
            # Convert PCM to G.711 μ-law for phone
            phone_audio = self.convert_pcm_to_g711(audio_bytes)
            # CRITICAL: Use lock for thread-safety to avoid AssertionError in websockets
            async with self.send_lock:
                await self.websocket.send_bytes(phone_audio)
        else:
            # Default behavior
            await super().send_audio_chunk(audio_bytes)

    def convert_pcm_to_g711(self, pcm_bytes: bytes) -> bytes:
        """Convert 16-bit PCM to G.711 μ-law"""
        # This is a placeholder - you'd need proper G.711 encoding
        # For now, just pass through (Twilio handles format conversion)
        logger.debug(f"Phone audio conversion: {len(pcm_bytes)} bytes")
        return pcm_bytes

    def convert_g711_to_pcm(self, g711_bytes: bytes) -> bytes:
        """Convert G.711 μ-law to 16-bit PCM"""
        # This is a placeholder - you'd need proper G.711 decoding
        # For now, just pass through (Twilio sends PCM)
        logger.debug(f"Phone audio received: {len(g711_bytes)} bytes")
        return g711_bytes

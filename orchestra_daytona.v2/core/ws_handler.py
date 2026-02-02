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
from datetime import datetime
import base64
import uuid
import secrets
import wave
import numpy as np
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from fastapi import WebSocket, WebSocketDisconnect

from core.state_manager import StateManager, State
from core.service_client import STTClient, TTSClient
from core.pipeline import ProcessingPipeline
from core.history_manager import HistoryManager
from dialogue.manager import MultiLangDialogueManager, DialogueType
from utils.lang_detect import detect_language
from fsm.appointment_fsm import SimpleAppointmentFSM
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

    # Host-based configuration overrides
    agent_name: str = "demo"
    agent_id: Optional[str] = None
    tenant_id: Optional[str] = None
    secondary_language: Optional[str] = None
    flow_config: Dict[str, Any] = field(default_factory=dict)
    rag_url: Optional[str] = None
    voice_id_override: Optional[str] = None

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

        stream_out = self.config.languages.stream_out.lower()

        if stream_out == "auto":
            # Use detected language, fallback to default
            return detected_language or self.config.languages.default
        elif stream_out in ["en", "de"]:
            # Force specific language regardless of input
            return stream_out
        else:
            # Invalid value, fallback to auto behavior
            logger.warning(f"Invalid stream_out value '{stream_out}', using auto mode")
            return detected_language or self.config.languages.default

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
        
        Naming Convention: <agent_name>.davinciai.eu
        RAG Service: rag.<agent_name>.davinciai.eu
        """
        # User defined defaults
        default_agent = "demo"
        # PRORITY: Use CARTESIA_VOICE_ID env var, then config.yaml, then hardcoded fallback
        default_voice = os.getenv("CARTESIA_VOICE_ID")
        
        if not default_voice and self.config.services.tts.voices.get("en"):
            default_voice = self.config.services.tts.voices["en"].voice_id
            
        if not default_voice or default_voice == "default":
            default_voice = "a0e99841-438c-4a64-b679-ae501e7d6091" # Standard Cartesia English
        
        config = {
            "agent_name": default_agent,
            "rag_url": f"https://rag.{default_agent}.davinciai.eu:8444",
            "voice_id": default_voice
        }

        if not host:
            return config

        # Clean host (remove port if present)
        clean_host = host.split(":")[0].lower()
        
        # Extract agent name from subdomain
        # e.g., partner.davinciai.eu -> partner
        agent_name = default_agent
        if "davinciai.eu" in clean_host:
            parts = clean_host.split(".")
            if len(parts) >= 3:
                agent_name = parts[0]
            
            # Construct RAG URL based on inferred agent name
            config["agent_name"] = agent_name
            config["rag_url"] = f"https://rag.{agent_name}.davinciai.eu:8444"
        
        # Specific Voice ID Mapping overrides
        VOICE_MAPPING = {
            "demo": default_voice,
            "daytona": "pfQ98Pt886VAnX66yHAt", # Keep existing override if needed
        }
        
        config["voice_id"] = VOICE_MAPPING.get(agent_name, default_voice)
        
        logger.info(f"🌐 Host Routing | Host: {clean_host} | Agent: {agent_name} | RAG: {config['rag_url']} | Voice: {config['voice_id']}")

        return config

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
        if source in ["stt_groq", "stt_groq_whisper"]:
            logger.warning(f"🔍 [{session_id}] Internal service '{source}' connected. Preventing session hijack.")
            await websocket.accept()
            # We accept the connection but DO NOT assign it to the session.
            # We just hold it open to prevent the service from retrying aggressively
            # or we could implement a separate handler if we needed its push events.
            try:
                while True:
                    await asyncio.sleep(60) # Keep alive
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
        session.agent_name = query_params.get("agent_name") or host_config["agent_name"]
        session.rag_url = query_params.get("rag_url") or host_config["rag_url"]
        session.voice_id_override = query_params.get("voice_id") or host_config["voice_id"]
        
        if query_params.get("rag_url"):
            logger.info(f"[{session_id}] 🚀 DYNAMIC RAG OVERRIDE: {session.rag_url}")
        if query_params.get("voice_id"):
            logger.info(f"[{session_id}] 🎤 DYNAMIC VOICE OVERRIDE: {session.voice_id_override}")

        logger.info(f"[{session_id}] 🌐 Final Session Config: RAG={session.rag_url}, Voice={session.voice_id_override}")

        session.stt_client = STTClient(self.config.services.stt, skip_ssl=self.config.server.skip_ssl_verify)
        session.tts_client = TTSClient(self.config.services.tts, skip_ssl=self.config.server.skip_ssl_verify)

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

        # Send session ready notification with service metadata
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
    
    async def _create_session(self, websocket: WebSocket, session_id: Optional[str] = None, user_id: Optional[str] = None) -> OrchestratorSession:
        """Create new session"""
        if session_id is None:
            session_id = f"daytona_{uuid.uuid4().hex[:12]}"
        
        state_mgr = StateManager(session_id, self.redis_client)
        await state_mgr.initialize()
        
        session = OrchestratorSession(
            session_id=session_id,
            websocket=websocket,
            state_manager=state_mgr,
            user_id=user_id
        )
        
        self.sessions[session_id] = session
        return session
    
    async def _route_message(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Route incoming message to appropriate handler"""
        # Support both 'type' (standard) and 'action' (new format) identifiers
        msg_type = msg.get("type") or msg.get("action")
        
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
        elif msg_type == "change_language":
            await self._handle_change_language(session, msg)
        elif msg_type == "pong":
            # Heartbeat response - can be safely ignored
            pass
        else:
            logger.warning(f"[{session.session_id}] Unknown message type: {msg_type}")
    
    async def _handle_audio_chunk(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """Handle incoming audio chunk from browser"""
        # Check if session is closed
        if session.is_closed:
            return
        
        state_mgr = session.state_manager
        
        # CRITICAL: Do NOT ignore audio during SPEAKING/THINKING to support barge-in
        # Forward everything to STT so it can detect user speech
        
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
        
        # CRITICAL: Always forward audio to STT, even in THINKING state
        # This ensures the STT service can continue to receive complete utterances
        # and handle barge-in/interrupt scenarios properly
        # Only skip in SPEAKING state when we're actively playing TTS output
        if state_mgr.state == State.SPEAKING:
            # In SPEAKING state, we might want to detect interrupts
            # For now, still forward audio to enable barge-in detection
            pass
        
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
        
        while not session.is_closed:
            try:
                # Check connection status
                if not session.tts_client.ws or session.tts_client.ws.closed:
                    logger.info(f"[{session.session_id}] 🔌 Reconnecting to TTS service...")
                    connected = await session.tts_client.connect(session.session_id)
                    if not connected:
                        logger.warning(f"[{session.session_id}] ❌ Failed to reconnect to TTS service, retrying in 2s...")
                        session.tts_connected_event.clear()
                        await asyncio.sleep(2.0)
                        continue
                
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
                    await session.tts_audio_queue.put(data)

                    # Record first audio chunk time for TTFC
                    if "speech_end" in session.current_turn_timers and "first_audio" not in session.current_turn_timers:
                        session.current_turn_timers["first_audio"] = time.time()
                        ttfc = (session.current_turn_timers["first_audio"] - session.current_turn_timers["speech_end"]) * 1000
                        logger.info(f"[{session.session_id}] 🔊 TTFC: {ttfc:.1f}ms (Turn: {session.current_turn_timers.get('user_text', 'unknown')[:30]}...)")
                        
                        # UPDATE TURN METRICS (fix for 0.0 average in session report)
                        if session.turn_metrics:
                            # The metrics entry for this turn was already appended to turn_metrics
                            # in _process_user_input, but ttfc_ms was likely None at that time.
                            # We update the last entry if the user_text matches.
                            last_metrics = session.turn_metrics[-1]
                            if last_metrics.get("user_text") == session.current_turn_timers.get("user_text"):
                                last_metrics["ttfc_ms"] = ttfc
                                logger.debug(f"[{session.session_id}] 📊 Updated turn_metrics with TTFC: {ttfc:.1f}ms")
                
                if not session.is_closed:
                    logger.warning(f"[{session.session_id}] ⚠️ TTS connection dropped, attempting reconnection...")
                    session.tts_connected_event.clear()
                    await asyncio.sleep(1.0)
                    
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
                await self._broadcast_state(State.LISTENING)
            return
        
        session.last_activity = time.time()
        session.timeout_count = 0
        
        # Detect language
        detected_lang = detect_language(text, self.config.languages.supported)
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
        
        # INTERRUPT LOGIC: If user speaks during SPEAKING state, trigger interrupt
        state_mgr = session.state_manager
        if state_mgr.state == State.SPEAKING:
            # Check if it looks like real speech (ignore very short fragments if they are not final)
            if len(text.strip()) > 3 or is_final:
                logger.info(f"[{session.session_id}] 🛑 Interruption detected via STT ({'FINAL' if is_final else 'Fragment'}): '{text}'")
                await self._handle_interrupt(session, {"type": "interrupt", "trigger": "stt_activity"})
                # After interrupt, we continue to process the final transcript in LISTENING state
                # but we return here if it was a fragment to avoid duplicate processing later
                if not is_final:
                    return

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
            # React immediately if we're listening or if we're in SPEAKING (interruption backup)
            if state_mgr.state == State.LISTENING or state_mgr.state == State.SPEAKING:
                if state_mgr.state == State.SPEAKING:
                    logger.info(f"[{session.session_id}] 🛑 Interruption detected via VAD (SPEECH_END)")
                    await self._handle_interrupt(session, {"type": "interrupt", "trigger": "vad_end"})
                
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
            # BARGE-IN DETECTION: Trigger immediate interrupt on speech start
            state_mgr = session.state_manager
            if state_mgr.state == State.SPEAKING:
                logger.info(f"[{session.session_id}] 🛑 Interruption detected via VAD (SPEECH_START)")
                await self._handle_interrupt(session, {"type": "interrupt", "trigger": "vad_start"})
            elif state_mgr.state == State.THINKING:
                # NEW: If we were thinking/waiting for a transcript and user starts speaking again,
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
                if transition_trigger in ["playback_done_server_timer", "playback_done_auto"]:
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
            # ---- ACTIVE FSM: Route input to FSM ----
            logger.info(f"[{session.session_id}] 📅 FSM ACTIVE: Routing to appointment FSM")
            fsm_result = session.appointment_fsm.process_input(text)
            
            # Check if FSM is complete or cancelled
            if fsm_result.get("complete") or fsm_result.get("cancelled"):
                session.fsm_active = False
                session.appointment_fsm = None
                status = 'COMPLETE' if fsm_result.get('complete') else 'CANCELLED'
                logger.info(f"[{session.session_id}] 📅 FSM {status}: Returning to RAG mode")
                
                # If complete, send appointment data to client for EmailJS confirmation
                if fsm_result.get("complete") and fsm_result.get("data"):
                    appointment_data = fsm_result.get("data")
                    logger.info(f"[{session.session_id}] 📧 Sending appointment_complete for email confirmation")
                    await self._send_json(session.websocket, {
                        "type": "appointment_complete",
                        "appointment_data": appointment_data,
                        "timestamp": time.time()
                    }, session)
            
            # Send FSM response to TTS
            fsm_response = fsm_result.get("response", "")
            if fsm_response:
                await self._stream_fsm_response(session, fsm_response, output_language)
            return
        
        elif any(trigger in text.lower() for trigger in APPOINTMENT_TRIGGERS):
            # ---- START FSM: User wants to book appointment ----
            logger.info(f"[{session.session_id}] 📅 APPOINTMENT TRIGGER DETECTED: Starting FSM")
            session.appointment_fsm = SimpleAppointmentFSM()
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
                    await asyncio.wait_for(
                        self._stream_response_and_tts(session, text, language, history_context),
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
            
            # Connect to TTS if needed
            if not session.tts_client.ws or session.tts_client.ws.closed:
                await session.tts_client.connect(session.session_id)
            
            if session.tts_client.ws and not session.tts_client.ws.closed:
                # CRITICAL: Cancel any existing playback timer at the start of a new stream
                if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
                    session.audio_playback_server_timer.cancel()
                    logger.debug(f"[{session.session_id}] ⏸️ Cancelled existing playback timer for new FSM stream")
                
                # Send to TTS for voice output (using correct stream_chunk method)
                await session.tts_client.stream_chunk(response_text, language=output_language, voice_id=session.voice_id_override)
                await session.tts_client.stream_end()
                
                # Transition to SPEAKING
                speaking_side_effects = await state_mgr.transition(State.SPEAKING, trigger="fsm_audio")
                await self._broadcast_state(session, State.SPEAKING)
                
                # Receive and forward audio from TTS (same pattern as _stream_response_and_tts)
                audio_received = False
                session.tts_total_bytes = 0  # Reset for this response
                session.audio_playback_start_time = None # Reset timing
                
                async for data in session.tts_client.receive_audio():
                    if session.is_closed:
                        break
                    
                    msg_type = data.get("type")
                    
                    # Capture and store format configuration from TTS service
                    if msg_type == "connected":
                        session.tts_sample_rate = data.get("sample_rate", session.tts_sample_rate)
                        session.tts_format = data.get("format", session.tts_format)
                        logger.info(f"[{session.session_id}] 📨 TTS connected: {session.tts_sample_rate}Hz, {session.tts_format}")
                        continue
                    
                    if msg_type in ("prewarmed", "sentence_start", "sentence_playing", "pong", "timeout"):
                        continue
                    
                    if msg_type == "audio":
                        audio_data = data.get("data")
                        if audio_data:
                            audio_received = True
                            
                            if data.get("is_binary"):
                                audio_bytes = audio_data
                                audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
                            else:
                                audio_b64 = audio_data
                                audio_bytes = base64.b64decode(audio_b64)
                                
                            session.tts_total_bytes += len(audio_bytes)
                            
                            # Use buffered sending for smooth playback
                            sample_rate = data.get("sample_rate", session.tts_sample_rate)
                            encoding = data.get("format", session.tts_format)
                            await self._enqueue_foreground_audio(session, audio_bytes, sample_rate=sample_rate, encoding=encoding)
                            
                    elif msg_type in ("complete", "stream_complete", "sentence_complete"):
                        logger.debug(f"[{session.session_id}] ✅ FSM TTS stream complete")
                        break
                    elif msg_type == "error":
                        logger.error(f"[{session.session_id}] ❌ FSM TTS error: {data.get('message', 'unknown')}")
                        break
                
                # Signal audio complete
                if audio_received:
                    # Flush remaining buffer
                    await self._enqueue_foreground_audio(session, b"", flush=True, sample_rate=session.tts_sample_rate, encoding=session.tts_format)
                    
                    await self._send_json(session.websocket, {
                        "type": "audio_complete",
                        "timestamp": time.time()
                    }, session)
                    
                    # Calculate and wait for playback duration
                    duration_sec = session.tts_total_bytes / 48000.0  # 24kHz 16-bit mono
                    logger.info(f"[{session.session_id}] 🔊 FSM audio sent, waiting {duration_sec:.1f}s for playback")
                    await asyncio.sleep(max(duration_sec, 0.5))
            
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
        session.audio_playback_duration = None
        session.audio_playback_start_time = None
        
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
                language=output_language,  # Use output language for RAG response
                history_context=history_context,
                form_data=form_data,
                rag_url=session.rag_url,
                tenant_id=session.agent_name
            ):
                # Check if session is closed (client disconnected)
                if session.is_closed:
                    logger.info(f"[{session.session_id}] ⏸️ Session closed, stopping RAG streaming")
                    break

                token = token_data.get("token", "")

                # Skip empty tokens (RAG sends empty token to signal completion)
                if not token:
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
                
                await session.tts_client.stream_chunk(token, output_language, emotion="helpful", voice_id=session.voice_id_override)

            # Cancel latency fillers since RAG response is complete and TTS is about to start
            if session.filler_task and not session.filler_task.done():
                session.filler_task.cancel()
                try:
                    await asyncio.wait_for(session.filler_task, timeout=0.5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                logger.debug(f"[{session.session_id}] ⏸️ Cancelled latency filler - RAG response ready for TTS")

            # Signal end of text stream to TTS (triggers EOS to ElevenLabs)
            if not session.is_closed:
                logger.info(f"[{session.session_id}] 🔚 Sending stream_end to TTS (response: {len(response_text)} chars)")
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

                            # Update format info
                            sample_rate = data.get("sample_rate", session.tts_sample_rate)
                            encoding = data.get("format", session.tts_format)

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
    
    async def _stream_tts_to_browser(self, session: OrchestratorSession, text: str, language: str, cancellation_task: Optional[asyncio.Task] = None):
        """Stream TTS audio/text to browser with optional cancellation support"""
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
            
            # Default values to prevent UnboundLocalError if no audio chunks arrive
            sample_rate = 44100
            encoding = "pcm_f32le"
            
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
                
                if msg_type == "audio":
                    # INTERRUPTION LOGIC: Stop if state is no longer THINKING or SPEAKING
                    state_mgr = session.state_manager
                    if state_mgr.state not in (State.SPEAKING, State.THINKING):
                         logger.info(f"[{session.session_id}] 🛑 Stopping TTS receive loop - state changed to {state_mgr.state.value}")
                         break
                         
                    audio_data = data.get("data", "")
                    if audio_data:
                        # Track playback start on first chunk (for TTS streaming)
                        # Skip tracking for fillers in THINKING state (they have cancellation_task)
                        if not cancellation_task and session.audio_playback_start_time is None:
                            # CRITICAL: Flush any existing filler audio from buffer (use default rates for fillers)
                            await self._enqueue_foreground_audio(session, b"", flush=True, sample_rate=24000, encoding="pcm_s16le")
                            
                            session.audio_playback_start_time = time.time()
                            logger.debug(f"[{session.session_id}] 📊 Tracking TTS playback start")
                        
                        if data.get("is_binary"):
                            audio_bytes = audio_data
                        else:
                            audio_bytes = base64.b64decode(audio_data)

                        # Update format info based on dynamic metadata from TTS service
                        sample_rate = data.get("sample_rate", sample_rate)
                        encoding = data.get("format", encoding)

                        # Decode audio and buffer into aligned frames
                        await self._enqueue_foreground_audio(session, audio_bytes, sample_rate=sample_rate, encoding=encoding)
                        
                        # Accumulate bytes for accurate duration
                        if not cancellation_task:
                            if not hasattr(session, 'tts_total_bytes'):
                                session.tts_total_bytes = 0
                            session.tts_total_bytes += len(audio_bytes)
                            
                            # Update duration based on sample rate and format
                            bytes_per_sample = 4 if "f32" in encoding else 2
                            bytes_per_sec = sample_rate * bytes_per_sample
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
        Server uses this for accurate timing instead of relying on browser 'ended' events.
        """
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
            
            # Start server-side timer (PRIMARY mechanism)
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
        Start server-side timer for accurate playback completion.
        
        This is the PRIMARY mechanism - transitions to LISTENING based on server-side timing.
        Browser playback_done is only used as a safety net if timer fails.
        """
        # Cancel any existing timer
        if session.audio_playback_server_timer and not session.audio_playback_server_timer.done():
            session.audio_playback_server_timer.cancel()
            try:
                await asyncio.wait_for(session.audio_playback_server_timer, timeout=0.1)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        
        async def server_timer():
            try:
                # Wait for actual duration + 500ms buffer
                await asyncio.sleep(duration_sec + 0.5)
                
                # Check if still in SPEAKING and session not closed
                state_mgr = session.state_manager
                if state_mgr.state == State.SPEAKING and not session.is_closed:
                    # Reset activity timer so we don't timeout immediately after speaking
                    session.last_activity = time.time()
                    
                    logger.debug(f"[{session.session_id}] ⏰ Server-side timer: Transitioning to LISTENING (duration: {duration_sec:.2f}s)")
                    output_language = self._get_output_language(session.current_language, session)
                    listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="playback_done_server_timer")
                    await self._execute_side_effects(session, listening_side_effects, output_language)
                    await self._broadcast_state(session, State.LISTENING)
                    
                    # Clear playback tracking
                    session.audio_playback_start_time = None
                    session.audio_playback_duration = None
                    session.audio_playback_predicted_end = None
            except asyncio.CancelledError:
                logger.debug(f"[{session.session_id}] Server-side playback timer cancelled")
            except Exception as e:
                logger.error(f"[{session.session_id}] Server-side playback timer error: {e}", exc_info=True)
        
        session.audio_playback_server_timer = asyncio.create_task(server_timer())
    
    async def _handle_playback_done(self, session: OrchestratorSession, msg: Dict[str, Any]):
        """
        Handle browser confirming playback completion (SECONDARY mechanism).
        
        Server-side timing is PRIMARY. Browser playback_done is only accepted if:
        1. Server timer hasn't fired yet (safety net)
        2. Enough time has passed (not premature)
        
        This handles both 'playback_done' and 'playback_confirmed_end' messages.
        """
        state_mgr = session.state_manager
        session.last_activity = time.time()

        if state_mgr.state != State.SPEAKING:
            logger.debug(f"[{session.session_id}] Playback done in non-SPEAKING state (ignored)")
            return

        # Validate playback_done timing - ignore premature messages
        if session.audio_playback_start_time and session.audio_playback_duration:
            elapsed = time.time() - session.audio_playback_start_time
            if elapsed < session.audio_playback_duration:
                # Premature playback_done - ignore it (server-side timer will handle it)
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
            logger.debug(f"[{session.session_id}] ⏸️ Cancelled server-side timer - browser confirmed playback done")
        
        logger.info(f"[{session.session_id}] ✅ Playback DONE (browser confirmed - secondary mechanism)")

        # Clear playback tracking
        session.audio_playback_start_time = None
        session.audio_playback_duration = None
        session.audio_playback_predicted_end = None

        # Transition back to LISTENING
        logger.info(f"[{session.session_id}] 🔄 LISTENING: Response complete, ready for next input")
        output_language = self._get_output_language(session.current_language, session)
        
        # Reset transcript tracking flags when returning to LISTENING
        session.pending_transcript = False
        session.partial_transcript = ""
        
        # Cancel transcript timeout task if running
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
            
            listening_side_effects = await state_mgr.transition(State.LISTENING, trigger="interrupt_complete")
            await self._execute_side_effects(session, listening_side_effects, output_language)
        
        await self._broadcast_state(session, State.LISTENING)
    
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

        # 5. Play Introduction (Prioritize flow_config)
        intro_text = None
        if output_language == session.current_language:
            intro_text = flow_config.get("intro_in_primary_lang")
        elif output_language == session.secondary_language:
            intro_text = flow_config.get("intro_in_secondary_lang")

        if intro_text:
            logger.info(f"[{session.session_id}] 🔊 Playing custom intro from flow_config ({output_language})")
            # Transition to SPEAKING
            speaking_side_effects = await session.state_manager.transition(State.SPEAKING, trigger="intro_start", data={"response": intro_text})
            await self._execute_side_effects(session, speaking_side_effects, output_language)
            await self._broadcast_state(session, State.SPEAKING)
            session.tts_task = asyncio.create_task(
                self._stream_tts_to_browser(session, intro_text, output_language)
            )
        else:
            # Fallback to dialogue manager assets
            intro_asset = self.dialogue_manager.get_intro(output_language)
            if not intro_asset:
                logger.warning(f"[{session.session_id}] ⚠️ No intro asset found for language '{output_language}', using fallback")
                fallback_text = f"Hello! Welcome to {session.agent_name}."
                # Transition to SPEAKING
                speaking_side_effects = await session.state_manager.transition(State.SPEAKING, trigger="intro_start")
                await self._execute_side_effects(session, speaking_side_effects, output_language)
                await self._broadcast_state(session, State.SPEAKING)
                session.tts_task = asyncio.create_task(
                    self._stream_tts_to_browser(session, fallback_text, output_language)
                )
            else:
                # Transition to SPEAKING (with side effects: cancel_filler)
                speaking_side_effects = await session.state_manager.transition(State.SPEAKING, trigger="intro_start", data={"response": intro_asset.text})
                await self._execute_side_effects(session, speaking_side_effects, output_language)
                await self._broadcast_state(session, State.SPEAKING)
                
                # Prefer audio file if available
                if intro_asset.has_audio():
                    logger.info(f"[{session.session_id}] 💿 Using pre-generated intro: {intro_asset.audio_path}")
                    session.tts_task = asyncio.create_task(self._stream_audio_file(session, intro_asset.audio_path))
                else:
                    logger.info(f"[{session.session_id}] 🔊 Generating intro via TTS: {intro_asset.text[:50]}...")
                    session.tts_task = asyncio.create_task(
                        self._stream_tts_to_browser(session, intro_asset.text, output_language)
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
        
        # Get exit message (use output language)
        exit_asset = self.dialogue_manager.get_exit(output_language)
        if exit_asset:
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
            
            # Prefer audio file if available
            if exit_asset.has_audio():
                logger.info(f"[{session.session_id}] 💿 Using pre-generated exit: {exit_asset.audio_path}")
                await self._stream_audio_file(session, exit_asset.audio_path)
            else:
                logger.info(f"[{session.session_id}] 🔊 Generating exit via TTS: {exit_asset.text[:50]}...")
                await self._stream_tts_to_browser(session, exit_asset.text, output_language)
            
            # Wait for exit audio to finish playing before closing
            if session.tts_task and not session.tts_task.done():
                try:
                    await session.tts_task
                except asyncio.CancelledError:
                    pass
            # Small delay to ensure audio completes
            await asyncio.sleep(0.5)

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
        """
        logger.info(f"[{session.session_id}] ❤️ KeepAlive loop started")
        while not session.is_closed:
            try:
                await asyncio.sleep(20.0)  # Send every 20 seconds (well below 60s standard timeout)
                
                if session.is_closed:
                    break
                    
                # Send explicit ping message
                await self._send_json(session.websocket, {
                    "type": "ping",
                    "timestamp": time.time()
                }, session)
                
                logger.debug(f"[{session.session_id}] 💓 Keep-alive ping sent")
                
            except Exception as e:
                if not session.is_closed:
                    logger.warning(f"[{session.session_id}] KeepAlive failed: {e}")
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
    
    async def _enqueue_foreground_audio(self, session: OrchestratorSession, audio_bytes: bytes, flush: bool = False, sample_rate: int = 44100, encoding: str = "pcm_s16le"):
        """
        Buffer audio into aligned frames (100ms = 4410 samples @ 44.1kHz).
        
        Prevents crackling caused by padding small intermediate TTS chunks.
        Ensures chunks are always exactly frame-aligned.
        """
        bytes_per_sample = 4 if "f32" in encoding else 2
        frame_size_bytes = int(sample_rate * 0.1) * bytes_per_sample
        chunk_counter = 0
        
        # Add new bytes to buffer
        if audio_bytes:
            session.fg_buffer.extend(audio_bytes)
        
        # Process full frames from buffer
        while len(session.fg_buffer) >= frame_size_bytes:
            # Extract full frame
            chunk = bytes(session.fg_buffer[:frame_size_bytes])
            del session.fg_buffer[:frame_size_bytes]
            
            # Send chunk to browser
            audio_b64 = base64.b64encode(chunk).decode('utf-8')
            if not await self._send_json(session.websocket, {
                "type": "audio_chunk",
                "audio": audio_b64,
                "sample_rate": sample_rate,
                "format": encoding,
                "chunk_id": f"{session.session_id}_chunk_{chunk_counter}",
                "is_final": False,
                "timestamp": time.time()
            }, session):
                return  # Client disconnected
            chunk_counter += 1
        
        # Handle flush (pad remainder)
        if flush and len(session.fg_buffer) > 0:
            remaining = len(session.fg_buffer)
            padding = b'\x00' * (frame_size_bytes - remaining)
            chunk = bytes(session.fg_buffer) + padding
            session.fg_buffer.clear()
            
            # Send final chunk
            audio_b64 = base64.b64encode(chunk).decode('utf-8')
            await self._send_json(session.websocket, {
                "type": "audio_chunk",
                "audio": audio_b64,
                "sample_rate": sample_rate,
                "format": encoding,
                "chunk_id": f"{session.session_id}_chunk_{chunk_counter}",
                "is_final": False,
                "timestamp": time.time()
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

        # Create phone session
        session = PhoneOrchestratorSession(
            session_id=session_id,
            websocket=websocket,
            call_sid=call_sid,
            audio_format="g711"
        )

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
            "start_time": datetime.fromtimestamp(session.created_at).isoformat(),
            "end_time": datetime.fromtimestamp(now_ts).isoformat(),
            "timestamp": datetime.fromtimestamp(now_ts).isoformat(),
            "duration_seconds": now_ts - session.created_at,
            "total_turns": len(session.turn_metrics),
            "avg_ttft_ms": 0.0,
            "avg_ttfc_ms": 0.0,
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
                                "tenant_id": session.tenant_id or "demo",
                                "metadata": {
                                    "agent_name": session.agent_name,
                                    "agent_id": session.agent_id,
                                    "source": "orchestrator_cleanup"
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


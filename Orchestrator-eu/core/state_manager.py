"""
Simplified State Manager for Orchestra-daytona

Manages conversation state transitions: IDLE → LISTENING → THINKING → SPEAKING → INTERRUPT
"""

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


class State(Enum):
    """Conversation states"""
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPT = "interrupt"


class StateContract:
    """Define what MUST happen when entering each state."""
    
    # Map state value (str) to contract details
    CONTRACTS = {
        State.IDLE.value: {
            "microphone": "CLOSED",
            "audio_playback": "STOPPED",
            "side_effects": [],
        },
        State.LISTENING.value: {
            "microphone": "OPEN",
            "audio_playback": "STOPPED",
            "side_effects": ["play_post_response_prompt"],
        },
        State.THINKING.value: {
            "microphone": "GATED",
            "audio_playback": "STOPPED",
            "side_effects": ["play_immediate_filler"],
        },
        State.SPEAKING.value: {
            "microphone": "GATED",
            "audio_playback": "STREAMING",
            "side_effects": ["cancel_filler"],
        },
        State.INTERRUPT.value: {
            "microphone": "GATED",
            "audio_playback": "STOPPED",
            "side_effects": ["cancel_tts", "cancel_filler"],
        }
    }
    
    @classmethod
    def get(cls, state: State) -> dict:
        return cls.CONTRACTS.get(state.value, {})


@dataclass
class ConversationContext:
    """Persistent conversation state"""
    session_id: str
    state: str = "idle"
    turn_number: int = 0
    last_activity_time: Optional[float] = None
    current_language: str = "en"
    
    def __post_init__(self):
        if self.last_activity_time is None:
            self.last_activity_time = time.time()


class StateManager:
    """
    Simplified FSM for real-time voice conversations.
    
    Manages state transitions with optional Redis persistence.
    """
    
    def __init__(self, session_id: str, redis_client=None):
        self.session_id = session_id
        self.redis = redis_client
        self.state = State.IDLE
        self.last_transition_trigger = None  # Track the trigger that caused the last transition
        self._lock = asyncio.Lock()  # Ensure atomic transitions
        self.context = ConversationContext(
            session_id=session_id,
            state=State.IDLE.value
        )

        # Valid state transitions
        self.valid_transitions = {
            State.IDLE: [State.LISTENING, State.SPEAKING],
            State.LISTENING: [State.THINKING, State.INTERRUPT, State.IDLE, State.SPEAKING],
            State.THINKING: [State.SPEAKING, State.IDLE, State.LISTENING],
            State.SPEAKING: [State.LISTENING, State.INTERRUPT, State.IDLE],
            State.INTERRUPT: [State.LISTENING, State.IDLE],
        }
    
    async def initialize(self):
        """Load session state from Redis or create new"""
        redis_key = f"orchestra_daytona:session:{self.session_id}"
        
        try:
            if self.redis:
                existing = await self.redis.hgetall(redis_key)
                if existing:
                    logger.info(f"[{self.session_id}] ✅ Loaded session from Redis")
                    self.context.state = existing.get("state", "idle")
                    self.context.turn_number = int(existing.get("turn_number", 0))
                    self.context.current_language = existing.get("current_language", "en")
                    if existing.get("last_activity_time"):
                        self.context.last_activity_time = float(existing.get("last_activity_time", time.time()))
                    try:
                        self.state = State(self.context.state)
                    except ValueError:
                        logger.warning(f"Invalid state in Redis: {self.context.state}, resetting to IDLE")
                        self.state = State.IDLE
                        self.context.state = State.IDLE.value
                else:
                    logger.info(f"[{self.session_id}] 🆕 Created new session")
                    await self.save_state()
        except Exception as e:
            logger.warning(f"[{self.session_id}] ⚠️ Redis load failed: {e}")
    
    async def transition(self, new_state: State, trigger: str, data: Optional[Dict] = None) -> list:
        """
        Atomic state transition with logging and Redis persistence.
        
        Args:
            new_state: Target state
            trigger: Transition trigger (e.g., "stt_final", "response_ready")
            data: Optional context data
        
        Returns:
            List of side effects to execute (e.g., ["play_immediate_filler"])
        """
        async with self._lock:
            old_state = self.state
            
            # Validate transition
            if new_state not in self.valid_transitions.get(old_state, []):
                if new_state == State.IDLE and trigger == "error":
                    pass  # Allow resetting to IDLE from anywhere on error
                else:
                    logger.error(
                        f"[{self.session_id}] ❌ INVALID TRANSITION: "
                        f"{old_state.value.upper()} → {new_state.value.upper()} "
                        f"(trigger: {trigger})"
                    )
                    return []
            
            # Update state
            self.state = new_state
            self.last_transition_trigger = trigger  # Track the trigger for side effects
            self.context.state = new_state.value
            self.context.last_activity_time = time.time()
            
            # Update context with data
            if data:
                if "language" in data:
                    self.context.current_language = data["language"]
                if "turn_number" in data:
                    self.context.turn_number = data["turn_number"]
            
            logger.info(f"[{self.session_id}] 🔄 {old_state.value.upper()} → {new_state.value.upper()} | {trigger}")
            
            # Persist to Redis
            await self.save_state()
            
            # Return side effects that need to be executed
            contract = StateContract.get(new_state)
            side_effects = contract.get("side_effects", [])
            return side_effects
    
    async def save_state(self):
        """Save current state to Redis"""
        if not self.redis:
            return
        
        try:
            redis_key = f"orchestra_daytona:session:{self.session_id}"
            await self.redis.hset(redis_key, mapping={
                "state": self.context.state,
                "turn_number": str(self.context.turn_number),
                "current_language": self.context.current_language,
                "last_activity_time": str(self.context.last_activity_time or time.time())
            })
            await self.redis.expire(redis_key, 3600)  # 1 hour TTL
        except Exception as e:
            logger.warning(f"[{self.session_id}] ⚠️ Redis save failed: {e}")




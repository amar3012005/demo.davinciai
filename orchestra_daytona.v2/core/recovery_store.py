"""
Recovery Store - Redis-backed recovery state storage for TARA Visual Copilot.

This module provides persistent storage for mission state, page position, and action history.
The backend is the canonical owner of all mission state, enabling robust reload/crash resilience.

Redis Keys:
- tara:session:{session_id}:recovery - RecoveryState per session
- tara:mission:{mission_id}:recovery - RecoveryState per mission (indexed)

TTL: 24 hours for active sessions
"""

import asyncio
import json
import logging
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _decode_json_maybe(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode()
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped == "null":
        return None
    if stripped[0] in "[{":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


class RecoveryStatus(str, Enum):
    """Mission status enumeration"""
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class MissionPhase(str, Enum):
    """Mission phase enumeration"""
    STRATEGY = "strategy"
    LAST_MILE = "last_mile"
    DONE = "done"


@dataclass
class PageNodeRef:
    """
    Reference to a page node in the site map hierarchy.
    
    Attributes:
        node_id: Unique identifier for the node
        logical_path: Hierarchical path (e.g., "root.console.playground")
        source: Origin of the node ("static_map" or "dynamic_runtime")
        url_pattern: Regex pattern for matching URLs
        current_url: Current browser URL
        expected_controls: List of expected UI controls on this page
        parent_node_id: Parent node ID in hierarchy
    """
    node_id: str = ""
    logical_path: str = ""
    source: str = "static_map"  # "static_map" | "dynamic_runtime"
    url_pattern: str = ""
    current_url: str = ""
    expected_controls: List[str] = field(default_factory=list)
    parent_node_id: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PageNodeRef":
        """Create from dictionary"""
        return cls(
            node_id=data.get("node_id", ""),
            logical_path=data.get("logical_path", ""),
            source=data.get("source", "static_map"),
            url_pattern=data.get("url_pattern", ""),
            current_url=data.get("current_url", ""),
            expected_controls=data.get("expected_controls", []),
            parent_node_id=data.get("parent_node_id", "")
        )


@dataclass
class RecoveryState:
    """
    Complete recovery state for a mission session.
    
    This is the canonical source of truth for mission state, stored in Redis.
    After a reload or crash, the backend uses this to resume missions without
    relying on browser-restored counters.
    
    Attributes:
        session_id: Unique session identifier
        mission_id: Unique mission identifier
        goal: Natural language mission goal
        status: Current mission status
        phase: Current mission phase
        step_count: Number of actions executed
        subgoal_index: Current subgoal index
        page_node: Current page node reference
        current_url: Current browser URL
        pending_pipeline: Pending multi-action pipeline
        latest_dom_signature: DOM signature for change detection
        last_stable_dom_at: Timestamp of last stable DOM
        resume_count: Number of times session has been resumed
        updated_at: Last update timestamp
    """
    session_id: str = ""
    mission_id: str = ""
    goal: str = ""
    status: str = RecoveryStatus.IN_PROGRESS.value
    phase: str = MissionPhase.STRATEGY.value
    step_count: int = 0
    subgoal_index: int = 0
    page_node: Optional[PageNodeRef] = None
    current_url: str = ""
    pending_pipeline: Optional[Dict[str, Any]] = None
    latest_dom_signature: str = ""
    last_stable_dom_at: float = 0.0
    resume_count: int = 0
    updated_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage"""
        data = asdict(self)
        # Convert PageNodeRef to dict if present
        if self.page_node and isinstance(self.page_node, PageNodeRef):
            data["page_node"] = self.page_node.to_dict()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecoveryState":
        """Create from dictionary (loaded from Redis)"""
        normalized = {k: _decode_json_maybe(v) for k, v in data.items()}
        # Handle page_node conversion
        page_node = None
        if normalized.get("page_node"):
            if isinstance(normalized["page_node"], dict):
                page_node = PageNodeRef.from_dict(normalized["page_node"])
            elif isinstance(normalized["page_node"], str):
                # Handle case where it's stored as JSON string
                try:
                    page_node = PageNodeRef.from_dict(json.loads(normalized["page_node"]))
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse page_node JSON: {normalized['page_node']}")
        
        return cls(
            session_id=normalized.get("session_id", ""),
            mission_id=normalized.get("mission_id", ""),
            goal=normalized.get("goal", ""),
            status=normalized.get("status", RecoveryStatus.IN_PROGRESS.value),
            phase=normalized.get("phase", MissionPhase.STRATEGY.value),
            step_count=_coerce_int(normalized.get("step_count"), 0),
            subgoal_index=_coerce_int(normalized.get("subgoal_index"), 0),
            page_node=page_node,
            current_url=normalized.get("current_url", ""),
            pending_pipeline=_decode_json_maybe(normalized.get("pending_pipeline")),
            latest_dom_signature=normalized.get("latest_dom_signature", ""),
            last_stable_dom_at=_coerce_float(normalized.get("last_stable_dom_at"), 0.0),
            resume_count=_coerce_int(normalized.get("resume_count"), 0),
            updated_at=_coerce_float(normalized.get("updated_at"), time.time())
        )


class RecoveryStore:
    """
    Redis-backed storage for recovery state.
    
    Provides methods to save, load, and clear recovery state for sessions and missions.
    Uses Redis hashes for structured storage with 24-hour TTL for active sessions.
    
    Usage:
        store = RecoveryStore(redis_client)
        state = RecoveryState(session_id="abc123", mission_id="m-001", goal="Find models")
        await store.save_recovery_state(state)
        
        loaded = await store.load_recovery_state(session_id="abc123")
    """
    
    # TTL: 24 hours for active sessions
    SESSION_TTL = 24 * 60 * 60  # 86400 seconds
    
    # Key prefixes
    SESSION_KEY_PREFIX = "tara:session"
    MISSION_KEY_PREFIX = "tara:mission"
    
    def __init__(self, redis_client):
        """
        Initialize recovery store.
        
        Args:
            redis_client: Async Redis client (aioredis or redis.asyncio)
        """
        self.redis = redis_client
        logger.info("RecoveryStore initialized")
    
    def _session_key(self, session_id: str) -> str:
        """Generate Redis key for session recovery state"""
        return f"{self.SESSION_KEY_PREFIX}:{session_id}:recovery"
    
    def _mission_key(self, mission_id: str) -> str:
        """Generate Redis key for mission recovery state"""
        return f"{self.MISSION_KEY_PREFIX}:{mission_id}:recovery"
    
    async def save_recovery_state(self, state: RecoveryState) -> bool:
        """
        Save recovery state to Redis.

        Stores state under both session and mission keys for flexible lookup.
        Updates the timestamp and sets TTL for automatic expiration.
        Uses exponential backoff retry logic for resilience.

        Args:
            state: RecoveryState object to persist

        Returns:
            True if save successful, False otherwise

        Example:
            >>> state = RecoveryState(session_id="s123", mission_id="m456", goal="Test")
            >>> await store.save_recovery_state(state)
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False

        # Retry loop with exponential backoff
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Update timestamp
                state.updated_at = time.time()

                # Convert to dict for Redis hash storage
                state_dict = state.to_dict()

                # Save under session key
                session_key = self._session_key(state.session_id)
                await self.redis.hset(session_key, mapping={
                    k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                    for k, v in state_dict.items()
                })
                await self.redis.expire(session_key, self.SESSION_TTL)

                # Save under mission key (for mission-based lookup)
                if state.mission_id:
                    mission_key = self._mission_key(state.mission_id)
                    await self.redis.hset(mission_key, mapping={
                        k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                        for k, v in state_dict.items()
                    })
                    await self.redis.expire(mission_key, self.SESSION_TTL)

                logger.info(
                    f"💾 Recovery state saved | session={state.session_id} | "
                    f"mission={state.mission_id} | phase={state.phase} | "
                    f"step={state.step_count} | url={state.current_url[:50]}..."
                )
                return True

            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to save recovery state after {max_retries} attempts: {e}", exc_info=True)
                    return False
                backoff_time = 2 ** attempt
                logger.warning(f"Redis save failed (attempt {attempt+1}/{max_retries}), retrying in {backoff_time}s: {e}")
                await asyncio.sleep(backoff_time)

        return False
    
    async def load_recovery_state(
        self,
        session_id: Optional[str] = None,
        mission_id: Optional[str] = None
    ) -> Optional[RecoveryState]:
        """
        Load recovery state from Redis.
        
        Can lookup by session_id or mission_id. Session lookup takes precedence
        if both are provided.
        
        Args:
            session_id: Session identifier (optional)
            mission_id: Mission identifier (optional)
            
        Returns:
            RecoveryState if found, None otherwise
            
        Example:
            >>> state = await store.load_recovery_state(session_id="s123")
            >>> if state:
            ...     print(f"Mission {state.mission_id} is {state.status}")
        """
        if not self.redis:
            logger.debug("Redis client not available")
            return None
        
        if not session_id and not mission_id:
            logger.warning("Either session_id or mission_id must be provided")
            return None
        
        try:
            # Try session lookup first
            if session_id:
                session_key = self._session_key(session_id)
                data = await self.redis.hgetall(session_key)
                if data:
                    # Decode bytes to strings if needed
                    decoded = {
                        k.decode() if isinstance(k, bytes) else k: 
                        v.decode() if isinstance(v, bytes) else v
                        for k, v in data.items()
                    }
                    state = RecoveryState.from_dict(decoded)
                    logger.info(
                        f"📖 Recovery state loaded | session={session_id} | "
                        f"mission={state.mission_id} | phase={state.phase}"
                    )
                    return state
            
            # Fallback to mission lookup
            if mission_id:
                mission_key = self._mission_key(mission_id)
                data = await self.redis.hgetall(mission_key)
                if data:
                    decoded = {
                        k.decode() if isinstance(k, bytes) else k: 
                        v.decode() if isinstance(v, bytes) else v
                        for k, v in data.items()
                    }
                    state = RecoveryState.from_dict(decoded)
                    logger.info(
                        f"📖 Recovery state loaded (mission lookup) | "
                        f"mission={mission_id} | session={state.session_id}"
                    )
                    return state
            
            logger.debug(f"No recovery state found for session={session_id}, mission={mission_id}")
            return None
            
        except Exception as e:
            logger.error(f"Failed to load recovery state: {e}", exc_info=True)
            return None
    
    async def clear_recovery_state(
        self,
        session_id: Optional[str] = None,
        mission_id: Optional[str] = None
    ) -> bool:
        """
        Clear recovery state from Redis.
        
        Marks the mission as completed/failed and removes pending pipeline.
        Can optionally delete the keys entirely.
        
        Args:
            session_id: Session identifier (optional)
            mission_id: Mission identifier (optional)
            
        Returns:
            True if clear successful, False otherwise
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False
        
        if not session_id and not mission_id:
            logger.warning("Either session_id or mission_id must be provided")
            return False
        
        try:
            # Clear session key
            if session_id:
                session_key = self._session_key(session_id)
                # Mark as done rather than deleting (for audit trail)
                await self.redis.hset(session_key, mapping={
                    "status": RecoveryStatus.COMPLETED.value,
                    "phase": MissionPhase.DONE.value,
                    "pending_pipeline": "null",
                    "updated_at": str(time.time())
                })
                logger.info(f"🗑️ Recovery state cleared for session={session_id}")
            
            # Clear mission key
            if mission_id:
                mission_key = self._mission_key(mission_id)
                await self.redis.hset(mission_key, mapping={
                    "status": RecoveryStatus.COMPLETED.value,
                    "phase": MissionPhase.DONE.value,
                    "pending_pipeline": "null",
                    "updated_at": str(time.time())
                })
                logger.info(f"🗑️ Recovery state cleared for mission={mission_id}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to clear recovery state: {e}", exc_info=True)
            return False
    
    async def update_page_node(
        self,
        session_id: str,
        page_node: PageNodeRef
    ) -> bool:
        """
        Update the current page node in recovery state.
        
        Called when navigation occurs to track the agent's position in the site map.
        
        Args:
            session_id: Session identifier
            page_node: New page node reference
            
        Returns:
            True if update successful
        """
        try:
            state = await self.load_recovery_state(session_id=session_id)
            if not state:
                logger.warning(f"Cannot update page node: no state for session={session_id}")
                return False
            
            state.page_node = page_node
            state.current_url = page_node.current_url
            return await self.save_recovery_state(state)
            
        except Exception as e:
            logger.error(f"Failed to update page node: {e}", exc_info=True)
            return False
    
    async def increment_step_count(self, session_id: str) -> int:
        """
        Atomically increment the step count.
        
        Args:
            session_id: Session identifier
            
        Returns:
            New step count after increment
        """
        try:
            state = await self.load_recovery_state(session_id=session_id)
            if not state:
                logger.warning(f"Cannot increment step: no state for session={session_id}")
                return 0
            
            state.step_count += 1
            await self.save_recovery_state(state)
            logger.debug(f"📈 Step count incremented: {state.step_count}")
            return state.step_count
            
        except Exception as e:
            logger.error(f"Failed to increment step count: {e}", exc_info=True)
            return 0
    
    async def update_phase(
        self,
        session_id: str,
        phase: MissionPhase,
        subgoal_index: Optional[int] = None
    ) -> bool:
        """
        Update mission phase and optionally subgoal index.
        
        Args:
            session_id: Session identifier
            phase: New mission phase
            subgoal_index: Optional new subgoal index
            
        Returns:
            True if update successful
        """
        try:
            state = await self.load_recovery_state(session_id=session_id)
            if not state:
                logger.warning(f"Cannot update phase: no state for session={session_id}")
                return False
            
            state.phase = phase.value
            if subgoal_index is not None:
                state.subgoal_index = subgoal_index
            
            return await self.save_recovery_state(state)
            
        except Exception as e:
            logger.error(f"Failed to update phase: {e}", exc_info=True)
            return False
    
    async def set_pending_pipeline(
        self,
        session_id: str,
        pipeline: Dict[str, Any]
    ) -> bool:
        """
        Store pending multi-action pipeline for resume.
        
        Args:
            session_id: Session identifier
            pipeline: Pipeline data (actions, last_acknowledged_index, etc.)
            
        Returns:
            True if save successful
        """
        try:
            state = await self.load_recovery_state(session_id=session_id)
            if not state:
                logger.warning(f"Cannot set pipeline: no state for session={session_id}")
                return False
            
            state.pending_pipeline = pipeline
            return await self.save_recovery_state(state)
            
        except Exception as e:
            logger.error(f"Failed to set pending pipeline: {e}", exc_info=True)
            return False
    
    async def get_active_sessions(self) -> List[str]:
        """
        Get list of all active session IDs.
        
        Returns:
            List of session IDs with active recovery state
        """
        if not self.redis:
            return []
        
        try:
            # Scan for session keys
            pattern = f"{self.SESSION_KEY_PREFIX}:*:recovery"
            session_ids = []
            
            async for key in self.redis.scan_iter(match=pattern):
                # Extract session_id from key
                key_str = key.decode() if isinstance(key, bytes) else key
                parts = key_str.split(":")
                if len(parts) >= 3:
                    session_ids.append(parts[1])
            
            logger.debug(f"Found {len(session_ids)} active sessions")
            return session_ids
            
        except Exception as e:
            logger.error(f"Failed to get active sessions: {e}", exc_info=True)
            return []


# Convenience function for creating RecoveryStore
def create_recovery_store(redis_client) -> RecoveryStore:
    """
    Create a RecoveryStore instance.
    
    Args:
        redis_client: Async Redis client
        
    Returns:
        Configured RecoveryStore instance
    """
    return RecoveryStore(redis_client)

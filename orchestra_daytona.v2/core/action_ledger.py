"""
Action Ledger - Action history ledger for TARA Visual Copilot.

This module maintains a capped list of actions executed during a mission session.
Used for loop detection, audit trails, and recovery after reloads.

Redis Key:
- tara:session:{session_id}:actions - Capped list (10 actions max) per session

ActionRecord Structure:
- seq: Sequential action number
- pipeline_id: ID of the pipeline that generated this action
- type: Action type (click|type|scroll|wait|answer)
- target_id: Target element ID
- label: Human-readable label for the action
- status: Action status (planned|sent|executed|confirmed|failed)
- timestamps: Creation and completion timestamps
"""

import json
import logging
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class ActionStatus(str, Enum):
    """Action status enumeration"""
    PLANNED = "planned"
    SENT = "sent"
    EXECUTED = "executed"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class ActionType(str, Enum):
    """Action type enumeration"""
    CLICK = "click"
    TYPE = "type"
    TYPE_TEXT = "type_text"
    SCROLL = "scroll"
    WAIT = "wait"
    ANSWER = "answer"
    NAVIGATE = "navigate"


@dataclass
class ActionRecord:
    """
    Record of a single action executed during a mission.
    
    Attributes:
        seq: Sequential action number (monotonically increasing)
        pipeline_id: ID of the pipeline that generated this action
        type: Action type (click|type|scroll|wait|answer)
        target_id: Target element ID (CSS selector, XPath, or semantic ID)
        label: Human-readable label for the action
        status: Current action status
        url_before: URL before action execution
        url_after: URL after action execution
        timestamp: Action creation timestamp
        completed_at: Action completion timestamp (if applicable)
        error: Error message if action failed
        metadata: Additional action-specific metadata
    """
    seq: int
    pipeline_id: str
    type: str
    target_id: str
    label: str
    status: str = ActionStatus.PLANNED.value
    url_before: str = ""
    url_after: str = ""
    timestamp: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActionRecord":
        """Create from dictionary"""
        return cls(
            seq=data.get("seq", 0),
            pipeline_id=data.get("pipeline_id", ""),
            type=data.get("type", ""),
            target_id=data.get("target_id", ""),
            label=data.get("label", ""),
            status=data.get("status", ActionStatus.PLANNED.value),
            url_before=data.get("url_before", ""),
            url_after=data.get("url_after", ""),
            timestamp=data.get("timestamp", time.time()),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            metadata=data.get("metadata", {})
        )
    
    def to_summary(self) -> str:
        """Generate human-readable summary of the action"""
        return f"{self.type}:{self.target_id} ({self.label})"


class ActionLedger:
    """
    Redis-backed action history ledger.
    
    Maintains a capped list of recent actions (default 10) for each session.
    Used for loop detection, recovery, and audit trails.
    
    Usage:
        ledger = ActionLedger(redis_client)
        await ledger.append_action(session_id, action_record)
        actions = await ledger.get_recent_actions(session_id, limit=5)
    """
    
    # Maximum actions to keep in the ledger (capped list)
    MAX_ACTIONS = 10
    
    # Key prefix
    SESSION_KEY_PREFIX = "tara:session"
    
    # TTL: 24 hours for active sessions
    SESSION_TTL = 24 * 60 * 60  # 86400 seconds
    
    def __init__(self, redis_client):
        """
        Initialize action ledger.
        
        Args:
            redis_client: Async Redis client (aioredis or redis.asyncio)
        """
        self.redis = redis_client
        self.max_actions = self.MAX_ACTIONS
        logger.info(f"ActionLedger initialized (max_actions={self.max_actions})")
    
    def _actions_key(self, session_id: str) -> str:
        """Generate Redis key for session actions"""
        return f"{self.SESSION_KEY_PREFIX}:{session_id}:actions"
    
    async def append_action(
        self,
        session_id: str,
        action: ActionRecord,
        max_actions: Optional[int] = None
    ) -> bool:
        """
        Append an action to the ledger.
        
        Maintains a capped list by removing oldest actions when limit is exceeded.
        Actions are stored as JSON strings in a Redis list.
        
        Args:
            session_id: Session identifier
            action: ActionRecord to append
            max_actions: Optional override for max actions to keep
            
        Returns:
            True if append successful, False otherwise
            
        Example:
            >>> action = ActionRecord(seq=1, pipeline_id="p1", type="click", 
            ...                       target_id="btn-submit", label="Submit")
            >>> await ledger.append_action("session123", action)
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False
        
        try:
            max_keep = max_actions if max_actions is not None else self.max_actions
            
            # Serialize action to JSON
            action_json = json.dumps(action.to_dict())
            
            # Push to Redis list
            actions_key = self._actions_key(session_id)
            await self.redis.rpush(actions_key, action_json)
            
            # Trim to max size (keep only recent actions)
            if max_keep > 0:
                await self.redis.ltrim(actions_key, -max_keep, -1)
            
            # Set TTL
            await self.redis.expire(actions_key, self.SESSION_TTL)
            
            logger.info(
                f"📝 Action appended | session={session_id} | "
                f"seq={action.seq} | type={action.type} | target={action.target_id}"
            )
            return True
            
        except Exception as e:
            logger.error(f"Failed to append action: {e}", exc_info=True)
            return False
    
    async def update_action_status(
        self,
        session_id: str,
        seq: int,
        new_status: ActionStatus,
        url_after: Optional[str] = None,
        error: Optional[str] = None,
        completed_at: Optional[float] = None
    ) -> bool:
        """
        Update the status of an existing action.
        
        Searches the action list for the matching seq number and updates it.
        
        Args:
            session_id: Session identifier
            seq: Sequence number of the action to update
            new_status: New status to set
            url_after: Optional URL after execution
            error: Optional error message
            completed_at: Optional completion timestamp
            
        Returns:
            True if update successful, False if action not found
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False
        
        try:
            actions_key = self._actions_key(session_id)
            
            # Get all actions
            actions_data = await self.redis.lrange(actions_key, 0, -1)
            
            if not actions_data:
                logger.warning(f"No actions found for session={session_id}")
                return False
            
            # Find and update the action
            updated = False
            for i, action_json in enumerate(actions_data):
                action_str = action_json.decode() if isinstance(action_json, bytes) else action_json
                action = ActionRecord.from_dict(json.loads(action_str))
                
                if action.seq == seq:
                    action.status = new_status.value
                    if url_after:
                        action.url_after = url_after
                    if error:
                        action.error = error
                    if completed_at:
                        action.completed_at = completed_at
                    elif new_status in (ActionStatus.EXECUTED, ActionStatus.CONFIRMED, ActionStatus.FAILED):
                        action.completed_at = time.time()
                    
                    # Update in list
                    updated_action_json = json.dumps(action.to_dict())
                    await self.redis.lset(actions_key, i, updated_action_json)
                    updated = True
                    
                    logger.info(
                        f"✏️ Action status updated | session={session_id} | "
                        f"seq={seq} | status={new_status}"
                    )
                    break
            
            if not updated:
                logger.warning(f"Action seq={seq} not found in session={session_id}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to update action status: {e}", exc_info=True)
            return False
    
    async def get_recent_actions(
        self,
        session_id: str,
        limit: int = 10,
        status_filter: Optional[ActionStatus] = None
    ) -> List[ActionRecord]:
        """
        Get recent actions from the ledger.
        
        Args:
            session_id: Session identifier
            limit: Maximum number of actions to return
            status_filter: Optional filter by status
            
        Returns:
            List of ActionRecord objects (most recent last)
            
        Example:
            >>> actions = await ledger.get_recent_actions("session123", limit=5)
            >>> for action in actions:
            ...     print(f"{action.seq}: {action.type} -> {action.status}")
        """
        if not self.redis:
            logger.debug("Redis client not available")
            return []
        
        try:
            actions_key = self._actions_key(session_id)
            
            # Get actions from Redis list
            if status_filter:
                # Get all actions if filtering
                actions_data = await self.redis.lrange(actions_key, 0, -1)
            else:
                # Get only the last N actions
                actions_data = await self.redis.lrange(actions_key, -limit, -1)
            
            if not actions_data:
                return []
            
            # Parse actions
            actions = []
            for action_json in actions_data:
                action_str = action_json.decode() if isinstance(action_json, bytes) else action_json
                action = ActionRecord.from_dict(json.loads(action_str))
                
                # Apply status filter if specified
                if status_filter and action.status != status_filter.value:
                    continue
                
                actions.append(action)
            
            # If no status filter, we already have the last N actions
            # If status filter, we need to limit after filtering
            if status_filter and len(actions) > limit:
                actions = actions[-limit:]
            
            return actions
            
        except Exception as e:
            logger.error(f"Failed to get recent actions: {e}", exc_info=True)
            return []
    
    async def get_action_by_seq(
        self,
        session_id: str,
        seq: int
    ) -> Optional[ActionRecord]:
        """
        Get a specific action by sequence number.
        
        Args:
            session_id: Session identifier
            seq: Sequence number to look up
            
        Returns:
            ActionRecord if found, None otherwise
        """
        if not self.redis:
            return None
        
        try:
            actions_key = self._actions_key(session_id)
            actions_data = await self.redis.lrange(actions_key, 0, -1)
            
            for action_json in actions_data:
                action_str = action_json.decode() if isinstance(action_json, bytes) else action_json
                action = ActionRecord.from_dict(json.loads(action_str))
                
                if action.seq == seq:
                    return action
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get action by seq: {e}", exc_info=True)
            return None
    
    async def get_last_action(
        self,
        session_id: str
    ) -> Optional[ActionRecord]:
        """
        Get the most recent action.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Most recent ActionRecord, or None if no actions
        """
        if not self.redis:
            return None
        
        try:
            actions_key = self._actions_key(session_id)
            action_json = await self.redis.lindex(actions_key, -1)
            
            if not action_json:
                return None
            
            action_str = action_json.decode() if isinstance(action_json, bytes) else action_json
            return ActionRecord.from_dict(json.loads(action_str))
            
        except Exception as e:
            logger.error(f"Failed to get last action: {e}", exc_info=True)
            return None
    
    async def detect_action_loop(
        self,
        session_id: str,
        window_size: int = 5
    ) -> bool:
        """
        Detect if the agent is stuck in an action loop.
        
        Checks if the last N actions are repeating (same type and target).
        
        Args:
            session_id: Session identifier
            window_size: Number of actions to check for repetition
            
        Returns:
            True if loop detected, False otherwise
        """
        try:
            actions = await self.get_recent_actions(session_id, limit=window_size)
            
            if len(actions) < window_size:
                return False
            
            # Check if last N actions are identical (type + target)
            signatures = [(a.type, a.target_id) for a in actions[-window_size:]]
            
            # If all signatures are the same, we have a loop
            if len(set(signatures)) == 1:
                logger.warning(
                    f"🔁 ACTION LOOP DETECTED | session={session_id} | "
                    f"pattern={signatures[0]} repeated {window_size} times"
                )
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Failed to detect action loop: {e}", exc_info=True)
            return False
    
    async def get_action_summary(
        self,
        session_id: str
    ) -> Dict[str, Any]:
        """
        Get summary statistics for actions in a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary with action counts by type and status
        """
        try:
            actions = await self.get_recent_actions(session_id, limit=self.max_actions)
            
            if not actions:
                return {
                    "total": 0,
                    "by_type": {},
                    "by_status": {},
                    "failed_count": 0
                }
            
            # Count by type and status
            by_type: Dict[str, int] = {}
            by_status: Dict[str, int] = {}
            failed_count = 0
            
            for action in actions:
                by_type[action.type] = by_type.get(action.type, 0) + 1
                by_status[action.status] = by_status.get(action.status, 0) + 1
                if action.status == ActionStatus.FAILED.value:
                    failed_count += 1
            
            return {
                "total": len(actions),
                "by_type": by_type,
                "by_status": by_status,
                "failed_count": failed_count
            }
            
        except Exception as e:
            logger.error(f"Failed to get action summary: {e}", exc_info=True)
            return {}
    
    async def clear_actions(self, session_id: str) -> bool:
        """
        Clear all actions for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if clear successful
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False
        
        try:
            actions_key = self._actions_key(session_id)
            await self.redis.delete(actions_key)
            
            logger.info(f"🗑️ Actions cleared for session={session_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to clear actions: {e}", exc_info=True)
            return False


# Convenience function for creating ActionLedger
def create_action_ledger(redis_client, max_actions: int = 10) -> ActionLedger:
    """
    Create an ActionLedger instance.
    
    Args:
        redis_client: Async Redis client
        max_actions: Maximum actions to keep in ledger
        
    Returns:
        Configured ActionLedger instance
    """
    ledger = ActionLedger(redis_client)
    ledger.max_actions = max_actions
    return ledger

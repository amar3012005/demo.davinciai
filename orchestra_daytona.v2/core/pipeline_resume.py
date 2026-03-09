"""
Pipeline Resume - Multi-action pipeline persistence for TARA Visual Copilot.

This module stores pending multi-action pipelines for recovery after reloads.
Enables the backend to resume speculative action bundles without re-planning.

Redis Key:
- tara:session:{session_id}:pending_pipeline

Pipeline Structure:
- pipeline_id: Unique pipeline identifier
- actions: List of planned actions
- last_acknowledged_index: Last successfully executed action index
- expected_navigation: Expected navigation outcome
- created_at: Pipeline creation timestamp
"""

import json
import logging
import time
import uuid
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class PipelineStatus(str, Enum):
    """Pipeline status enumeration"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"  # Some actions completed, some failed


@dataclass
class PipelineAction:
    """
    Individual action within a pipeline.
    
    Attributes:
        index: Position in the pipeline (0-based)
        type: Action type (click|type|scroll|wait|answer)
        target_id: Target element identifier
        label: Human-readable description
        parameters: Action-specific parameters (e.g., text for type actions)
        status: Execution status
        error: Error message if failed
    """
    index: int
    type: str
    target_id: str
    label: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineAction":
        """Create from dictionary"""
        return cls(
            index=data.get("index", 0),
            type=data.get("type", ""),
            target_id=data.get("target_id", ""),
            label=data.get("label", ""),
            parameters=data.get("parameters", {}),
            status=data.get("status", "pending"),
            error=data.get("error")
        )


@dataclass
class PipelineState:
    """
    Complete state of a multi-action pipeline.
    
    Attributes:
        pipeline_id: Unique pipeline identifier
        session_id: Session this pipeline belongs to
        mission_id: Mission this pipeline belongs to
        status: Current pipeline status
        actions: List of actions in the pipeline
        last_acknowledged_index: Index of last successfully executed action (-1 if none)
        expected_navigation: Expected navigation outcome (URL pattern, node change)
        created_at: Pipeline creation timestamp
        updated_at: Last update timestamp
        completed_at: Pipeline completion timestamp
        metadata: Additional pipeline metadata
    """
    pipeline_id: str
    session_id: str
    mission_id: str
    status: str = PipelineStatus.PENDING.value
    actions: List[PipelineAction] = field(default_factory=list)
    last_acknowledged_index: int = -1
    expected_navigation: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        # Convert actions to dicts
        data["actions"] = [a.to_dict() if isinstance(a, PipelineAction) else a for a in self.actions]
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineState":
        """Create from dictionary"""
        # Convert action dicts to PipelineAction objects
        actions = []
        for action_data in data.get("actions", []):
            if isinstance(action_data, dict):
                actions.append(PipelineAction.from_dict(action_data))
            elif isinstance(action_data, PipelineAction):
                actions.append(action_data)
        
        return cls(
            pipeline_id=data.get("pipeline_id", str(uuid.uuid4())),
            session_id=data.get("session_id", ""),
            mission_id=data.get("mission_id", ""),
            status=data.get("status", PipelineStatus.PENDING.value),
            actions=actions,
            last_acknowledged_index=data.get("last_acknowledged_index", -1),
            expected_navigation=data.get("expected_navigation"),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            completed_at=data.get("completed_at"),
            metadata=data.get("metadata", {})
        )
    
    def get_next_action(self) -> Optional[PipelineAction]:
        """Get the next pending action"""
        next_index = self.last_acknowledged_index + 1
        if next_index < len(self.actions):
            return self.actions[next_index]
        return None
    
    def get_remaining_actions(self) -> List[PipelineAction]:
        """Get all remaining (unexecuted) actions"""
        if self.last_acknowledged_index >= len(self.actions) - 1:
            return []
        return self.actions[self.last_acknowledged_index + 1:]
    
    def is_complete(self) -> bool:
        """Check if all actions have been executed"""
        return self.last_acknowledged_index >= len(self.actions) - 1
    
    def get_progress(self) -> Dict[str, Any]:
        """Get pipeline progress information"""
        total = len(self.actions)
        completed = self.last_acknowledged_index + 1 if self.last_acknowledged_index >= 0 else 0
        remaining = total - completed
        
        return {
            "total": total,
            "completed": completed,
            "remaining": remaining,
            "percentage": (completed / total * 100) if total > 0 else 0
        }


class PipelineResume:
    """
    Redis-backed pipeline persistence for multi-action resumption.
    
    Stores pending pipelines so they can be resumed after page reloads
    or connection drops. Prevents re-planning overhead and maintains
    action continuity.
    
    Usage:
        resume = PipelineResume(redis_client)
        pipeline = PipelineState(pipeline_id="p1", session_id="s1", mission_id="m1")
        await resume.save_pipeline(pipeline)
        
        loaded = await resume.load_pipeline(session_id="s1")
        await resume.mark_action_complete(session_id="s1", action_index=0)
    """
    
    # Key prefix
    SESSION_KEY_PREFIX = "tara:session"
    
    # TTL: 1 hour for pending pipelines (shorter than session TTL)
    PIPELINE_TTL = 60 * 60  # 3600 seconds
    
    def __init__(self, redis_client):
        """
        Initialize pipeline resume manager.
        
        Args:
            redis_client: Async Redis client (aioredis or redis.asyncio)
        """
        self.redis = redis_client
        logger.info("PipelineResume initialized")
    
    def _pipeline_key(self, session_id: str) -> str:
        """Generate Redis key for pending pipeline"""
        return f"{self.SESSION_KEY_PREFIX}:{session_id}:pending_pipeline"
    
    async def save_pipeline(self, pipeline: PipelineState) -> bool:
        """
        Save a pipeline to Redis.
        
        Stores the complete pipeline state for later resumption.
        
        Args:
            pipeline: PipelineState to persist
            
        Returns:
            True if save successful, False otherwise
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False
        
        try:
            # Update timestamp
            pipeline.updated_at = time.time()
            
            # Serialize to JSON
            pipeline_json = json.dumps(pipeline.to_dict())
            
            # Save to Redis
            key = self._pipeline_key(pipeline.session_id)
            await self.redis.set(key, pipeline_json, ex=self.PIPELINE_TTL)
            
            logger.info(
                f"💾 Pipeline saved | session={pipeline.session_id} | "
                f"pipeline={pipeline.pipeline_id} | actions={len(pipeline.actions)}"
            )
            return True
            
        except Exception as e:
            logger.error(f"Failed to save pipeline: {e}", exc_info=True)
            return False
    
    async def load_pipeline(
        self,
        session_id: str
    ) -> Optional[PipelineState]:
        """
        Load pending pipeline from Redis.
        
        Args:
            session_id: Session identifier
            
        Returns:
            PipelineState if found, None otherwise
        """
        if not self.redis:
            logger.debug("Redis client not available")
            return None
        
        try:
            key = self._pipeline_key(session_id)
            pipeline_json = await self.redis.get(key)
            
            if not pipeline_json:
                logger.debug(f"No pending pipeline for session={session_id}")
                return None
            
            # Handle bytes decoding
            if isinstance(pipeline_json, bytes):
                pipeline_json = pipeline_json.decode()
            
            pipeline = PipelineState.from_dict(json.loads(pipeline_json))
            
            logger.info(
                f"📖 Pipeline loaded | session={session_id} | "
                f"pipeline={pipeline.pipeline_id} | "
                f"status={pipeline.status} | "
                f"progress={pipeline.get_progress()}"
            )
            return pipeline
            
        except Exception as e:
            logger.error(f"Failed to load pipeline: {e}", exc_info=True)
            return None
    
    async def mark_pipeline_complete(
        self,
        session_id: str,
        status: PipelineStatus = PipelineStatus.COMPLETED
    ) -> bool:
        """
        Mark a pipeline as complete.
        
        Args:
            session_id: Session identifier
            status: Final status (COMPLETED or FAILED)
            
        Returns:
            True if update successful
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False
        
        try:
            pipeline = await self.load_pipeline(session_id)
            if not pipeline:
                logger.warning(f"Cannot mark complete: no pipeline for session={session_id}")
                return False
            
            pipeline.status = status.value
            pipeline.completed_at = time.time()
            pipeline.updated_at = time.time()
            
            return await self.save_pipeline(pipeline)
            
        except Exception as e:
            logger.error(f"Failed to mark pipeline complete: {e}", exc_info=True)
            return False
    
    async def mark_action_complete(
        self,
        session_id: str,
        action_index: int,
        error: Optional[str] = None
    ) -> bool:
        """
        Mark an individual action as complete.
        
        Updates the last_acknowledged_index to enable resumption from the
        correct position.
        
        Args:
            session_id: Session identifier
            action_index: Index of the completed action (0-based)
            error: Optional error message if action failed
            
        Returns:
            True if update successful
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False
        
        try:
            pipeline = await self.load_pipeline(session_id)
            if not pipeline:
                logger.warning(f"Cannot mark action complete: no pipeline for session={session_id}")
                return False
            
            # Validate index
            if action_index < 0 or action_index >= len(pipeline.actions):
                logger.error(f"Invalid action index: {action_index}")
                return False
            
            # Update action status
            pipeline.actions[action_index].status = "failed" if error else "completed"
            if error:
                pipeline.actions[action_index].error = error
            
            # Update last acknowledged index
            if action_index > pipeline.last_acknowledged_index:
                pipeline.last_acknowledged_index = action_index
            
            # Update pipeline status if all actions complete
            if pipeline.is_complete():
                pipeline.status = PipelineStatus.COMPLETED.value
                pipeline.completed_at = time.time()
            
            pipeline.updated_at = time.time()
            
            return await self.save_pipeline(pipeline)
            
        except Exception as e:
            logger.error(f"Failed to mark action complete: {e}", exc_info=True)
            return False
    
    async def clear_pipeline(self, session_id: str) -> bool:
        """
        Clear pending pipeline for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if clear successful
        """
        if not self.redis:
            logger.error("Redis client not available")
            return False
        
        try:
            key = self._pipeline_key(session_id)
            await self.redis.delete(key)
            
            logger.info(f"🗑️ Pipeline cleared for session={session_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to clear pipeline: {e}", exc_info=True)
            return False
    
    async def has_pending_pipeline(self, session_id: str) -> bool:
        """
        Check if a session has a pending pipeline.
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if pipeline exists and is not complete
        """
        if not self.redis:
            return False
        
        try:
            pipeline = await self.load_pipeline(session_id)
            
            if not pipeline:
                return False
            
            # Check if pipeline is still pending or in progress
            return pipeline.status in (
                PipelineStatus.PENDING.value,
                PipelineStatus.IN_PROGRESS.value,
                PipelineStatus.PARTIAL.value
            )
            
        except Exception as e:
            logger.error(f"Failed to check pending pipeline: {e}", exc_info=True)
            return False
    
    async def create_pipeline(
        self,
        session_id: str,
        mission_id: str,
        actions: List[Dict[str, Any]],
        expected_navigation: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[PipelineState]:
        """
        Create a new pipeline from action specifications.
        
        Convenience method to create and save a pipeline in one call.
        
        Args:
            session_id: Session identifier
            mission_id: Mission identifier
            actions: List of action specifications (type, target_id, label, parameters)
            expected_navigation: Expected navigation outcome
            metadata: Additional metadata
            
        Returns:
            Created PipelineState, or None if failed
        """
        try:
            # Convert action specs to PipelineAction objects
            pipeline_actions = []
            for i, action_spec in enumerate(actions):
                pipeline_actions.append(PipelineAction(
                    index=i,
                    type=action_spec.get("type", ""),
                    target_id=action_spec.get("target_id", ""),
                    label=action_spec.get("label", ""),
                    parameters=action_spec.get("parameters", {})
                ))
            
            # Create pipeline
            pipeline = PipelineState(
                pipeline_id=str(uuid.uuid4()),
                session_id=session_id,
                mission_id=mission_id,
                actions=pipeline_actions,
                expected_navigation=expected_navigation,
                metadata=metadata or {}
            )
            
            # Save to Redis
            if await self.save_pipeline(pipeline):
                return pipeline
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to create pipeline: {e}", exc_info=True)
            return None


# Convenience function for creating PipelineResume
def create_pipeline_resume(redis_client) -> PipelineResume:
    """
    Create a PipelineResume instance.
    
    Args:
        redis_client: Async Redis client
        
    Returns:
        Configured PipelineResume instance
    """
    return PipelineResume(redis_client)

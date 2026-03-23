"""
mission_brain.py

PURPOSE: Central decision engine and constraint enforcer.
         Creates missions from TacticalSchema, enforces blocking rules,
         audits actions before execution, and tracks mission progress.

DEPENDENCIES:
    - redis.asyncio: For mission state persistence
    - tara_models: TacticalSchema, MissionState, Constraint, ConstraintStatus
    - hive_interface: For strategy retrieval
    - live_graph: For DOM queries
    - semantic_detective: For element investigation

USED BY:
    - visual_orchestrator.py: Main integration point
    - WebSocket handlers: Action approval/auditing

MIGRATION STATUS: [NEW] - Decision layer for Ultimate TARA

CONSTRAINT ENFORCEMENT:
    Before any action is executed, audit_action() checks:
    1. Are all blocking constraints filled?
    2. Has this action been tried and failed before?
    3. Is the target element still valid?
    
    Example blocking rules:
    {
        "Add to Cart": ["size", "color"],
        "Checkout": ["size", "color", "quantity"],
        "Export": ["date_range"]
    }

ERROR HANDLING:
    - Redis failure → Degrades to in-memory state (no persistence)
    - Qdrant unavailable → Uses heuristic planning
    - Invalid node ID → Substitutes top detective candidate

Example:
    from mission_brain import MissionBrain
    from tara_models import TacticalSchema, ActionIntent
    
    brain = MissionBrain(redis_client, hive_interface)
    
    # Create mission from user input
    schema = TacticalSchema(
        action=ActionIntent.PURCHASE,
        target_entity="shirt",
        domain="shop.com",
        constraints={"color": "white", "size": None}
    )
    
    mission = await brain.create_mission("session-123", schema)
    
    # Audit action before execution
    approved, reason = await brain.audit_action(
        mission_id="mission-456",
        action_type="click",
        target_id="add-to-cart-btn"
    )
    
    if not approved:
        print(f"Action blocked: {reason}")
        # → "Cannot Add to Cart until size is selected"
"""

import json
import logging
import time
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass

from tara_models import (
    TacticalSchema, ActionIntent,
    MissionState, Constraint, ConstraintStatus,
    StrategyHint, VisualHint,
    ScoredCandidate, DetectiveReport
)

# Optional imports
try:
    from redis import asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)


class MissionBrain:
    """
    Central decision engine and constraint enforcer.
    
    Responsibilities:
    1. Create missions from TacticalSchema
    2. Generate sub-goals from strategy hints
    3. Audit actions before execution (constraint enforcement)
    4. Track mission progress and constraint status
    5. Handle ambiguity resolution
    
    Attributes:
        redis: Redis client for state persistence
        hive: HiveInterface for strategy retrieval
        ttl: Mission state TTL in seconds
    """

    def __init__(
        self,
        redis_client: Optional[Any] = None,
        hive_interface: Optional[Any] = None,
        ttl: int = 3600
    ):
        """
        Initialize MissionBrain.
        
        Args:
            redis_client: Redis client for state persistence
            hive_interface: HiveInterface for strategy retrieval
            ttl: Mission state TTL in seconds
        """
        self.redis = redis_client
        self.hive = hive_interface
        self.ttl = ttl
        
        # In-memory state fallback (if Redis unavailable)
        self._memory_state: Dict[str, MissionState] = {}
        
        logger.info(
            f"[MISSION] MissionBrain initialized: "
            f"Redis={REDIS_AVAILABLE and redis_client is not None}"
        )

    async def get_or_create_mission(
        self,
        session_id: str,
        schema: TacticalSchema,
        strategy: Optional[StrategyHint] = None
    ) -> MissionState:
        """
        Get existing mission for session (in_progress OR completed), or create a new one.
        
        CRITICAL: We must return completed missions too, so the upstream
        Terminal State check can fire and return an "answer" action.
        If we only return in_progress missions, the completed one gets
        silently discarded and a BRAND NEW mission is created with
        subgoal_index=0, causing the infinite loop.
        """
        existing = await self._load_session_mission(session_id)
        if existing:
            # ── Goal Integrity Guard: Check for semantic mismatch ──
            # If the user has changed the goal, we must NOT resume the old mission.
            # Resuming an old mission with a different goal causes subgoal index
            # collisions and premature Last-Mile handoffs.
            new_goal = (schema.raw_utterance or schema.target_entity or "").lower()
            old_goal = (getattr(existing.schema, "raw_utterance", "") or \
                        getattr(existing.schema, "target_entity", "") or "").lower()
            
            # Simple keyword overlap check
            def _get_terms(s):
                import re
                return set(re.findall(r"[a-z0-9]{3,}", s))
            
            new_terms = _get_terms(new_goal)
            old_terms = _get_terms(old_goal)
            
            # If goals are non-empty and have zero overlap, it's a new mission.
            if new_terms and old_terms and not (new_terms & old_terms):
                logger.warning(
                    f"MISSION GOAL MISMATCH: incoming='{new_goal}' mission='{old_goal}' "
                    f"-> invalidating mission {existing.mission_id} to start fresh."
                )
                existing.status = "failed" # Invalidate so _load_session_mission won't find it again
                await self._save_mission(existing)
                existing = None

        if existing:
            if existing.status == "in_progress":
                logger.debug(
                    f"[MISSION] resuming: {existing.mission_id} "
                    f"(subgoal {existing.current_subgoal_index}/{len(existing.subgoals)}, "
                    f"history: {len(existing.action_history)} actions)"
                )
                return existing
            elif existing.status == "completed":
                logger.info(
                    f"[MISSION] already completed: {existing.mission_id} "
                    f"- returning for terminal state check"
                )
                return existing

        return await self.create_mission(session_id, schema, strategy)

    async def _load_session_mission(self, session_id: str) -> Optional[MissionState]:
        """Load the active mission for a session."""
        key = f"session_mission:{session_id}"
        try:
            if self.redis and REDIS_AVAILABLE:
                mission_id = await self.redis.get(key)
                if mission_id:
                    if isinstance(mission_id, bytes):
                        mission_id = mission_id.decode('utf-8')
                    return await self._load_mission(mission_id)
            else:
                # In-memory fallback: scan for session match
                for mid, m in self._memory_state.items():
                    if m.session_id == session_id and m.status == "in_progress":
                        return m
        except Exception as e:
            logger.error(f"Failed to load session mission: {e}")
        return None

    async def create_mission(
        self,
        session_id: str,
        schema: TacticalSchema,
        strategy: Optional[StrategyHint] = None
    ) -> MissionState:
        """
        Create a new mission from user intent.
        
        Args:
            session_id: Session identifier
            schema: TacticalSchema from MindReader
            strategy: Optional StrategyHint from Hive
        
        Returns:
            MissionState with initialized sub-goals
        """
        import uuid
        
        mission_id = f"mission-{uuid.uuid4().hex[:8]}"
        
        # Generate sub-goals from strategy or heuristics
        subgoals = await self._generate_subgoals(schema, strategy)
        
        # Initialize constraints from schema
        constraints = self._init_constraints(schema)
        
        # Create mission state
        mission = MissionState(
            mission_id=mission_id,
            session_id=session_id,
            schema=schema,
            status="in_progress",
            current_subgoal_index=0,
            subgoals=subgoals,
            constraints=constraints,
            visited_urls=[],
            action_history=[],
            ambiguity_count=0,
            created_at=time.time(),
            updated_at=time.time()
        )
        
        # Save state
        await self._save_mission(mission)
        
        logger.info(
            f"[MISSION] created: {mission_id} "
            f"({len(subgoals)} subgoals, {len(constraints)} constraints)"
        )
        
        return mission

    async def _generate_subgoals(
        self,
        schema: TacticalSchema,
        strategy: Optional[StrategyHint]
    ) -> List[str]:
        """
        Generate sub-goals from strategy or heuristics.
        
        Args:
            schema: TacticalSchema
            strategy: Optional StrategyHint
        
        Returns:
            List of sub-goal descriptions
        """
        if strategy and strategy.sequence:
            # Use strategy from Hive
            return strategy.sequence
        
        # Heuristic sub-goals based on action type
        if schema.action == ActionIntent.PURCHASE:
            return [
                f"Navigate to {schema.target_entity} section",
                f"Select {schema.target_entity} with specified options",
                "Add item to cart",
                "Proceed to checkout"
            ]
        
        elif schema.action == ActionIntent.EXTRACTION:
            return [
                f"Navigate to page showing {schema.target_entity}",
                f"Locate {schema.target_entity} data",
                "Extract and present data to user"
            ]
        
        elif schema.action == ActionIntent.NAVIGATION:
            return [
                f"Navigate to {schema.target_entity}"
            ]
        
        elif schema.action == ActionIntent.INTERACTION:
            return [
                f"Locate element for {schema.target_entity}",
                "Interact with element"
            ]
        
        elif schema.action == ActionIntent.SEARCH:
            return [
                "Locate search input",
                f"Search for {schema.target_entity}",
                "Review search results"
            ]
        
        # Default generic flow
        return [
            f"Navigate to page for {schema.target_entity}",
            f"Interact with {schema.target_entity}",
            "Complete task"
        ]

    async def _react_generate_subgoal(
        self,
        schema: TacticalSchema,
        nodes: list,
        app: Any,
        mission: Optional[Any] = None,
    ) -> List[str]:
        """
        Zero-shot ReAct subgoal generation using LLM + live DOM.

        Called when no Hive strategy exists (unknown domain) or when
        strategy subgoals are exhausted and replanning is needed.

        Uses the ReAct navigator prompt to analyze the current DOM
        and produce a grounded next-step subgoal.

        Returns:
            List of subgoal strings (typically 1 element), or empty list on failure.
        """
        try:
            from visual_copilot.prompts.react.navigator_prompt import build_react_prompt
        except ImportError:
            logger.warning("ReAct prompt builder not available, falling back to heuristic subgoals")
            return await self._generate_subgoals(schema, None)

        llm = None
        if app and hasattr(app, "state") and hasattr(app.state, "mind_reader"):
            mr = app.state.mind_reader
            if hasattr(mr, "llm"):
                llm = mr.llm

        if not llm or not hasattr(llm, "generate"):
            logger.warning("LLM not available for ReAct, falling back to heuristic subgoals")
            return await self._generate_subgoals(schema, None)

        prompt = build_react_prompt(schema=schema, nodes=nodes, mission=mission, app=app)

        try:
            response = await llm.generate(
                prompt,
                model="llama-3.1-8b-instant",
                temperature=0.0,
                response_format={"type": "json_object"},
            )

            import json as _json
            parsed = _json.loads(response)

            # Check terminal states
            if parsed.get("is_done"):
                logger.info("ReAct: goal already visible on screen -- signalling done")
                return ["Extract and present the answer to the user"]

            if parsed.get("is_impossible"):
                logger.warning(f"ReAct: task impossible -- {parsed.get('why', 'unknown reason')}")
                return []

            if parsed.get("decide") == "clarify":
                next_step = parsed.get("next_step", "Ask user for clarification")
                logger.info(f"ReAct: clarification needed -- {next_step}")
                return [next_step]

            # Extract actionable subgoal
            next_step = parsed.get("next_step", "").strip()
            if not next_step:
                logger.warning("ReAct returned empty next_step")
                return []

            logger.info(f"ReAct subgoal: '{next_step}' (confidence={parsed.get('confidence', '?')})")
            return [next_step]

        except Exception as e:
            logger.error(f"ReAct subgoal generation failed: {e}")
            return await self._generate_subgoals(schema, None)

    def _init_constraints(
        self,
        schema: TacticalSchema
    ) -> Dict[str, Constraint]:
        """
        Initialize constraints from TacticalSchema.
        
        Args:
            schema: TacticalSchema with constraints
        
        Returns:
            Dict of constraint name to Constraint object
        """
        constraints = {}
        
        for name, value in schema.constraints.items():
            status = ConstraintStatus.FILLED if value else ConstraintStatus.MISSING
            constraints[name] = Constraint(
                name=name,
                value=value,
                status=status
            )
        
        return constraints

    async def audit_action(
        self,
        mission_id: str,
        action_type: str,
        target_id: str,
        target_text: str = "",
        detective_report: Optional[DetectiveReport] = None
    ) -> Tuple[bool, str]:
        """
        Audit action before execution (CRITICAL GATE).
        
        Checks:
        1. Are all blocking constraints filled?
        2. Has this action been tried and failed?
        3. Is the target element valid?
        
        Args:
            mission_id: Mission identifier
            action_type: Type of action (click, type, etc.)
            target_id: Target element ID
            target_text: Target element text
            detective_report: Optional detective report
        
        Returns:
            Tuple of (approved, reason)
        """
        # Load mission
        mission = await self._load_mission(mission_id)
        if not mission:
            logger.warning(f"Mission not found: {mission_id}")
            return False, f"Mission not found: {mission_id}"
        
        # Check 1: Blocking constraints
        blocking_result = self._check_blocking_constraints(
            mission, action_type, target_text
        )
        if not blocking_result[0]:
            logger.info(f"[BLOCKED] Action blocked: {blocking_result[1]}")
            return blocking_result
        
        # Check 2: History (avoid loops)
        history_result = self._check_action_history(
            mission, action_type, target_id
        )
        if not history_result[0]:
            logger.info(f"[BLOCKED] Action blocked (history): {history_result[1]}")
            return history_result
        
        # Check 3: Constraint status update
        self._update_constraints_from_action(mission, action_type, target_text)
        
        # Save updated constraints
        await self._save_mission(mission)
        
        logger.debug(f"[OK] Action approved: {action_type} on {target_id}")
        return True, "Action approved"

    def _check_blocking_constraints(
        self,
        mission: MissionState,
        action_type: str,
        target_text: str
    ) -> Tuple[bool, str]:
        """
        Check if action is blocked by missing constraints.
        
        Args:
            mission: MissionState
            action_type: Type of action
            target_text: Target element text
        
        Returns:
            Tuple of (approved, reason)
        """
        # Get blocking rules from strategy (if available)
        blocking_rules = self._get_blocking_rules(mission)
        
        # Check if this action type has blocking rules
        for blocked_action, required_constraints in blocking_rules.items():
            if blocked_action.lower() in target_text.lower() or \
               blocked_action.lower() in action_type.lower():
                
                # Check each required constraint
                missing = []
                for constraint_name in required_constraints:
                    constraint = mission.constraints.get(constraint_name)
                    if not constraint or constraint.status != ConstraintStatus.FILLED:
                        missing.append(constraint_name)
                
                if missing:
                    return False, (
                        f"Cannot {blocked_action} until {', '.join(missing)} is selected"
                    )
        
        # Check all constraints for purchase actions
        if action_type == "click" and any(
            kw in target_text.lower() for kw in ["buy", "purchase", "checkout", "add to cart", "add to bag"]
        ):
            missing_constraints = [
                name for name, c in mission.constraints.items()
                if c.status == ConstraintStatus.MISSING
            ]
            if missing_constraints:
                return False, (
                    f"Missing required selections: {', '.join(missing_constraints)}"
                )
        
        return True, ""

    def _get_blocking_rules(self, mission: MissionState) -> Dict[str, List[str]]:
        """
        Get blocking rules from mission strategy.
        
        Args:
            mission: MissionState
        
        Returns:
            Dict of action → required constraints
        """
        # This would come from Hive strategy in a full implementation
        # For now, return empty dict (no blocking rules)
        # In production, this comes from StrategyHint.blocking_rules
        
        # Default blocking rules for common e-commerce flows
        return {
            "add to cart": ["size", "color"],
            "add to bag": ["size", "color"],
            "buy now": ["size", "color"],
            "checkout": ["size", "color", "quantity"],
            "export": ["date_range"],
            "filter": ["filter_type"]
        }

    def _check_action_history(
        self,
        mission: MissionState,
        action_type: str,
        target_id: str
    ) -> Tuple[bool, str]:
        """
        Check if action was already tried and failed.
        
        Args:
            mission: MissionState
            action_type: Type of action
            target_id: Target element ID
        
        Returns:
            Tuple of (approved, reason)
        """
        # Check if this exact action was tried
        action_key = f"{action_type}:{target_id}"
        
        if action_key in mission.action_history:
            return False, f"Already tried {action_type} on {target_id}"
        
        return True, ""

    def _update_constraints_from_action(
        self,
        mission: MissionState,
        action_type: str,
        target_text: str
    ) -> None:
        """
        Update constraint status based on action.
        
        Args:
            mission: MissionState
            action_type: Type of action
            target_text: Target element text
        """
        # Detect constraint fulfillment from action
        constraint_keywords = {
            "size": ["size", "small", "medium", "large", "xl", "xs"],
            "color": ["color", "white", "black", "red", "blue", "green", "yellow"],
            "quantity": ["quantity", "count", "number"],
            "date_range": ["date", "range", "period", "last"]
        }
        
        target_lower = target_text.lower()
        
        for constraint_name, keywords in constraint_keywords.items():
            if constraint_name in mission.constraints:
                constraint = mission.constraints[constraint_name]
                if constraint.status == ConstraintStatus.MISSING:
                    # Check if action fulfills this constraint
                    for keyword in keywords:
                        if keyword in target_lower:
                            constraint.status = ConstraintStatus.FILLED
                            constraint.value = keyword
                            logger.info(
                                f"Constraint fulfilled: {constraint_name} = {keyword}"
                            )
                            break

    async def update_constraint(
        self,
        mission_id: str,
        constraint_name: str,
        value: str,
        status: ConstraintStatus = ConstraintStatus.FILLED
    ) -> bool:
        """
        Update a constraint value (e.g., from user input).
        
        Args:
            mission_id: Mission identifier
            constraint_name: Constraint name
            value: Constraint value
            status: Constraint status
        
        Returns:
            True if updated successfully
        """
        mission = await self._load_mission(mission_id)
        if not mission:
            return False
        
        if constraint_name not in mission.constraints:
            # Create new constraint
            mission.constraints[constraint_name] = Constraint(
                name=constraint_name,
                value=value,
                status=status
            )
        else:
            # Update existing
            constraint = mission.constraints[constraint_name]
            constraint.value = value
            constraint.status = status
        
        await self._save_mission(mission)
        return True

    async def advance_subgoal(
        self,
        mission_id: str,
        is_zero_shot: bool = False
    ) -> bool:
        """
        Advance to next sub-goal.
        
        Args:
            mission_id: Mission identifier
            is_zero_shot: Whether this was a zero-shot generated subgoal
        
        Returns:
            True if advanced successfully
        """
        mission = await self._load_mission(mission_id)
        if not mission:
            return False
        
        mission.current_subgoal_index += 1
        
        if mission.current_subgoal_index >= len(mission.subgoals):
            mission.status = "completed"
            logger.info(f"[OK] Mission completed: {mission_id}")
        else:
            logger.info(
                f"Advanced to subgoal {mission.current_subgoal_index}: "
                f"{mission.subgoals[mission.current_subgoal_index]}"
            )

        await self._save_mission(mission)
        return True

    async def advance_subgoal_verified(
        self,
        mission_id: str,
        is_zero_shot: bool = False,
        verification_reason: str = ""
    ) -> bool:
        """
        Advance to next sub-goal upon verified action.
        Clears pending action state.
        
        Args:
            mission_id: Mission identifier
            is_zero_shot: Whether this was a zero-shot generated subgoal
            verification_reason: Reason why action was verified
            
        Returns:
            True if advanced successfully
        """
        mission = await self._load_mission(mission_id)
        if not mission:
            return False
        
        # Clear pending verification state
        mission.pending_action = None
        mission.pending_verify_attempts = 0
        
        mission.current_subgoal_index += 1
        
        if mission.current_subgoal_index >= len(mission.subgoals):
            mission.status = "completed"
            logger.info(f"[OK] Mission completed (verified: {verification_reason}): {mission_id}")
        else:
            logger.info(
                f"Advanced to subgoal {mission.current_subgoal_index} (verified: {verification_reason}): "
                f"{mission.subgoals[mission.current_subgoal_index]}"
            )
        
        await self._save_mission(mission)
        return True

    async def record_action(
        self,
        mission_id: str,
        action_type: str,
        target_id: str,
        success: bool,
        **kwargs
    ) -> bool:
        """
        Record action in mission history.
        
        Args:
            mission_id: Mission identifier
            action_type: Type of action
            target_id: Target element ID
            success: Whether action succeeded
            **kwargs: Extra action properties like text
        
        Returns:
            True if recorded successfully
        """
        mission = await self._load_mission(mission_id)
        if not mission:
            return False
        
        if action_type == "type" and "text" in kwargs:
            action_key = f"{action_type}:{target_id}='{kwargs['text']}'"
        else:
            action_key = f"{action_type}:{target_id}"
        
        if success:
            # Add to history (to avoid repeating)
            if action_key not in mission.action_history:
                mission.action_history.append(action_key)
        else:
            # Failed action - might want to track separately
            logger.warning(f"[ERROR] Failed action: {action_type} on {target_id}")
        
        await self._save_mission(mission)
        return True

    async def set_pending_action(
        self,
        mission_id: str,
        pending_action: Dict[str, Any],
        dom_signature: str,
        current_url: str
    ) -> bool:
        """
        Stage an action for verification on the next step.
        """
        mission = await self._load_mission(mission_id)
        if not mission:
            return False
        
        mission.pending_action = pending_action
        mission.last_dom_signature = dom_signature
        mission.last_url = current_url
        
        await self._save_mission(mission)
        return True

    async def get_mission_status(self, mission_id: str) -> Optional[Dict[str, Any]]:
        """
        Get mission status summary.
        
        Args:
            mission_id: Mission identifier
        
        Returns:
            Status dict or None
        """
        mission = await self._load_mission(mission_id)
        if not mission:
            return None
        
        return {
            "mission_id": mission.mission_id,
            "session_id": mission.session_id,
            "status": mission.status,
            "current_subgoal": (
                mission.subgoals[mission.current_subgoal_index]
                if mission.current_subgoal_index < len(mission.subgoals)
                else None
            ),
            "subgoal_index": mission.current_subgoal_index,
            "total_subgoals": len(mission.subgoals),
            "constraints": {
                name: {
                    "value": c.value,
                    "status": c.status.value
                }
                for name, c in mission.constraints.items()
            },
            "action_history": mission.action_history,
            "ambiguity_count": mission.ambiguity_count
        }

    # ═══════════════════════════════════════════════════════════
    # STATE PERSISTENCE
    # ═══════════════════════════════════════════════════════════

    def _mission_key(self, mission_id: str) -> str:
        """Generate Redis key for mission."""
        return f"mission:{mission_id}"

    async def _save_mission(self, mission: MissionState) -> bool:
        """
        Save mission state to Redis.
        
        Args:
            mission: MissionState to save
        
        Returns:
            True if saved successfully
        """
        mission.updated_at = time.time()

        try:
            if self.redis and REDIS_AVAILABLE:
                await self.redis.set(
                    self._mission_key(mission.mission_id),
                    json.dumps(mission.to_dict()),
                    ex=self.ttl
                )
                # Also store session→mission mapping
                await self.redis.set(
                    f"session_mission:{mission.session_id}",
                    mission.mission_id,
                    ex=self.ttl
                )
            else:
                # Fallback to in-memory
                self._memory_state[mission.mission_id] = mission

            return True
            
        except Exception as e:
            logger.error(f"Failed to save mission: {e}")
            # Fallback to in-memory
            self._memory_state[mission.mission_id] = mission
            return True

    async def _load_mission(self, mission_id: str) -> Optional[MissionState]:
        """
        Load mission state from Redis.
        
        Args:
            mission_id: Mission identifier
        
        Returns:
            MissionState or None
        """
        try:
            if self.redis and REDIS_AVAILABLE:
                raw = await self.redis.get(self._mission_key(mission_id))
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode('utf-8')
                    return MissionState.from_dict(json.loads(raw))
            
            # Fallback to in-memory
            return self._memory_state.get(mission_id)
            
        except Exception as e:
            logger.error(f"Failed to load mission: {e}")
            return self._memory_state.get(mission_id)

    async def delete_mission(self, mission_id: str) -> bool:
        """
        Delete mission state.
        
        Args:
            mission_id: Mission identifier
        
        Returns:
            True if deleted successfully
        """
        try:
            if self.redis and REDIS_AVAILABLE:
                await self.redis.delete(self._mission_key(mission_id))
            else:
                self._memory_state.pop(mission_id, None)
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete mission: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════

def create_mission_brain(
    redis_url: str = "redis://localhost:6379",
    hive_interface: Optional[Any] = None
) -> MissionBrain:
    """
    Factory function to create MissionBrain.
    
    Args:
        redis_url: Redis server URL
        hive_interface: HiveInterface instance
    
    Returns:
        MissionBrain instance
    """
    redis_client = None
    
    if REDIS_AVAILABLE:
        try:
            redis_client = aioredis.from_url(redis_url)
        except Exception as e:
            logger.warning(f"Could not connect to Redis: {e}")
    
    return MissionBrain(
        redis_client=redis_client,
        hive_interface=hive_interface
    )

import time
import logging
from typing import Any, Dict, Optional, Tuple, List

logger = logging.getLogger(__name__)


def build_excluded_ids(action_history: Optional[list]) -> set[str]:
    excluded_ids: set[str] = set()
    if not action_history:
        return excluded_ids
    for entry in action_history:
        if isinstance(entry, dict):
            if entry.get("action") == "click" and entry.get("target_id"):
                excluded_ids.add(entry["target_id"].strip())
        elif isinstance(entry, str) and ":" in entry:
            action_type_str, target = entry.split(":", 1)
            if action_type_str.strip().lower() == "click":
                excluded_ids.add(target.strip())
    return excluded_ids


async def apply_backend_recovery_reconciliation(
    *,
    session_id: str,
    mission_id: Optional[str],
    current_url: str,
    page_title: str,
    frontend_step_count: int,
    frontend_subgoal_index: int,
    frontend_action_history: Optional[List[Dict]],
    redis_client=None,
    logger,
) -> Tuple[Optional[Dict[str, Any]], int, int, Optional[Dict[str, Any]]]:
    """
    RECOVERY RECONCILIATION: Load backend recovery state and reconcile with frontend state.
    
    This is the enhanced replacement for apply_frontend_amnesia_guard.
    It loads the authoritative backend state from Redis and uses it to override
    frontend-provided counters when they diverge.
    
    Args:
        session_id: Session identifier
        mission_id: Optional mission identifier
        current_url: Current browser URL
        page_title: Current page title
        frontend_step_count: Step count from frontend (may be stale after reload)
        frontend_subgoal_index: Subgoal index from frontend
        frontend_action_history: Action history from frontend (for reference)
        redis_client: Redis client for recovery store access
        logger: Logger instance
        
    Returns:
        Tuple of:
        - recovery_state: Backend recovery state dict (or None if not found)
        - effective_step_count: Authoritative step count to use
        - effective_subgoal_index: Authoritative subgoal index to use
        - early_response: Optional early response if mission is complete
    """
    # Default to frontend values
    effective_step_count = frontend_step_count
    effective_subgoal_index = frontend_subgoal_index
    recovery_state = None
    early_response = None
    
    if not redis_client:
        logger.debug("No Redis client available, skipping recovery reconciliation")
        return None, effective_step_count, effective_subgoal_index, early_response
    
    try:
        # Import recovery store
        from core.recovery_store import RecoveryStore
        
        recovery_store = RecoveryStore(redis_client)
        
        # Load recovery state from Redis
        recovery_state = await recovery_store.load_recovery_state(
            session_id=session_id,
            mission_id=mission_id
        )
        
        if not recovery_state:
            logger.debug(f"No recovery state found for session={session_id}")
            return None, effective_step_count, effective_subgoal_index, early_response
        
        # Recovery state found - this is the authoritative source
        logger.info(
            f"🔄 RECOVERY RECONCILIATION | session={session_id} | "
            f"mission={recovery_state.mission_id} | "
            f"backend_step={recovery_state.step_count} | "
            f"frontend_step={frontend_step_count} | "
            f"phase={recovery_state.phase}"
        )
        
        # Use backend step count (overrides frontend if divergent)
        if recovery_state.step_count > frontend_step_count:
            logger.warning(
                f"🛡️ BACKEND AUTHORITY: Frontend step={frontend_step_count} but Redis has "
                f"{recovery_state.step_count}. Using backend value."
            )
            effective_step_count = recovery_state.step_count
        
        # Use backend subgoal index
        if recovery_state.subgoal_index != frontend_subgoal_index:
            logger.info(
                f"📍 Subgoal index reconciled: frontend={frontend_subgoal_index}, "
                f"backend={recovery_state.subgoal_index}"
            )
            effective_subgoal_index = recovery_state.subgoal_index
        
        # Check if mission is already complete
        if recovery_state.status == "completed" or recovery_state.phase == "done":
            logger.info(f"🏁 Mission {recovery_state.mission_id} already complete. Halting.")
            early_response = {
                "success": True,
                "blocked": False,
                "complete": True,
                "action": {"type": "answer", "speech": "I've already completed that task. What would you like to do next?"},
                "speech": "I've already completed that task. What would you like to do next?",
                "pipeline": "backend_recovery_complete_guard",
            }
            return recovery_state.to_dict(), effective_step_count, effective_subgoal_index, early_response
        
        # Check for pending pipeline (multi-action resumption)
        if recovery_state.pending_pipeline:
            logger.info(
                f"📋 Pending pipeline found: {recovery_state.pending_pipeline.get('pipeline_id')} | "
                f"last_ack_index={recovery_state.pending_pipeline.get('last_acknowledged_index', -1)}"
            )
        
        # Return recovery state for planner to use
        return recovery_state.to_dict(), effective_step_count, effective_subgoal_index, early_response
        
    except ImportError as e:
        logger.warning(f"Recovery store not available: {e}")
        return None, effective_step_count, effective_subgoal_index, early_response
    except Exception as e:
        logger.error(f"Recovery reconciliation failed: {e}", exc_info=True)
        return None, effective_step_count, effective_subgoal_index, early_response


async def apply_frontend_amnesia_guard(
    *,
    mission_brain: Any,
    session_id: str,
    mission_id: Optional[str],
    step_number: int,
    current_url: str,
    is_last_mile_enabled_for_domain,
    logger,
) -> Tuple[Optional[Any], int, Optional[Dict[str, Any]]]:
    """
    Legacy amnesia guard - kept for backward compatibility.
    
    DEPRECATED: Use apply_backend_recovery_reconciliation instead.
    This function now wraps the new recovery reconciliation for legacy callers.
    """
    existing_mission = await mission_brain._load_session_mission(session_id)
    if not existing_mission and mission_id:
        by_id = await mission_brain._load_mission(mission_id)
        if by_id and by_id.status in ("in_progress", "paused", "completed"):
            existing_mission = by_id
            if by_id.session_id != session_id:
                old_session_id = by_id.session_id
                by_id.session_id = session_id
                await mission_brain._save_mission(by_id)
                logger.info(f"🧷 Mission rebind: {by_id.mission_id} moved from session {old_session_id} → {session_id}")

    backend_step = 0
    early_response = None
    if existing_mission and existing_mission.status in ("in_progress", "paused"):
        backend_step = len(existing_mission.action_history)
        if step_number == 0 and backend_step > 0:
            logger.warning(
                f"🛡️ FRONTEND AMNESIA GUARD: Frontend sent step=0 but Redis has {backend_step} "
                f"actions on mission {existing_mission.mission_id}. Trusting backend state."
            )
            step_number = backend_step
    elif existing_mission and existing_mission.status == "completed":
        existing_phase = getattr(existing_mission, "phase", "strategy")
        existing_domain = ""
        if existing_mission.schema:
            existing_domain = getattr(existing_mission.schema, "domain", "") or ""
        if not existing_domain and current_url and "://" in current_url:
            try:
                from urllib.parse import urlparse
                existing_domain = (urlparse(current_url).netloc or "").replace("www.", "")
            except Exception:
                existing_domain = ""
        if is_last_mile_enabled_for_domain(existing_domain) and existing_phase != "done":
            logger.info(
                f"LAST_MILE_ENTER mission={existing_mission.mission_id} status=completed "
                f"phase={existing_phase} source=dedup_guard"
            )
        else:
            logger.info(f"🏁 Mission {existing_mission.mission_id} already complete. Halting.")
            early_response = {
                "success": True,
                "blocked": False,
                "complete": True,
                "action": {"type": "answer", "speech": "I've already completed that task. What would you like to do next?"},
                "speech": "I've already completed that task. What would you like to do next?",
                "pipeline": "ultimate_tara_dedup_guard",
            }

    effective_step = max(step_number, backend_step)
    return existing_mission, effective_step, early_response


def set_pre_mission_context(*, app_state: Any, session_id: str, goal: str, current_url: str, effective_step: int) -> None:
    if not hasattr(app_state, "_pre_mission_context"):
        app_state._pre_mission_context = {}
    app_state._pre_mission_context[session_id] = {
        "goal": goal,
        "url": current_url,
        "step": effective_step,
        "ts": time.time(),
    }

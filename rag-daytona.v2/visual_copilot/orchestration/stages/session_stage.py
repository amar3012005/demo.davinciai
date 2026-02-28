import time
from typing import Any, Dict, Optional, Tuple


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

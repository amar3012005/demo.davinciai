import time
from typing import Any, Dict, Optional, Tuple


async def handle_terminal_mission_state(
    *,
    mission: Any,
    last_mile_enabled: bool,
    mission_brain: Any,
    validate_and_end_mission,
    schema: Any,
    nodes: list,
    app: Any,
    start_time: float,
    logger,
) -> Tuple[Any, Optional[Dict[str, Any]]]:
    if not mission or mission.status not in ("completed", "paused"):
        return mission, None

    if last_mile_enabled and getattr(mission, "phase", "strategy") != "done":
        logger.info(
            f"LAST_MILE_ENTER mission={mission.mission_id} status={mission.status} "
            f"phase={getattr(mission, 'phase', 'strategy')} reason=mission_terminal_state"
        )
        mission.status = "in_progress"
        mission.phase = "last_mile"
        if not getattr(mission, "last_mile_started_at", None):
            mission.last_mile_started_at = time.time()
        await mission_brain._save_mission(mission)
        return mission, None

    logger.info("🏁 Mission already complete. Generating end dialogue...")
    end_result = await validate_and_end_mission(schema, nodes, mission, mission_brain, app, start_time)
    if end_result:
        return mission, end_result

    return mission, {
        "success": True,
        "blocked": False,
        "action": {
            "type": "answer",
            "speech": f"I've completed the task for {schema.target_entity}. What would you like to do next?",
        },
        "complete": True,
        "pipeline": "ultimate_tara",
        "confidence": 1.0,
    }

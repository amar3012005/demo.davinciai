import time
from typing import Any, Optional

from visual_copilot.logging.config import emit_event, get_logger

logger = get_logger("vc.mission.service")


class MissionService:
    def __init__(self, mission_brain: Any):
        self._mission_brain = mission_brain

    async def load_existing(self, *, session_id: str, mission_id: Optional[str]) -> Optional[Any]:
        mission = await self._mission_brain._load_session_mission(session_id)
        if mission is None and mission_id:
            mission = await self._mission_brain._load_mission(mission_id)
        return mission

    async def get_or_create(
        self,
        *,
        trace_id: str,
        session_id: str,
        schema: Any,
        strategy: Any,
        nodes: list,
        app: Any,
        mission_id: Optional[str] = None,
    ) -> Any:
        start = time.time()
        mission = await self._mission_brain.get_or_create_mission(
            session_id=session_id,
            schema=schema,
            strategy=strategy,
        )
        emit_event(
            logger,
            "VC_MISSION_READY",
            trace_id=trace_id,
            session_id=session_id,
            mission_id=getattr(mission, "mission_id", ""),
            module="vc.mission.service",
            step_number=len(getattr(mission, "action_history", []) or []),
            subgoal_index=int(getattr(mission, "current_subgoal_index", 0) or 0),
            decision="loaded" if mission_id else "active",
            reason=f"subgoals={len(getattr(mission, 'subgoals', []) or [])}",
            duration_ms=int((time.time() - start) * 1000),
        )
        return mission

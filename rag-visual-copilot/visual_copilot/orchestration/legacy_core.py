"""Deprecated compatibility wrappers for Visual CoPilot.

Runtime ownership is now in modular modules:
- plan-next-step: visual_copilot.orchestration.plan_next_step_flow
- update-constraint: visual_copilot.mission.constraints

This module remains import-compatible for older call sites.
"""

from typing import Any, Dict, Optional

from visual_copilot.mission.constraints import update_constraint
from visual_copilot.orchestration.plan_next_step_flow import ultimate_plan_next_step_impl


async def ultimate_plan_next_step(
    app: Any,
    session_id: str,
    goal: str,
    current_url: str,
    step_number: int = 0,
    action_history: Optional[list] = None,
    previous_goal: Optional[str] = None,
    mission_id: Optional[str] = None,
) -> Dict[str, Any]:
    return await ultimate_plan_next_step_impl(
        app=app,
        session_id=session_id,
        goal=goal,
        current_url=current_url,
        step_number=step_number,
        action_history=action_history,
        previous_goal=previous_goal,
        mission_id=mission_id,
    )


async def ultimate_update_constraint(
    app: Any,
    mission_id: str,
    constraint_name: str,
    value: str,
) -> Dict[str, Any]:
    mission_brain = getattr(app.state, "mission_brain", None)
    if mission_brain is None:
        return {"success": False, "error": "Mission Brain not initialized"}

    return await update_constraint(
        mission_brain=mission_brain,
        mission_id=mission_id,
        constraint_name=constraint_name,
        value=value,
    )

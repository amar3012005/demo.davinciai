"""
Ultimate API facade -> Visual CoPilot modular pipeline.

Keeps import contract stable for app.py and any legacy callers.
"""

from typing import Any, Dict, Optional

from visual_copilot.orchestration.pipeline import run_pipeline, run_update_constraint


async def ultimate_plan_next_step(
    app,
    session_id: str,
    goal: str,
    current_url: str,
    step_number: int = 0,
    action_history: Optional[list] = None,
    screenshot_b64: str = "",
) -> Dict[str, Any]:
    return await run_pipeline(
        app=app,
        session_id=session_id,
        goal=goal,
        current_url=current_url,
        step_number=step_number,
        action_history=action_history or [],
        screenshot_b64=screenshot_b64,
    )


async def ultimate_update_constraint(
    app,
    mission_id: str,
    constraint_name: str,
    value: str,
) -> Dict[str, Any]:
    return await run_update_constraint(
        app=app,
        mission_id=mission_id,
        constraint_name=constraint_name,
        value=value,
    )

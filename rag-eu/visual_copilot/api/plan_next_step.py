from typing import Optional, Any

from visual_copilot.orchestration.pipeline import run_pipeline


async def ultimate_plan_next_step(
    app: Any,
    session_id: str,
    goal: str,
    current_url: str,
    step_number: int,
    action_history: list[str],
    previous_goal: Optional[str] = None,
    mission_id: Optional[str] = None,
):
    return await run_pipeline(
        app=app,
        session_id=session_id,
        goal=goal,
        current_url=current_url,
        step_number=step_number,
        action_history=action_history,
        previous_goal=previous_goal,
        mission_id=mission_id,
    )

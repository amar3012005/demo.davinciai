from typing import Any, Dict

from visual_copilot.orchestration.pipeline import run_update_constraint


async def ultimate_update_constraint(
    app: Any,
    mission_id: str,
    constraint_name: str,
    value: str,
) -> Dict:
    return await run_update_constraint(
        app=app,
        mission_id=mission_id,
        constraint_name=constraint_name,
        value=value,
    )

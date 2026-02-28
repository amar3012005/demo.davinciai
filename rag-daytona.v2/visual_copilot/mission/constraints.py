from typing import Any, Dict


async def update_constraint(mission_brain: Any, mission_id: str, constraint_name: str, value: str) -> Dict:
    return await mission_brain.update_constraint(
        mission_id=mission_id,
        constraint_name=constraint_name,
        value=value,
    )

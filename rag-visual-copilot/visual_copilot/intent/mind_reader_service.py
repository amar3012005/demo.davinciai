import time
from typing import Any, List, Optional

from visual_copilot.logging.config import emit_event, get_logger

logger = get_logger("vc.intent.mind_reader")


class MindReaderService:
    def __init__(self, mind_reader: Any):
        self._mind_reader = mind_reader

    async def parse(
        self,
        *,
        trace_id: str,
        session_id: str,
        goal: str,
        current_url: str,
        previous_goal: Optional[str],
        nodes: Optional[List[Any]],
    ) -> Any:
        start = time.time()
        schema = await self._mind_reader.translate(
            user_input=goal,
            current_url=current_url,
            previous_goal=previous_goal,
            nodes=nodes,
        )
        emit_event(
            logger,
            "VC_INTENT_PARSED",
            trace_id=trace_id,
            session_id=session_id,
            mission_id="",
            module="vc.intent.mind_reader",
            step_number=0,
            subgoal_index=0,
            decision=getattr(getattr(schema, "action", None), "value", "unknown"),
            reason=getattr(schema, "target_entity", ""),
            duration_ms=int((time.time() - start) * 1000),
        )
        return schema

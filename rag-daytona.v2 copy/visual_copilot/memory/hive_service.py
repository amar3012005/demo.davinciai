import time
from typing import Any

from tara_models import HiveResponse
from visual_copilot.logging.config import emit_event, get_logger
from visual_copilot.memory.cache_service import CacheService

logger = get_logger("vc.memory.hive")


class HiveService:
    def __init__(self, hive_interface: Any):
        self._hive_interface = hive_interface

    async def retrieve(self, *, app_state: Any, trace_id: str, session_id: str, schema: Any, effective_step: int) -> Any:
        start = time.time()
        if effective_step > 0:
            cached = CacheService.get_hive(app_state, session_id)
            if cached is not None:
                return cached

        known = await self._hive_interface.is_domain_indexed(getattr(schema, "domain", "") or "")
        if known:
            response = await self._hive_interface.retrieve(schema)
            reason = "indexed"
        else:
            response = HiveResponse(
                strategy=None,
                visual_hints=[],
                cached=False,
                query_time_ms=0,
                strategy_score=0.0,
                strategy_query_used="",
                strategy_threshold_used=0.0,
                strategy_accepted=False,
            )
            reason = "domain_not_indexed"

        CacheService.set_hive(app_state, session_id, response)
        emit_event(
            logger,
            "VC_HIVE_RETRIEVE",
            trace_id=trace_id,
            session_id=session_id,
            mission_id="",
            module="vc.memory.hive",
            step_number=effective_step,
            subgoal_index=0,
            decision="strategy" if bool(getattr(response, "strategy", None)) else "no_strategy",
            reason=reason,
            duration_ms=int((time.time() - start) * 1000),
        )
        return response

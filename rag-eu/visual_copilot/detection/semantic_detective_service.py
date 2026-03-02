from typing import Any, List, Optional

from visual_copilot.constants import DETECTIVE_MIN_SCORE
from visual_copilot.routing.action_guard import _action_tag_compatible


class SemanticDetectiveService:
    def __init__(self, detective: Any):
        self._detective = detective

    async def investigate(
        self,
        *,
        session_id: str,
        query: str,
        hive_hints: Optional[List[Any]],
        excluded_ids: Optional[List[str]],
        subgoal_mode: str,
    ) -> Any:
        return await self._detective.investigate(
            session_id=session_id,
            query=query,
            hive_hints=hive_hints or [],
            excluded_ids=excluded_ids or [],
            subgoal_mode=subgoal_mode,
        )

    @staticmethod
    def acceptable(report: Any, *, subgoal_mode: str, min_score: float = DETECTIVE_MIN_SCORE) -> bool:
        best = getattr(report, "best_match", None)
        if not best:
            return False
        score = float(getattr(best, "final_score", 0.0) or getattr(best, "hybrid_score", 0.0) or 0.0)
        if score < min_score:
            return False
        return _action_tag_compatible(subgoal_mode, best)

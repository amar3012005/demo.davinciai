from typing import Any, Dict, List, Optional

from visual_copilot.constants import DETECTIVE_MIN_SCORE
from visual_copilot.detection.semantic_detective_service import SemanticDetectiveService


async def route_semantic(
    *,
    service: SemanticDetectiveService,
    session_id: str,
    query: str,
    hive_hints: Optional[List[Any]],
    excluded_ids: List[str],
    subgoal_mode: str,
) -> Optional[Dict[str, Any]]:
    report = await service.investigate(
        session_id=session_id,
        query=query,
        hive_hints=hive_hints,
        excluded_ids=excluded_ids,
        subgoal_mode=subgoal_mode,
    )
    if not service.acceptable(report, subgoal_mode=subgoal_mode, min_score=DETECTIVE_MIN_SCORE):
        return None

    best = getattr(report, "best_match", None)
    if not best:
        return None

    action_type = "type_text" if subgoal_mode == "literal_type" else "click"
    action: Dict[str, Any] = {
        "type": action_type,
        "target_id": getattr(best, "node_id", ""),
        "confidence": float(getattr(best, "final_score", 0.0) or getattr(best, "hybrid_score", 0.0) or 0.0),
    }
    return {
        "route": "semantic_detective",
        "reason": f"score>={DETECTIVE_MIN_SCORE}",
        "action": action,
        "report": report,
    }

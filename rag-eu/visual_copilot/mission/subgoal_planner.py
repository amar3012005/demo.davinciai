from typing import Any, List


def current_subgoal(mission: Any) -> str:
    subgoals = list(getattr(mission, "subgoals", []) or [])
    idx = int(getattr(mission, "current_subgoal_index", 0) or 0)
    if idx < 0 or idx >= len(subgoals):
        return ""
    return str(subgoals[idx] or "")


def subgoal_hint_queries(mission: Any, *, max_items: int = 2) -> List[str]:
    subgoals = [str(s).strip() for s in list(getattr(mission, "subgoals", []) or []) if str(s).strip()]
    if not subgoals:
        return []
    idx = int(getattr(mission, "current_subgoal_index", 0) or 0)
    head = subgoals[idx: idx + max_items]
    return head[:max_items]

from typing import Any, Iterable, List, Optional

from visual_copilot.routing.action_guard import _is_clickable_node, _is_type_node


def prefilter_candidates(
    *,
    nodes: Iterable[Any],
    subgoal_mode: Optional[str],
    excluded_ids: Optional[set[str]] = None,
) -> List[Any]:
    excluded_ids = excluded_ids or set()
    filtered: List[Any] = []
    for node in nodes:
        if not getattr(node, "interactive", False):
            continue
        node_id = getattr(node, "id", "")
        if node_id in excluded_ids:
            continue
        if subgoal_mode == "literal_click" and not _is_clickable_node(node):
            continue
        if subgoal_mode == "literal_type" and not _is_type_node(node):
            continue
        filtered.append(node)
    return filtered

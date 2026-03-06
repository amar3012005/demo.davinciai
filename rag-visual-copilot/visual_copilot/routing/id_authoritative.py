from typing import Any, Dict, List, Optional

from visual_copilot.routing.action_guard import _validate_action_target
from visual_copilot.text.tokenization import _extract_explicit_target_id


def route_explicit_id(*, query: str, subgoal_mode: str, nodes: List[Any], excluded_ids: set[str], default_type_text: str = "") -> Optional[Dict[str, Any]]:
    target_id = _extract_explicit_target_id(query)
    if not target_id:
        return None

    action_type = "type_text" if subgoal_mode == "literal_type" else "click"
    ok, reason = _validate_action_target(action_type, target_id, nodes, excluded_ids=excluded_ids)
    if not ok:
        return None

    action: Dict[str, Any] = {"type": action_type, "target_id": target_id, "confidence": 0.99}
    if action_type == "type_text":
        action["text"] = default_type_text
    return {"route": "id_authoritative", "reason": reason, "action": action}

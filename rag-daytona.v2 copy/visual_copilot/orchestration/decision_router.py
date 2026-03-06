from typing import Any, Dict, List, Optional

from visual_copilot.constants import LEXICAL_DIRECT_ACCEPT
from visual_copilot.routing.id_authoritative import route_explicit_id
from visual_copilot.routing.lexical_router import _lexical_ground_candidate
from visual_copilot.routing.semantic_router import route_semantic
from visual_copilot.text.tokenization import _classify_subgoal_mode, _extract_type_text


async def route_step(
    *,
    session_id: str,
    query: str,
    schema: Any,
    nodes: List[Any],
    hive_hints: list,
    excluded_ids: set[str],
    semantic_service: Any,
) -> Dict[str, Any]:
    mode = _classify_subgoal_mode(query)

    explicit = route_explicit_id(
        query=query,
        subgoal_mode=mode,
        nodes=nodes,
        excluded_ids=excluded_ids,
        default_type_text=_extract_type_text(query, getattr(schema, "target_entity", "")),
    )
    if explicit:
        explicit["mode"] = mode
        return explicit

    lexical = _lexical_ground_candidate(query, schema, nodes, mode, excluded_ids)
    if lexical and float(lexical.get("score", 0.0) or 0.0) >= LEXICAL_DIRECT_ACCEPT:
        node = lexical["node"]
        action = {
            "type": "type_text" if mode == "literal_type" else "click",
            "target_id": getattr(node, "id", ""),
            "confidence": float(lexical.get("score", 0.0) or 0.0),
        }
        if action["type"] == "type_text":
            action["text"] = _extract_type_text(query, getattr(schema, "target_entity", ""))
        return {"route": "lexical", "reason": "high_lexical_confidence", "action": action, "mode": mode}

    if mode in {"literal_click", "literal_type", "ambiguous"}:
        semantic = await route_semantic(
            service=semantic_service,
            session_id=session_id,
            query=query,
            hive_hints=hive_hints,
            excluded_ids=list(excluded_ids),
            subgoal_mode=mode,
        )
        if semantic:
            semantic["mode"] = mode
            if semantic["action"]["type"] == "type_text":
                semantic["action"].setdefault("text", _extract_type_text(query, getattr(schema, "target_entity", "")))
            return semantic

    return {"route": "tier3_required", "reason": "no_deterministic_match", "action": {}, "mode": mode}

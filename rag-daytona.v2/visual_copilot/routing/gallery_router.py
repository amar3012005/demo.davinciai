from typing import Any, List, Optional, Tuple

from visual_copilot.constants import _GALLERY_WORDS, _CLICK_TAGS
from visual_copilot.routing.action_guard import _resolve_clickable_target_id


def _is_gallery_subgoal(query: str) -> bool:
    q = (query or "").lower()
    return any(w in q for w in _GALLERY_WORDS) and any(v in q for v in ("open", "click", "show", "view"))


def _is_image_like_node(node: Any) -> bool:
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    text = ((getattr(node, "text", "") or "") + " " + (getattr(node, "id", "") or "")).lower()
    if tag in {"img", "image", "picture", "figure"} or role in {"img", "image"}:
        return True
    return any(k in text for k in ("thumb", "thumbnail", "image", "photo", "pic", "gallery", "card"))


def _find_gallery_click_target(nodes: List[Any], excluded_ids: set[str]) -> Optional[Tuple[str, str]]:
    best = None
    best_score = -1.0
    for n in nodes:
        if not getattr(n, "interactive", False):
            continue
        nid = getattr(n, "id", "")
        if not nid or nid in excluded_ids:
            continue
        zone = (getattr(n, "zone", "") or "").lower()
        text = (getattr(n, "text", "") or "").lower()
        tag = (getattr(n, "tag", "") or "").lower()
        score = 0.0
        if _is_image_like_node(n):
            score += 0.8
        if zone == "main":
            score += 0.35
        if tag in _CLICK_TAGS:
            score += 0.15
        if tag in {"img", "picture", "figure"}:
            score += 0.25
        if zone in {"nav", "header", "sidebar", "footer"}:
            score -= 0.30
        if any(x in text for x in ("random gallery", "categories", "tags", "search")):
            score -= 0.25
        if score > best_score:
            best_score = score
            best = n

    if not best or best_score < 0.55:
        return None

    target_id = getattr(best, "id", "")
    resolved_id = target_id
    if (getattr(best, "tag", "") or "").lower() not in _CLICK_TAGS:
        rid, _ = _resolve_clickable_target_id(target_id, nodes, excluded_ids=excluded_ids)
        if rid:
            resolved_id = rid
    return resolved_id, target_id

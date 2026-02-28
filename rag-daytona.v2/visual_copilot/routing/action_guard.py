import logging
from typing import Any, List, Optional, Tuple

from visual_copilot.constants import _CLICK_TAGS, _CLICK_ROLES, _TYPE_TAGS, _TYPE_ROLES, _TEXT_HEAVY_TAGS
from visual_copilot.text.tokenization import _canonicalize_label
from visual_copilot.text.label_extraction import _collect_node_text_for_match
from visual_copilot.text.normalization import _expand_label_synonyms

logger = logging.getLogger(__name__)


def _is_clickable_node(node: Any) -> bool:
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    return tag in _CLICK_TAGS or role in _CLICK_ROLES


def _is_type_node(node: Any) -> bool:
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    return tag in _TYPE_TAGS or role in _TYPE_ROLES


def _is_text_heavy(tag: str, text: str) -> bool:
    words = len((text or "").split())
    return tag.lower() in _TEXT_HEAVY_TAGS and words >= 8


def _action_tag_compatible(subgoal_mode: str, candidate: Any) -> bool:
    if not candidate:
        return False
    tag = (getattr(candidate, "tag", "") or "").lower()
    text = getattr(candidate, "text", "") or ""
    role = (getattr(candidate, "role", "") or "").lower()
    if subgoal_mode == "literal_type":
        return tag in _TYPE_TAGS or role in _TYPE_ROLES
    if subgoal_mode == "literal_click":
        if tag in _CLICK_TAGS or role in _CLICK_ROLES:
            return not _is_text_heavy(tag, text)
        return False
    return True


def _validate_action_target(action: str, target_id: str, nodes: List[Any], *, excluded_ids: Optional[set[str]] = None) -> Tuple[bool, str]:
    if action in {"answer", "wait", "clarify", "scroll"}:
        return True, "ok"
    if not target_id:
        return False, "missing_target_id"
    node = next((n for n in nodes if getattr(n, "id", "") == target_id and getattr(n, "interactive", False)), None)
    if not node:
        return False, "target_not_in_live_graph"
    if excluded_ids and action == "click" and target_id in excluded_ids:
        return False, "target_already_excluded"
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    if action == "type_text" and not (tag in _TYPE_TAGS or role in _TYPE_ROLES):
        return False, "type_target_not_input"
    if action == "click" and not (tag in _CLICK_TAGS or role in _CLICK_ROLES):
        return False, "click_target_not_clickable"
    return True, "ok"


def _node_text_blob(node: Any) -> str:
    return " ".join([
        getattr(node, "text", "") or "",
        getattr(node, "id", "") or "",
        getattr(node, "role", "") or "",
        getattr(node, "zone", "") or "",
        getattr(node, "state", "") or "",
        str(getattr(node, "placeholder", "") or ""),
        str(getattr(node, "name", "") or ""),
        str(getattr(node, "value", "") or ""),
    ]).lower()


def _node_matches_expected_labels(node: Any, labels: List[str], domain: str) -> bool:
    if not node or not labels:
        return False
    blob = _canonicalize_label(_collect_node_text_for_match(node))
    if not blob:
        return False
    expanded = []
    for l in labels:
        expanded.extend(_expand_label_synonyms(l, domain))
    return any(e and (e == blob or e in blob) for e in expanded)


def _resolve_clickable_target_id(target_id: str, nodes: List[Any], excluded_ids: Optional[set[str]] = None) -> Tuple[Optional[str], str]:
    if not target_id:
        return None, "missing_target_id"
    node_map = {getattr(n, "id", ""): n for n in nodes}
    seen = set()
    current_id = target_id
    chain: List[str] = []
    for _ in range(8):
        if not current_id or current_id in seen:
            break
        seen.add(current_id)
        chain.append(current_id)
        node = node_map.get(current_id)
        if not node:
            return None, "target_not_in_live_graph"
        if excluded_ids and current_id in excluded_ids:
            return None, "target_already_excluded"
        if getattr(node, "interactive", False) and _is_clickable_node(node):
            if len(chain) > 1:
                logger.info(f"ROUTER_SHADOW ancestor_resolve chain={' -> '.join(chain)}")
            return current_id, "ok"
        current_id = getattr(node, "parent_id", None) or ""
    return None, "click_target_not_clickable"


def _resolve_clickable_by_label_context(raw_node_id: str, labels: List[str], domain: str, nodes: List[Any], excluded_ids: Optional[set[str]] = None) -> Tuple[Optional[str], str]:
    if not raw_node_id or not labels:
        return None, "missing_context"
    node_map = {getattr(n, "id", ""): n for n in nodes}
    raw_node = node_map.get(raw_node_id)
    if not raw_node:
        return None, "raw_node_not_found"

    expected_variants: List[str] = []
    for label in labels:
        expected_variants.extend(_expand_label_synonyms(label, domain))
    expected_variants = [v for v in expected_variants if v]
    if not expected_variants:
        return None, "no_expected_variants"

    raw_parent = getattr(raw_node, "parent_id", "") or ""
    raw_zone = (getattr(raw_node, "zone", "") or "").lower()
    best_id: Optional[str] = None
    best_score = 0.0

    for n in nodes:
        nid = getattr(n, "id", "")
        if not nid or nid == raw_node_id:
            continue
        if excluded_ids and nid in excluded_ids:
            continue
        if not (getattr(n, "interactive", False) and _is_clickable_node(n)):
            continue
        blob = _canonicalize_label(_collect_node_text_for_match(n))
        if not blob:
            continue

        score = 0.0
        for v in expected_variants:
            if v == blob:
                score = max(score, 1.0)
            elif v in blob:
                score = max(score, 0.96)
            else:
                vt = set(v.split())
                bt = set(blob.split())
                overlap = len(vt & bt)
                if overlap > 0:
                    score = max(score, min(0.90, overlap / max(1, len(vt))))

        if score <= 0.0:
            continue
        if raw_parent and (getattr(n, "parent_id", "") or "") == raw_parent:
            score += 0.05
        if raw_zone and (getattr(n, "zone", "") or "").lower() == raw_zone:
            score += 0.02

        if score > best_score:
            best_score = score
            best_id = nid

    if best_id and best_score >= 0.93:
        return best_id, "ok"
    return None, "no_clickable_label_peer"

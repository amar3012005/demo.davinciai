import logging
import re
from typing import Any, List, Tuple

from visual_copilot.constants import _CLICK_ROLES, _CLICK_TAGS
from visual_copilot.routing.action_guard import _is_clickable_node
from visual_copilot.text.tokenization import _canonicalize_label, _strategy_focus_terms, _tokenize

logger = logging.getLogger(__name__)


def is_reclick_safe_node(node: Any) -> bool:
    if not node:
        return False
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    zone = (getattr(node, "zone", "") or "").lower()
    text = _canonicalize_label(getattr(node, "text", "") or "")
    node_id = (getattr(node, "id", "") or "").lower()
    state = (getattr(node, "state", "") or "").lower()
    aria_expanded = str(getattr(node, "aria_expanded", "") or "").lower()
    aria_selected = str(getattr(node, "aria_selected", "") or "").lower()

    if tag not in _CLICK_TAGS and role not in _CLICK_ROLES:
        return False

    trigger_words = {"menu", "dropdown", "tab", "tabs", "trigger", "developers", "docs", "documentation", "usage", "activity"}
    has_trigger_signal = any(w in text for w in trigger_words) or any(w in node_id for w in trigger_words)
    has_state_signal = any(v in {"true", "false"} for v in {aria_expanded, aria_selected}) or state in {
        "expanded", "collapsed", "active", "inactive", "open", "closed", "selected"
    }
    in_nav_cluster = zone in {"nav", "sidebar", "header", "menu", "modal"}

    return bool(
        role in {"tab", "menuitem"}
        or (in_nav_cluster and (has_trigger_signal or has_state_signal))
        or ("trigger" in node_id)
    )


def drop_reclick_safe_exclusions(excluded_ids: set[str], nodes: List[Any]) -> set[str]:
    if not excluded_ids:
        return excluded_ids
    by_id = {getattr(n, "id", ""): n for n in nodes}
    removed: List[str] = []
    for nid in list(excluded_ids):
        node = by_id.get(nid)
        if node and is_reclick_safe_node(node):
            excluded_ids.discard(nid)
            removed.append(nid)
    if removed:
        logger.info(f"EXCLUSION_RELAX removed={len(removed)} reclick_safe_ids={removed[:4]}")
    return excluded_ids


def mission_progress_label(mission: Any) -> str:
    subgoals = list(getattr(mission, "subgoals", []) or [])
    total = len(subgoals)
    raw_idx = int(getattr(mission, "current_subgoal_index", 0) or 0)
    if total <= 0:
        return f"{raw_idx}/0 (step 0 of 0)"
    shown_idx = min(max(0, raw_idx), total - 1)
    suffix = " terminal_handoff" if raw_idx >= total else ""
    return f"{shown_idx}/{total} (step {shown_idx + 1} of {total}{suffix})"


def extract_visible_goal_evidence(main_goal: str, nodes: List[Any]) -> str:
    goal_terms = _tokenize(main_goal)
    if not goal_terms:
        return ""
    candidates: List[Tuple[int, int, str]] = []
    required_overlap = max(2, min(4, len(goal_terms)))
    for n in nodes:
        zone = (getattr(n, "zone", "") or "").lower()
        if zone not in {"main", "content"}:
            continue
        text = (getattr(n, "text", "") or "").strip()
        if len(text) < 18:
            continue
        line_terms = _tokenize(text)
        overlap = len(goal_terms & line_terms)
        has_numeric_signal = bool(re.search(r"\d", text))
        if overlap >= required_overlap and has_numeric_signal:
            candidates.append((overlap, len(text), text))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][2][:320]


def extract_model_usage_evidence(main_goal: str, nodes: List[Any]) -> str:
    goal_tokens = _tokenize(main_goal)
    if not goal_tokens:
        return ""

    model_tokens = [
        t for t in sorted(goal_tokens)
        if t not in {"show", "read", "usage", "token", "tokens", "model", "models", "cost", "spend", "dashboard", "console", "groq"}
    ]
    if not model_tokens:
        return ""

    ordered = [n for n in nodes if (getattr(n, "zone", "") or "").lower() in {"main", "content"}]
    for idx, n in enumerate(ordered):
        txt = (getattr(n, "text", "") or "").strip()
        if not txt:
            continue
        canon = _canonicalize_label(txt)
        if not all(tok in canon for tok in model_tokens[: min(2, len(model_tokens))]):
            continue

        for j in range(idx + 1, min(idx + 5, len(ordered))):
            vtxt = (getattr(ordered[j], "text", "") or "").strip()
            if not vtxt:
                continue
            if re.search(r"[$€£]\s?\d", vtxt) or re.search(r"\d[\d,\.]*\s?(?:tokens?|usd)", vtxt.lower()):
                return f"{txt}: {vtxt}"[:320]
    return ""


def subgoal_focus_visible(query: str, nodes: List[Any]) -> bool:
    terms = _strategy_focus_terms(query)
    if not terms:
        return True
    for n in nodes:
        txt = _canonicalize_label(getattr(n, "text", "") or "")
        if not txt:
            continue
        if all(t in txt for t in terms):
            return True
    return False


def retarget_click_to_nav_duplicate_if_needed(*, target_node: Any, nodes: List[Any], query: str, goal: str):
    if not target_node:
        return target_node, "no_target"

    target_text = _canonicalize_label(getattr(target_node, "text", "") or "")
    if not target_text:
        return target_node, "no_target_text"

    ctx = _canonicalize_label(f"{query} {goal}")
    nav_intent_terms = {"docs", "documentation", "developer", "developers", "stt", "speech", "text", "model"}
    if not any(t in ctx for t in nav_intent_terms):
        return target_node, "no_nav_intent"

    zone = (getattr(target_node, "zone", "") or "").lower()
    if zone in {"nav", "sidebar", "header", "menu"}:
        return target_node, "already_nav_zone"

    dup = [
        n for n in nodes
        if getattr(n, "interactive", False)
        and _is_clickable_node(n)
        and (getattr(n, "id", "") or "") != (getattr(target_node, "id", "") or "")
        and _canonicalize_label(getattr(n, "text", "") or "") == target_text
        and (getattr(n, "zone", "") or "").lower() in {"nav", "sidebar", "header", "menu"}
    ]
    if not dup:
        return target_node, "no_nav_duplicate"

    zone_priority = {"nav": 0, "sidebar": 1, "header": 2, "menu": 3}
    dup.sort(key=lambda n: zone_priority.get((getattr(n, "zone", "") or "").lower(), 9))
    return dup[0], "nav_duplicate_preferred"

import hashlib
import time
from typing import Any, Dict, List, Tuple

from visual_copilot.text.tokenization import _canonicalize_label, _strategy_focus_terms


def should_advance_from_dom(*, query: str, nodes: List[Any], min_hits: int = 1) -> bool:
    terms = _strategy_focus_terms(query)
    if not terms:
        return True
    hits = 0
    for node in nodes:
        text = _canonicalize_label(getattr(node, "text", "") or "")
        if not text:
            continue
        if all(t in text for t in terms):
            hits += 1
            if hits >= min_hits:
                return True
    return False


def snapshot_node_state(node: Any) -> Dict[str, Any]:
    if not node:
        return {}
    return {
        "state": getattr(node, "state", "") or "",
        "value": str(getattr(node, "value", "") or ""),
        "aria_selected": str(getattr(node, "aria_selected", "") or ""),
        "aria_expanded": str(getattr(node, "aria_expanded", "") or ""),
        "text": (getattr(node, "text", "") or "")[:200],
    }


def build_dom_signature(nodes: List[Any]) -> str:
    rows: List[str] = []
    for n in nodes:
        if not getattr(n, "interactive", False):
            continue
        rows.append(
            "|".join(
                [
                    getattr(n, "id", "") or "",
                    getattr(n, "zone", "") or "",
                    getattr(n, "tag", "") or "",
                    (getattr(n, "text", "") or "")[:120],
                    getattr(n, "state", "") or "",
                    str(getattr(n, "aria_selected", "") or ""),
                    str(getattr(n, "aria_expanded", "") or ""),
                    str(getattr(n, "value", "") or ""),
                ]
            )
        )
    rows.sort()
    raw = "\n".join(rows)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def verify_pending_action_effect(
    mission: Any,
    nodes: List[Any],
    current_url: str,
    current_dom_signature: str,
) -> Tuple[bool, str]:
    pending = getattr(mission, "pending_action", None) or {}
    if not pending:
        return False, "no_pending_action"

    action_type = (pending.get("type") or "").lower()
    target_id = pending.get("target_id", "")
    prev_url = (getattr(mission, "last_url", "") or "").strip()
    prev_sig = (getattr(mission, "last_dom_signature", "") or "").strip()
    prev_snapshot = pending.get("target_snapshot") or {}

    if prev_url and current_url and prev_url != current_url:
        return True, "url_changed"
    if prev_sig and current_dom_signature and prev_sig != current_dom_signature:
        return True, "dom_signature_changed"

    node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
    if not node:
        return True, "target_disappeared"

    cur_snapshot = snapshot_node_state(node)
    if prev_snapshot and cur_snapshot != prev_snapshot:
        return True, "target_state_changed"

    if action_type == "type_text":
        typed = (pending.get("text") or "").strip().lower()
        if typed:
            node_value = str(getattr(node, "value", "") or "").lower()
            if typed in node_value:
                return True, "input_contains_typed_text"

    # ── Flyout / dropdown detection ─────────────────────────────────────────
    # Menus like "Pics" set aria_expanded=true when opened but may not change
    # the overall DOM signature (the menu items are pre-rendered, just shown/hidden).
    prev_aria_expanded = str(prev_snapshot.get("aria_expanded", "") or "")
    cur_aria_expanded = str((getattr(node, "aria_expanded", "") or ""))
    if prev_aria_expanded != cur_aria_expanded and cur_aria_expanded.lower() in ("true", "1"):
        return True, "aria_expanded_changed"

    # ── New interactive nodes appeared ──────────────────────────────────────
    # If the click revealed new interactive DOM nodes that weren't in the
    # pre-click snapshot (tracked via pending_action["node_count"]), treat it as an effect.
    prev_node_count = int(pending.get("node_count") or 0)
    if prev_node_count:
        cur_node_count = sum(1 for n in nodes if getattr(n, "interactive", False))
        if cur_node_count > prev_node_count + 2:  # allow minor noise
            return True, "new_nodes_appeared"

    return False, "no_observable_effect"



async def record_and_stage_pending_action(
    *,
    mission_brain: Any,
    mission: Any,
    action_type: str,
    target_id: str,
    nodes: List[Any],
    current_url: str,
    dom_signature: str,
    text: str = "",
) -> None:
    target_node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
    if action_type == "type_text":
        await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text)
    else:
        await mission_brain.record_action(mission.mission_id, "click", target_id, True)

    pending_action = {
        "type": action_type,
        "target_id": target_id,
        "text": text,
        "target_snapshot": snapshot_node_state(target_node),
        "node_count": sum(1 for n in nodes if getattr(n, "interactive", False)),
        "created_at": time.time(),
    }

    await mission_brain.set_pending_action(
        mission.mission_id,
        pending_action,
        dom_signature,
        current_url,
    )


async def record_and_maybe_advance(
    *,
    mission_brain: Any,
    mission: Any,
    action_type: str,
    target_id: str,
    nodes: List[Any],
    current_url: str,
    dom_signature: str,
    text: str = "",
    verified_advance_active: bool = False,
    is_zero_shot: bool = False,
) -> int:
    if verified_advance_active:
        await record_and_stage_pending_action(
            mission_brain=mission_brain,
            mission=mission,
            action_type=action_type,
            target_id=target_id,
            nodes=nodes,
            current_url=current_url,
            dom_signature=dom_signature,
            text=text,
        )
        return mission.current_subgoal_index

    if action_type == "type_text":
        await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text)
    else:
        await mission_brain.record_action(mission.mission_id, "click", target_id, True)
    await mission_brain.advance_subgoal(
        mission.mission_id,
        is_zero_shot=is_zero_shot,
    )
    _updated = await mission_brain._load_mission(mission.mission_id)
    return _updated.current_subgoal_index if _updated else mission.current_subgoal_index + 1

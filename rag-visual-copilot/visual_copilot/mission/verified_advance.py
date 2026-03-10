import hashlib
import logging
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from visual_copilot.navigation.site_map_validator import SiteMapValidator, get_validator
from visual_copilot.text.tokenization import _canonicalize_label, _strategy_focus_terms

logger = logging.getLogger(__name__)

# Site map validator instance (lazy-loaded)
_site_map_validator: SiteMapValidator = None


def _get_validator() -> SiteMapValidator:
    """Get or create the site map validator instance."""
    global _site_map_validator
    if _site_map_validator is None:
        _site_map_validator = get_validator()
    return _site_map_validator


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

    # ── STRICT NAVIGATION VALIDATION ─────────────────────────────────────────
    # For navigation subgoals (e.g., "Click Usage"), verify URL changed to expected destination
    
    # First, check if this is a navigation subgoal
    subgoals = getattr(mission, "subgoals", []) or []
    current_idx = getattr(mission, "current_subgoal_index", 0) or 0
    is_navigation_subgoal = False
    expected_target_name = ""
    
    if current_idx < len(subgoals):
        subgoal = subgoals[current_idx]
        subgoal_text = (subgoal or "").lower()
        if subgoal_text.startswith("click "):
            is_navigation_subgoal = True
            expected_target_name = subgoal_text[6:].strip().lower()
    
    # For navigation subgoals, URL MUST change to expected destination
    if is_navigation_subgoal and prev_url and current_url:
        # ── SITE MAP VALIDATION ─────────────────────────────────────────────
        # Use site map to validate navigation against ground truth
        validator = _get_validator()

        # Get the node we were on before navigation
        from_node = validator.get_node_for_url(prev_url)

        if from_node:
            # Use site map to predict expected outcome
            outcome = validator.get_expected_navigation_outcome(
                from_node.get("node_id", ""),
                expected_target_name
            )

            if outcome and outcome.expected_node:
                # Validate against site map expected URL pattern
                expected_url_pattern = outcome.expected_url
                expected_node_id = outcome.expected_node.get("node_id", "")

                # Check if current URL matches expected pattern
                is_valid, validation_msg = validator.validate_navigation_success(
                    from_url=prev_url,
                    to_url=current_url,
                    clicked_target=expected_target_name
                )

                if is_valid:
                    logger.info(
                        f"PENDING_VERIFY_SITE_MAP_SUCCESS: Navigation to '{expected_target_name}' "
                        f"validated via site map. Reached '{expected_node_id}'. "
                        f"Reason: {validation_msg}"
                    )
                    return True, f"site_map_validated_{validation_msg}"
                else:
                    # Site map validation failed
                    logger.warning(
                        f"PENDING_VERIFY_SITE_MAP_FAILED: {validation_msg}. "
                        f"From '{from_node.get('node_id')}' expected '{expected_node_id}' "
                        f"but current URL is '{current_url}'"
                    )

                    # Provide recovery suggestion
                    recovery = outcome.alternative_targets
                    if recovery:
                        alt_ids = [n.get('node_id', '?') for n in recovery[:2]]
                        logger.info(f"PENDING_VERIFY_RECOVERY_SUGGESTION: Try alternatives {alt_ids}")

                    return False, f"site_map_validation_failed_{validation_msg}"

        # ── FALLBACK: Pattern-based validation ──────────────────────────────
        # If site map validation unavailable, use pattern matching
        url_lower = current_url.lower()
        expected_patterns = [
            f"/{expected_target_name}",
            f"/{expected_target_name}/",
            f"-{expected_target_name}",
            f"_{expected_target_name}",
        ]
        matches_expected = any(p in url_lower for p in expected_patterns)

        if prev_url == current_url:
            # URL didn't change at all - navigation definitely failed
            logger.warning(
                f"PENDING_VERIFY_NAV_FAILED: Navigation subgoal 'Click {expected_target_name}' "
                f"did not change URL (still '{current_url}'). Click likely failed."
            )
            return False, "navigation_failed_url_unchanged"
        elif not matches_expected:
            # URL changed but not to expected destination
            logger.warning(
                f"PENDING_VERIFY_URL_MISMATCH: Expected navigation to '{expected_target_name}' "
                f"but URL changed from '{prev_url}' to '{current_url}'"
            )
            return False, "navigation_failed_wrong_destination"
        else:
            # URL changed to expected destination
            return True, f"url_changed_to_expected_{expected_target_name}"
    
    # For non-navigation actions, use standard verification
    if prev_url and current_url and prev_url != current_url:
        # URL changed but couldn't verify it was the expected destination
        # This is a weaker signal - only accept if the change is significant
        # (not just a hash or query param change)
        from urllib.parse import urlparse
        prev_parsed = urlparse(prev_url)
        curr_parsed = urlparse(current_url)

        # Check if path changed (not just hash or query)
        if prev_parsed.path != curr_parsed.path:
            return True, "url_path_changed"
        else:
            # Only hash or query changed - this is likely not a real navigation
            logger.warning(
                f"PENDING_VERIFY_WEAK_URL_CHANGE: Path unchanged, only hash/query changed "
                f"from '{prev_url}' to '{current_url}'"
            )
            # Don't return True - this is likely a false positive
    
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

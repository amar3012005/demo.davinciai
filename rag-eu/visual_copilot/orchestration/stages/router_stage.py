from typing import Any, Dict, List, Tuple


def build_router_context(
    *,
    query: str,
    schema: Any,
    mission: Any,
    hive_response: Any,
    nodes: List[Any],
    domain_name: str,
    subgoal_mode_fn,
    override_mode_for_labels_fn,
    extract_label_candidates_fn,
    extract_explicit_target_id_fn,
    is_v3_keyword_active: bool,
    subgoal_hint_query_active: bool,
    hive_interface: Any,
    mission_brain: Any,
    logger,
) -> Dict[str, Any]:
    subgoal_mode = subgoal_mode_fn(query)
    strategy_authoritative = bool(getattr(mission, "strategy_locked", False))
    explicit_target_id_in_query = extract_explicit_target_id_fn(query)
    allow_first_subgoal_fallback = (not strategy_authoritative) and (not explicit_target_id_in_query)
    if explicit_target_id_in_query:
        logger.info(
            f"SUBGOAL_CONTEXT_LOCK reason=explicit_id_query id={explicit_target_id_in_query} first_subgoal_fallback=false"
        )

    label_candidates = extract_label_candidates_fn(
        query,
        schema,
        allow_first_subgoal_fallback=allow_first_subgoal_fallback,
    )

    logger.info(
        f"TURN_DIAG strategy_locked={getattr(mission, 'strategy_locked', False)} "
        f"strategy_score={getattr(hive_response, 'strategy_score', 0.0):.2f} "
        f"subgoal_idx={mission.current_subgoal_index + 1}/{max(1, len(mission.subgoals))}"
    )

    overridden_mode = override_mode_for_labels_fn(subgoal_mode, label_candidates)
    if is_v3_keyword_active and overridden_mode != subgoal_mode:
        logger.info(
            f"ROUTER_MODE_OVERRIDE reason=label_present old={subgoal_mode} new={overridden_mode} labels={label_candidates[:2]}"
        )
        subgoal_mode = overridden_mode

    return {
        "subgoal_mode": subgoal_mode,
        "strategy_authoritative": strategy_authoritative,
        "explicit_target_id_in_query": explicit_target_id_in_query,
        "label_candidates": label_candidates,
        "effective_hive_hints": list(hive_response.visual_hints or []),
    }

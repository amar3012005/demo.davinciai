import time
from typing import Any, Dict, Optional, Set


async def run_router_lexical_stage(
    *,
    app: Any,
    goal: str,
    query: str,
    schema: Any,
    mission: Any,
    mission_brain: Any,
    nodes: list,
    excluded_ids: Set[str],
    label_policy: Dict[str, Any],
    subgoal_mode: str,
    strategy_authoritative: bool,
    is_zero_shot: bool,
    domain_name: str,
    current_url: str,
    current_dom_signature: str,
    verified_advance_active: bool,
    start_time: float,
    should_use_detective_v2: bool,
    bypass_reason_v2: str,
    router_v2_enabled: bool,
    router_v2_shadow: bool,
    lexical_direct_accept_type: float,
    lexical_direct_accept_click: float,
    logger,
    lexical_ground_candidate_fn,
    is_canary_domain_fn,
    node_matches_strategy_focus_fn,
    record_and_maybe_advance_fn,
    extract_type_text_fn,
    run_tier3_fallback_fn,
    find_best_type_target_fn,
    validate_action_target_fn,
) -> Optional[Dict[str, Any]]:
    lexical_hit = lexical_ground_candidate_fn(
        query=query,
        schema=schema,
        nodes=nodes,
        subgoal_mode=subgoal_mode,
        excluded_ids=excluded_ids,
    ) if subgoal_mode in {"literal_click", "literal_type"} else None

    if lexical_hit and router_v2_shadow:
        logger.info(
            f"ROUTER_SHADOW lexical_hit score={lexical_hit['score']:.2f} "
            f"node={lexical_hit['node'].id} mode={subgoal_mode}"
        )
    if router_v2_shadow and (not router_v2_enabled) and not should_use_detective_v2:
        logger.info(
            f"ROUTER_SHADOW would_bypass_detective mode={subgoal_mode} "
            f"reason={bypass_reason_v2 or 'cognitive'}"
        )

    canary_active = is_canary_domain_fn(domain_name)
    allow_lexical_direct = not label_policy.get("has_explicit_label", False)
    direct_threshold = (
        lexical_direct_accept_type if subgoal_mode == "literal_type" else lexical_direct_accept_click
    )

    lexical_direct_safe = True
    if lexical_hit:
        if subgoal_mode == "literal_click":
            lexical_direct_safe = bool(
                lexical_hit.get("zone_match", True)
                and (
                    lexical_hit.get("label_exact", False)
                    or lexical_hit.get("explicit_overlap", 0) >= 1
                )
            )
        elif subgoal_mode == "literal_type":
            lexical_direct_safe = bool(
                lexical_hit.get("label_exact", False)
                or lexical_hit.get("explicit_overlap", 0) >= 1
            )
        if strategy_authoritative and subgoal_mode == "literal_click":
            lexical_direct_safe = bool(
                lexical_direct_safe
                and node_matches_strategy_focus_fn(lexical_hit["node"], query)
            )

    router_v2_active = router_v2_enabled or canary_active
    if router_v2_active:
        if canary_active and not router_v2_enabled:
            logger.info(
                f"ROUTER_DECISION canary_override active domain={domain_name} "
                "global_enabled=False"
            )
        if lexical_hit and not canary_active:
            logger.info(
                f"ROUTER_SHADOW lexical_rejected reason=domain_not_in_canary "
                f"domain={domain_name} score={lexical_hit['score']:.2f}"
            )
        if lexical_hit and canary_active and lexical_hit["score"] < direct_threshold:
            logger.info(
                f"ROUTER_SHADOW lexical_rejected reason=below_threshold "
                f"domain={domain_name} score={lexical_hit['score']:.2f} threshold={direct_threshold:.2f}"
            )
        if (
            lexical_hit
            and canary_active
            and strategy_authoritative
            and subgoal_mode == "literal_click"
            and not node_matches_strategy_focus_fn(lexical_hit["node"], query)
        ):
            logger.info(
                "ROUTER_SHADOW lexical_rejected reason=strategy_subgoal_mismatch "
                f"domain={domain_name} query='{query[:80]}'"
            )
        if lexical_hit and canary_active and not lexical_direct_safe:
            logger.info(
                "ROUTER_SHADOW lexical_rejected reason=failed_explicit_match "
                f"domain={domain_name} score={lexical_hit['score']:.2f} "
                f"explicit_overlap={lexical_hit.get('explicit_overlap', 0)} "
                f"label_exact={lexical_hit.get('label_exact', False)} "
                f"zone_match={lexical_hit.get('zone_match', True)} "
                f"terms={lexical_hit.get('explicit_terms', [])} "
                f"zones={lexical_hit.get('target_zones', [])}"
            )
        if lexical_hit and canary_active and not allow_lexical_direct:
            logger.info("ROUTER_SHADOW lexical_rejected reason=explicit_label_policy")

        if lexical_hit and canary_active and lexical_hit["score"] >= direct_threshold and lexical_direct_safe and allow_lexical_direct:
            ln = lexical_hit["node"]
            if subgoal_mode == "literal_type":
                text_to_type = extract_type_text_fn(query, schema.target_entity)
                logger.info(
                    f"ROUTER_DECISION mode={subgoal_mode} detective_used=False "
                    f"score={lexical_hit['score']:.2f} threshold={direct_threshold:.2f} "
                    f"domain={domain_name} tier=lexical_direct"
                )
                _next_idx = await record_and_maybe_advance_fn(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="type_text",
                    target_id=ln.id,
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=current_dom_signature,
                    text=text_to_type,
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                )
                return {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "type_text",
                        "target_id": ln.id,
                        "text": text_to_type,
                        "press_enter": True,
                    },
                    "speech": f"Typing '{text_to_type}' and searching...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_router_lexical",
                    "router_mode": subgoal_mode,
                    "detective_used": False,
                    "detective_score": 0.0,
                    "fallback_tier": "lexical",
                    "pending_verification": verified_advance_active,
                }
            logger.info(
                f"ROUTER_DECISION mode={subgoal_mode} detective_used=False "
                f"score={lexical_hit['score']:.2f} threshold={direct_threshold:.2f} "
                f"domain={domain_name} tier=lexical_direct"
            )
            _next_idx = await record_and_maybe_advance_fn(
                mission_brain=mission_brain,
                mission=mission,
                action_type="click",
                target_id=ln.id,
                nodes=nodes,
                current_url=current_url,
                dom_signature=current_dom_signature,
                verified_advance_active=verified_advance_active,
                is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
            )
            return {
                "success": True,
                "blocked": False,
                "action": {"type": "click", "target_id": ln.id, "text": ln.text[:60] if ln.text else ""},
                "speech": f"Clicking on {(ln.text[:40].strip() if ln.text else 'the element')}...",
                "mission_id": mission.mission_id,
                "subgoal_index": _next_idx,
                "confidence": "high",
                "timing_ms": int((time.time() - start_time) * 1000),
                "pipeline": "ultimate_tara_router_lexical",
                "router_mode": subgoal_mode,
                "detective_used": False,
                "detective_score": 0.0,
                "fallback_tier": "lexical",
                "pending_verification": verified_advance_active,
            }

        if not should_use_detective_v2:
            tier3_result = await run_tier3_fallback_fn(
                app=app,
                goal=goal,
                query=query,
                nodes=nodes,
                mission=mission,
                mission_brain=mission_brain,
                schema=schema,
                is_zero_shot=is_zero_shot,
                start_time=start_time,
                forced_reason=bypass_reason_v2 or "cognitive_bypass",
                excluded_ids=excluded_ids,
                expected_labels=label_policy["label_candidates"] if label_policy.get("has_explicit_label") else None,
                expected_domain=domain_name,
                current_url=current_url,
                dom_signature=current_dom_signature,
                verified_advance_active=verified_advance_active,
            )
            if tier3_result:
                tier3_result["router_mode"] = subgoal_mode
                tier3_result["detective_used"] = False
                tier3_result["detective_score"] = 0.0
                tier3_result["fallback_tier"] = "tier3_direct"
                return tier3_result
            return {
                "success": False,
                "blocked": True,
                "reason": f"I cannot confidently execute '{query}' on this screen.",
                "action": None,
                "router_mode": subgoal_mode,
                "detective_used": False,
                "detective_score": 0.0,
                "fallback_tier": "tier3_failed",
            }

    if subgoal_mode == "literal_type":
        heuristic_type_node = find_best_type_target_fn(nodes, query, excluded_ids)
        if heuristic_type_node:
            text_to_type = extract_type_text_fn(query, schema.target_entity)
            ok, reason = validate_action_target_fn("type_text", heuristic_type_node.id, nodes, excluded_ids=excluded_ids)
            if ok:
                logger.info(
                    f"ROUTER_DECISION mode={subgoal_mode} detective_used=False "
                    f"tier=heuristic_type target={heuristic_type_node.id}"
                )
                _next_idx = await record_and_maybe_advance_fn(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="type_text",
                    target_id=heuristic_type_node.id,
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=current_dom_signature,
                    text=text_to_type,
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                )
                return {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "type_text",
                        "target_id": heuristic_type_node.id,
                        "text": text_to_type,
                        "press_enter": True,
                    },
                    "speech": f"Typing '{text_to_type}' and searching...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_router_heuristic_type",
                    "router_mode": subgoal_mode,
                    "detective_used": False,
                    "detective_score": 0.0,
                    "fallback_tier": "heuristic_type",
                    "pending_verification": verified_advance_active,
                }
            logger.warning(f"ROUTER_SHADOW heuristic_type_rejected reason={reason}")

    return None

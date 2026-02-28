import time
from typing import Any, Dict, List, Set


async def run_router_pre_detective_stage(
    *,
    app: Any,
    session_id: str,
    goal: str,
    current_url: str,
    start_time: float,
    mission: Any,
    schema: Any,
    nodes: list,
    mission_brain: Any,
    semantic_detective: Any,
    live_graph: Any,
    hive_response: Any,
    hive_interface: Any,
    query: str,
    domain_name: str,
    excluded_ids: Set[str],
    is_zero_shot: bool,
    verified_advance_active: bool,
    current_dom_signature: str,
    location_guard_candidate: str,
    keyword_direct_v3_active: bool,
    subgoal_hint_query_active: bool,
    tara_router_v2_enabled: bool,
    tara_router_v2_shadow: bool,
    logger,
    build_router_context_fn,
    classify_subgoal_mode_fn,
    override_mode_for_labels_fn,
    extract_label_candidates_fn,
    extract_explicit_target_id_fn,
    validate_action_target_fn,
    record_and_maybe_advance_fn,
    check_if_arrived_fn,
    run_read_only_terminal_fn,
    is_gallery_subgoal_fn,
    find_gallery_click_target_fn,
    build_label_policy_fn,
    find_hard_keyword_match_fn,
    resolve_clickable_target_id_fn,
    resolve_clickable_by_label_context_fn,
    extract_type_text_fn,
    retarget_click_to_nav_duplicate_if_needed_fn,
    validate_and_end_mission_fn,
    is_canary_domain_fn,
    match_result_cls,
) -> Dict[str, Any]:
    def _is_question_like(text: str) -> bool:
        t = (text or "").strip().lower()
        return ("?" in t) or t.startswith(("what ", "which ", "how ", "why ", "where ", "when "))

    router_ctx = build_router_context_fn(
        query=query,
        schema=schema,
        mission=mission,
        hive_response=hive_response,
        nodes=nodes,
        domain_name=domain_name,
        subgoal_mode_fn=classify_subgoal_mode_fn,
        override_mode_for_labels_fn=override_mode_for_labels_fn,
        extract_label_candidates_fn=extract_label_candidates_fn,
        extract_explicit_target_id_fn=extract_explicit_target_id_fn,
        is_v3_keyword_active=keyword_direct_v3_active,
        subgoal_hint_query_active=subgoal_hint_query_active,
        hive_interface=hive_interface,
        mission_brain=mission_brain,
        logger=logger,
    )
    subgoal_mode = router_ctx["subgoal_mode"]
    strategy_authoritative = router_ctx["strategy_authoritative"]
    explicit_target_id_in_query = router_ctx["explicit_target_id_in_query"]
    label_candidates = router_ctx["label_candidates"]
    effective_hive_hints = router_ctx["effective_hive_hints"]
    allow_first_subgoal_fallback = (not strategy_authoritative) and (not explicit_target_id_in_query)

    if subgoal_hint_query_active:
        subgoal_hint_queries: List[str] = []
        subgoal_hint_queries.append(query)
        subgoal_hint_queries.extend(label_candidates)
        first_sub = getattr(schema, "first_subgoal", "") or ""
        strategy_is_authoritative = bool(getattr(mission, "strategy_locked", False))
        if first_sub and not strategy_is_authoritative:
            subgoal_hint_queries.append(first_sub)
        dedup_queries: List[str] = []
        seen_queries = set()
        for q in subgoal_hint_queries:
            sq = (q or "").strip()
            if not sq:
                continue
            k = sq.lower()
            if k in seen_queries:
                continue
            seen_queries.add(k)
            dedup_queries.append(sq)
        if dedup_queries:
            mission.subgoal_hint_queries = dedup_queries[:6]
            await mission_brain._save_mission(mission)
            if strategy_is_authoritative:
                logger.info(
                    f"SUBGOAL_QUERY_LOCK query='{query[:80]}' source=strategy_only "
                    f"queries={dedup_queries[:3]}"
                )
            try:
                extra_hints = await hive_interface.retrieve_visual_hints_for_queries(
                    schema=schema,
                    queries=dedup_queries[:6],
                    use_cache=True,
                )
                if extra_hints:
                    merged = []
                    seen_hint = set()
                    for hint in effective_hive_hints + extra_hints:
                        key = (
                            getattr(hint, "selector", ""),
                            getattr(hint, "element_type", ""),
                            getattr(hint, "zone", ""),
                            getattr(hint, "text_pattern", "") or "",
                        )
                        if key in seen_hint:
                            continue
                        seen_hint.add(key)
                        merged.append(hint)
                    effective_hive_hints = merged
            except Exception as e:
                logger.warning(f"HIVE_HINT_QUERY source=subgoal failed: {e}")

    if explicit_target_id_in_query and subgoal_mode in {"literal_click", "literal_type"}:
        explicit_node = next(
            (
                n
                for n in nodes
                if getattr(n, "id", "") == explicit_target_id_in_query
                and getattr(n, "interactive", False)
            ),
            None,
        )
        if explicit_node:
            explicit_action = "click" if subgoal_mode == "literal_click" else "type_text"
            if explicit_action == "click":
                ok_target, reason_target = validate_action_target_fn(
                    "click", explicit_target_id_in_query, nodes, excluded_ids=excluded_ids
                )
                if ok_target:
                    logger.info(
                        f"ID_AUTHORITATIVE_HIT action=click id={explicit_target_id_in_query}"
                    )
                    _next_idx = await record_and_maybe_advance_fn(
                        mission_brain=mission_brain,
                        mission=mission,
                        action_type="click",
                        target_id=explicit_target_id_in_query,
                        nodes=nodes,
                        current_url=current_url,
                        dom_signature=current_dom_signature,
                        verified_advance_active=verified_advance_active,
                        is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                    )
                    return {
                        "response": {
                            "success": True,
                            "blocked": False,
                            "action": {
                                "type": "click",
                                "target_id": explicit_target_id_in_query,
                                "text": (getattr(explicit_node, "text", "") or "")[:80],
                            },
                            "speech": f"Clicking {(getattr(explicit_node, 'text', '') or 'the selected element')[:60]}...",
                            "mission_id": mission.mission_id,
                            "subgoal_index": _next_idx,
                            "confidence": "high",
                            "pipeline": "ultimate_tara_id_authoritative",
                            "pending_verification": verified_advance_active,
                            "timing_ms": int((time.time() - start_time) * 1000),
                        }
                    }
                logger.info(
                    f"ID_AUTHORITATIVE_SKIP action=click id={explicit_target_id_in_query} reason={reason_target}"
                )

    if location_guard_candidate and subgoal_mode != "cognitive_read":
        total_subgoals = len(mission.subgoals or [])
        current_idx = mission.current_subgoal_index
        has_plan = total_subgoals > 0
        on_final_subgoal = (not has_plan) or (current_idx >= max(0, total_subgoals - 1))
        if not on_final_subgoal:
            logger.info(
                f"⏩ LOCATION GUARD Skipped: mission still in progress "
                f"({current_idx + 1}/{total_subgoals}). Guard runs at terminal phase only."
            )
        else:
            logger.info(f"🎯 LOCATION GUARD: Terminal-phase LLM check for '{schema.target_entity}'...")
            nodes = await live_graph.get_visible_nodes(session_id)
            is_arrived, arrival_reason = await check_if_arrived_fn(
                app, session_id, goal, current_url, nodes, schema
            )
            if is_arrived:
                logger.info("🎯 LOCATION GUARD: LLM confirmed arrival at terminal phase. Generating final answer...")
                visible_text = " ".join([n.text for n in nodes if n.text])
                try:
                    summary_prompt = f"The user asked '{goal}'. Summarize the answer briefly using this page content: {visible_text[:3000]}"
                    if hasattr(app.state, 'mind_reader') and hasattr(app.state.mind_reader, 'llm'):
                        final_answer = await app.state.mind_reader.llm.generate(
                            summary_prompt,
                            model="llama-3.3-70b-versatile",
                            temperature=0.3,
                        )
                    else:
                        final_answer = f"I've reached the '{schema.target_entity}' documentation."
                except Exception as e:
                    logger.error(f"Failed to generate LLM summary for location guard: {e}")
                    final_answer = f"I've reached the '{schema.target_entity}' documentation, but had trouble reading the text."
                return {
                    "response": {
                        "success": True,
                        "blocked": False,
                        "action": {
                            "type": "answer",
                            "text": final_answer,
                        },
                        "speech": final_answer,
                        "complete": True,
                        "pipeline": "ultimate_tara",
                        "confidence": 1.0,
                    }
                }

    total_subgoals = len(mission.subgoals or [])
    current_idx = mission.current_subgoal_index
    on_final_strategy_subgoal = (total_subgoals > 0) and (current_idx >= max(0, total_subgoals - 1))
    action_name = str(getattr(getattr(schema, "action", None), "value", getattr(schema, "action", ""))).lower()
    is_commerce_flow = action_name in {"search", "purchase"}
    goal_text = (getattr(schema, "raw_utterance", "") or goal or "").strip()
    should_block_terminal_read_mode = is_commerce_flow and not _is_question_like(goal_text)

    if subgoal_mode == "cognitive_read" and should_block_terminal_read_mode:
        logger.info(
            "⏩ READ MODE BLOCKED: commerce/search intent without question-like goal; "
            "routing through action path."
        )
        subgoal_mode = "ambiguous"

    if (
        subgoal_mode == "cognitive_read"
        and getattr(mission, "phase", "strategy") != "last_mile"
        and not on_final_strategy_subgoal
    ):
        logger.info(
            f"⏩ READ MODE BYPASS: intermediate subgoal ({current_idx + 1}/{total_subgoals}); "
            "routing through detective."
        )
        subgoal_mode = "ambiguous"
    elif subgoal_mode == "cognitive_read" and on_final_strategy_subgoal:
        logger.info(
            f"🧠 READ MODE: final strategy subgoal ({current_idx + 1}/{total_subgoals}); "
            "running read-only extraction."
        )

    if subgoal_mode == "cognitive_read":
        logger.info("🧠 READ MODE: terminal read-only path (no click/type fallback).")
        read_result = await run_read_only_terminal_fn(
            session_id=session_id,
            app=app,
            goal=goal,
            query=query,
            schema=schema,
            nodes=nodes,
            mission=mission,
            mission_brain=mission_brain,
            semantic_detective=semantic_detective,
            hive_hints=effective_hive_hints,
            excluded_ids=excluded_ids,
            start_time=start_time,
            is_zero_shot=is_zero_shot,
            current_url=current_url,
            dom_signature=current_dom_signature,
            verified_advance_active=verified_advance_active,
        )
        return {"response": read_result}

    if subgoal_mode in {"literal_click", "cognitive_navigate", "ambiguous"} and is_gallery_subgoal_fn(query):
        gallery_target = find_gallery_click_target_fn(nodes, excluded_ids)
        if gallery_target:
            resolved_id, raw_id = gallery_target
            ok, reason = validate_action_target_fn("click", resolved_id, nodes, excluded_ids=excluded_ids)
            if ok:
                logger.info(
                    f"GALLERY_DIRECT_HIT target={resolved_id} raw={raw_id} "
                    f"query='{query[:80]}'"
                )
                _next_idx = await record_and_maybe_advance_fn(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="click",
                    target_id=resolved_id,
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=current_dom_signature,
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                )
                node = next((n for n in nodes if getattr(n, "id", "") == resolved_id), None)
                return {
                    "response": {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "click", "target_id": resolved_id, "text": (node.text[:60] if node and node.text else "")},
                        "speech": "Opening gallery item...",
                        "mission_id": mission.mission_id,
                        "subgoal_index": _next_idx,
                        "confidence": "high",
                        "timing_ms": int((time.time() - start_time) * 1000),
                        "pipeline": "ultimate_tara_gallery_direct",
                        "router_mode": "literal_click",
                        "detective_used": False,
                        "detective_score": 0.0,
                        "fallback_tier": "gallery_direct",
                        "pending_verification": verified_advance_active,
                    }
                }
            logger.info(f"GALLERY_DIRECT_MISS reason={reason}")

    label_policy = build_label_policy_fn(
        query,
        schema,
        subgoal_mode,
        label_candidates=label_candidates,
        allow_first_subgoal_fallback=allow_first_subgoal_fallback,
    )

    if label_policy["has_explicit_label"]:
        hard_match = find_hard_keyword_match_fn(
            nodes=nodes,
            labels=label_policy["label_candidates"],
            domain=domain_name,
            subgoal_mode=subgoal_mode,
            excluded_ids=excluded_ids,
        )
        if (
            subgoal_mode == "literal_click"
            and strategy_authoritative
            and hard_match.candidate_id
            and hard_match.match_mode == "token_overlap"
        ):
            retry_exact = find_hard_keyword_match_fn(
                nodes=nodes,
                labels=label_policy["label_candidates"],
                domain=domain_name,
                subgoal_mode=subgoal_mode,
                excluded_ids=set(),
            )
            if retry_exact.candidate_id and retry_exact.match_mode in {"exact", "synonym"}:
                logger.info(
                    f"KEYWORD_DIRECT_RETRY_HIT reason=excluded_exact_label node={retry_exact.candidate_id} "
                    f"mode={retry_exact.match_mode}"
                )
                hard_match = retry_exact

        if hard_match.candidate_id:
            label_policy["match_mode"] = hard_match.match_mode
            label_policy["matched_label"] = hard_match.matched_label
            label_policy["raw_node_id"] = hard_match.raw_node_id
            if (
                subgoal_mode == "literal_click"
                and strategy_authoritative
                and label_policy["match_mode"] == "token_overlap"
            ):
                label_policy["miss_reason"] = "weak_keyword_overlap"
                logger.info(
                    f"KEYWORD_DIRECT_MISS reason=weak_keyword_overlap labels={label_policy['label_candidates'][:2]}"
                )
                hard_match = match_result_cls(None, "", "none", None, "weak_keyword_overlap")
            if subgoal_mode == "literal_click" and hard_match.candidate_id:
                target_id = hard_match.candidate_id
                resolved_id, resolve_reason = resolve_clickable_target_id_fn(
                    target_id, nodes, excluded_ids=excluded_ids
                )
                if resolved_id:
                    target_id = resolved_id
                else:
                    peer_id, peer_reason = resolve_clickable_by_label_context_fn(
                        raw_node_id=target_id,
                        labels=label_policy["label_candidates"],
                        domain=domain_name,
                        nodes=nodes,
                        excluded_ids=excluded_ids,
                    )
                    if peer_id:
                        target_id = peer_id
                        logger.info(
                            f"KEYWORD_DIRECT_HIT label={label_policy['matched_label']} "
                            f"mode={label_policy['match_mode']} node={label_policy['raw_node_id']} "
                            f"resolved={target_id} resolver=label_peer"
                        )
                    else:
                        label_policy["miss_reason"] = "no_clickable_ancestor"
                        logger.info(
                            f"KEYWORD_DIRECT_MISS reason=no_clickable_ancestor labels={label_policy['label_candidates'][:2]} "
                            f"fallback={peer_reason or resolve_reason}"
                        )
                        target_id = ""
                if target_id:
                    ok, reason = validate_action_target_fn("click", target_id, nodes, excluded_ids=excluded_ids)
                    if ok:
                        label_policy["resolved_target_id"] = target_id
                        logger.info(
                            f"KEYWORD_DIRECT_HIT label={label_policy['matched_label']} "
                            f"mode={label_policy['match_mode']} node={label_policy['raw_node_id']} "
                            f"resolved={target_id}"
                        )
                        _next_idx = await record_and_maybe_advance_fn(
                            mission_brain=mission_brain,
                            mission=mission,
                            action_type="click",
                            target_id=target_id,
                            nodes=nodes,
                            current_url=current_url,
                            dom_signature=current_dom_signature,
                            verified_advance_active=verified_advance_active,
                            is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                        )
                        _click_node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
                        return {
                            "response": {
                                "success": True,
                                "blocked": False,
                                "action": {"type": "click", "target_id": target_id, "text": _click_node.text[:60] if _click_node and _click_node.text else ""},
                                "speech": f"Clicking on {(_click_node.text[:40].strip() if _click_node and _click_node.text else 'the element')}...",
                                "mission_id": mission.mission_id,
                                "subgoal_index": _next_idx,
                                "confidence": "high",
                                "timing_ms": int((time.time() - start_time) * 1000),
                                "pipeline": "ultimate_tara_router_keyword_direct_hard",
                                "router_mode": subgoal_mode,
                                "detective_used": False,
                                "detective_score": 0.0,
                                "fallback_tier": "keyword_direct_hard",
                                "pending_verification": verified_advance_active,
                            }
                        }
                    logger.warning(f"KEYWORD_DIRECT_MISS reason={reason} labels={label_policy['label_candidates'][:2]}")
                    label_policy["miss_reason"] = reason
            elif subgoal_mode == "literal_type":
                ok, reason = validate_action_target_fn("type_text", hard_match.candidate_id, nodes, excluded_ids=excluded_ids)
                if ok:
                    label_policy["resolved_target_id"] = hard_match.candidate_id
                    text_to_type = extract_type_text_fn(query, schema.target_entity)
                    logger.info(
                        f"KEYWORD_DIRECT_HIT label={label_policy['matched_label']} "
                        f"mode={label_policy['match_mode']} node={label_policy['raw_node_id']} "
                        f"resolved={hard_match.candidate_id}"
                    )
                    _next_idx = await record_and_maybe_advance_fn(
                        mission_brain=mission_brain,
                        mission=mission,
                        action_type="type_text",
                        target_id=hard_match.candidate_id,
                        nodes=nodes,
                        current_url=current_url,
                        dom_signature=current_dom_signature,
                        text=text_to_type,
                        verified_advance_active=verified_advance_active,
                        is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                    )
                    return {
                        "response": {
                            "success": True,
                            "blocked": False,
                            "action": {
                                "type": "type_text",
                                "target_id": hard_match.candidate_id,
                                "text": text_to_type,
                                "press_enter": True,
                            },
                            "speech": f"Typing '{text_to_type}' and searching...",
                            "mission_id": mission.mission_id,
                            "subgoal_index": _next_idx,
                            "confidence": "high",
                            "timing_ms": int((time.time() - start_time) * 1000),
                            "pipeline": "ultimate_tara_router_keyword_direct_hard",
                            "router_mode": subgoal_mode,
                            "detective_used": False,
                            "detective_score": 0.0,
                            "fallback_tier": "keyword_direct_hard",
                            "pending_verification": verified_advance_active,
                        }
                    }
                logger.warning(f"KEYWORD_DIRECT_MISS reason={reason} labels={label_policy['label_candidates'][:2]}")
                label_policy["miss_reason"] = reason
        else:
            label_policy["miss_reason"] = hard_match.reason
            logger.info(
                f"KEYWORD_DIRECT_MISS reason={hard_match.reason} labels={label_policy['label_candidates'][:2]}"
            )

    should_use_detective_v2 = subgoal_mode in {"literal_click", "literal_type", "ambiguous"}
    bypass_reason_v2 = "" if should_use_detective_v2 else f"subgoal_mode={subgoal_mode}"
    logger.info(
        f"ROUTER_DECISION mode={subgoal_mode} detective_used={should_use_detective_v2} "
        f"domain={domain_name} tier=precheck enabled={tara_router_v2_enabled} "
        f"shadow={tara_router_v2_shadow} canary={is_canary_domain_fn(domain_name)}"
    )

    import re as _re_fast

    _type_shortcut = _re_fast.match(
        r"Type\s+'([^']+)'\s+.*?\[(?:ID:\s*)?(\S+?)\]",
        query,
        _re_fast.IGNORECASE,
    )
    _click_shortcut = _re_fast.match(
        r"Click\s+.*?\[(?:ID:\s*)?(\S+?)\]",
        query,
        _re_fast.IGNORECASE,
    ) if not _type_shortcut else None

    if _type_shortcut:
        _text_to_type = _type_shortcut.group(1)
        _target_id = _type_shortcut.group(2).rstrip("]")
        _target_node = next(
            (n for n in nodes if n.id == _target_id and n.interactive),
            None,
        )
        if not _target_node:
            _target_node = next(
                (
                    n
                    for n in nodes
                    if n.interactive and n.tag in ('input', 'textarea')
                    and (
                        getattr(n, 'role', '') == 'searchbox'
                        or 'search' in (n.text or '').lower()
                        or 'search' in (getattr(n, 'placeholder', '') or '').lower()
                        or 'search' in (n.id or '').lower()
                    )
                ),
                None,
            )
            if _target_node:
                logger.info(f"   ⚡ FAST PATH: ID '{_target_id}' not found, using search input '{_target_node.id}' instead")
                _target_id = _target_node.id

        _already_typed = any(
            entry.startswith(f"type:{_target_id}")
            for entry in mission.action_history
        )

        if _target_node and not _already_typed:
            logger.info(f"   ⚡ FAST PATH: TYPE '{_text_to_type}' into {_target_id} (tag={_target_node.tag}) + press_enter")
            _next_idx = await record_and_maybe_advance_fn(
                mission_brain=mission_brain,
                mission=mission,
                action_type="type_text",
                target_id=_target_id,
                nodes=nodes,
                current_url=current_url,
                dom_signature=current_dom_signature,
                text=_text_to_type,
                verified_advance_active=verified_advance_active,
                is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
            )
            return {
                "response": {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "type_text",
                        "target_id": _target_id,
                        "text": _text_to_type,
                        "press_enter": True,
                    },
                    "speech": f"Searching for {_text_to_type}...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_fast_path",
                    "pending_verification": verified_advance_active,
                }
            }
        elif _already_typed:
            logger.info(
                f"   ⚡ FAST PATH: Already typed into '{_target_id}'. "
                f"{'Waiting for verification.' if verified_advance_active else 'Auto-advancing to next subgoal.'}"
            )
            if not verified_advance_active:
                await mission_brain.advance_subgoal(
                    mission.mission_id,
                    is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                )
                mission = await mission_brain._load_mission(mission.mission_id)
                if mission and mission.current_subgoal_index < len(mission.subgoals):
                    query = mission.subgoals[mission.current_subgoal_index]
                    logger.info(f"   ⏩ New query (subgoal {mission.current_subgoal_index}): '{query}'")
                else:
                    logger.info("   🏁 All subgoals exhausted after auto-advance.")
        else:
            logger.info(f"   ⚡ FAST PATH: ID '{_target_id}' not found as interactive in DOM, falling through")

    elif _click_shortcut:
        _target_id = _click_shortcut.group(1).rstrip("]")
        _generic_click_shortcut = bool(
            _re_fast.match(r"^\s*Click\s+element\s+\[(?:ID:\s*)?\S+?\]\s*$", query, _re_fast.IGNORECASE)
        )
        _target_node = next(
            (n for n in nodes if n.id == _target_id and n.interactive),
            None,
        )
        if _generic_click_shortcut and _target_node:
            logger.info("   ⚡ FAST PATH: generic 'Click element [ID: ...]' resolved via grounded ID.")
        if _target_node and f"click:{_target_id}" not in mission.action_history:
            _retargeted_node, _retarget_reason = retarget_click_to_nav_duplicate_if_needed_fn(
                target_node=_target_node,
                nodes=nodes,
                query=query,
                goal=goal,
            )
            if _retargeted_node and getattr(_retargeted_node, "id", "") != _target_id:
                logger.info(
                    f"   ⚡ FAST PATH RETARGET: {_target_id} -> {_retargeted_node.id} "
                    f"reason={_retarget_reason}"
                )
                _target_node = _retargeted_node
                _target_id = _retargeted_node.id
            _btn_label = (_target_node.text[:40].strip() if _target_node.text else "the element")
            logger.info(f"   ⚡ FAST PATH: CLICK '{_target_node.text[:30] if _target_node.text else ''}' [ID: {_target_id}]")
            _next_idx = await record_and_maybe_advance_fn(
                mission_brain=mission_brain,
                mission=mission,
                action_type="click",
                target_id=_target_id,
                nodes=nodes,
                current_url=current_url,
                dom_signature=current_dom_signature,
                verified_advance_active=verified_advance_active,
                is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
            )
            return {
                "response": {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "click",
                        "target_id": _target_id,
                        "text": _target_node.text[:60] if _target_node.text else "",
                    },
                    "speech": f"Clicking on {_btn_label}...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_fast_path",
                    "pending_verification": verified_advance_active,
                }
            }

    if query.lower().startswith("ask user:"):
        logger.info(f"   💬 ReAct wants to clarify: '{query}' — skipping Semantic Detective.")
        speech = query[9:].strip()
        mission.status = "paused"
        await mission_brain._save_mission(mission)
        return {
            "response": {
                "success": True,
                "blocked": False,
                "action": {
                    "type": "clarify",
                    "speech": speech,
                },
                "mission_id": mission.mission_id,
                "subgoal_index": mission.current_subgoal_index,
                "confidence": "high",
                "timing_ms": int((time.time() - start_time) * 1000),
                "pipeline": "ultimate_tara",
            }
        }

    if "extract and present" in query.lower():
        logger.info("   🏁 ReAct signaled completion: validating and ending mission.")
        end_result = await validate_and_end_mission_fn(
            schema, nodes, mission, mission_brain, app, start_time
        )
        if end_result:
            return {"response": end_result}

    return {
        "response": None,
        "mission": mission,
        "nodes": nodes,
        "query": query,
        "subgoal_mode": subgoal_mode,
        "strategy_authoritative": strategy_authoritative,
        "effective_hive_hints": effective_hive_hints,
        "label_policy": label_policy,
        "should_use_detective_v2": should_use_detective_v2,
        "bypass_reason_v2": bypass_reason_v2,
    }

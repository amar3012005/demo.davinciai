"""Extracted plan_next_step flow from legacy_core for modular orchestration."""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from tara_models import ActionIntent
from visual_copilot.routing.lexical_router import MatchResult
from visual_copilot.constants import (
    TARA_ROUTER_V2_ENABLED,
    TARA_ROUTER_V2_SHADOW,
    DETECTIVE_MIN_SCORE,
    DETECTIVE_AMBIGUOUS_BAND,
    LEXICAL_DIRECT_ACCEPT_CLICK,
    LEXICAL_DIRECT_ACCEPT_TYPE,
    MAX_DETECTIVE_RETRIES_PER_SUBGOAL,
    LAST_MILE_MAX_ATTEMPTS,
    ENABLE_KEYWORD_DIRECT_V3,
    ENABLE_SUBGOAL_HINT_QUERY,
    ENABLE_VERIFIED_ADVANCE,
    ENABLE_PRE_ROUTER_VISION,
    PRE_ROUTER_VISION_MIN_CONF,
    _TYPE_TAGS,
    _is_v3_feature_enabled,
    _register_v3_pending_drop,
    _register_v3_success,
    _is_canary_domain,
    _is_last_mile_enabled_for_domain,
)
from visual_copilot.text.tokenization import (
    _classify_subgoal_mode,
    _extract_explicit_target_id,
    _extract_type_text,
    _candidate_signature,
    _canonicalize_label,
    _override_mode_for_labels,
)
from visual_copilot.text.label_extraction import _extract_label_candidates
from visual_copilot.routing.action_guard import (
    _validate_action_target,
    _action_tag_compatible,
    _node_matches_expected_labels,
    _resolve_clickable_target_id,
    _resolve_clickable_by_label_context,
)
from visual_copilot.routing.lexical_router import (
    _find_hard_keyword_match,
    _build_label_policy,
    _lexical_ground_candidate,
    _node_matches_strategy_focus,
    _find_best_type_target,
)
from visual_copilot.routing.gallery_router import (
    _is_gallery_subgoal,
    _find_gallery_click_target,
)
from visual_copilot.mission.last_mile import (
    _should_enter_last_mile,
    _goal_completion_guard,
    _validate_last_mile_step,
    _hash_last_mile_sequence,
)
from visual_copilot.orchestration.state_helpers import (
    drop_reclick_safe_exclusions as _drop_reclick_safe_exclusions,
    mission_progress_label as _mission_progress_label,
    extract_visible_goal_evidence as _extract_visible_goal_evidence,
    extract_model_usage_evidence as _extract_model_usage_evidence,
    subgoal_focus_visible as _subgoal_focus_visible,
    retarget_click_to_nav_duplicate_if_needed as _retarget_click_to_nav_duplicate_if_needed,
)
from visual_copilot.mission.verified_advance import (
    build_dom_signature as _build_dom_signature,
    verify_pending_action_effect as _verify_pending_action_effect,
    record_and_maybe_advance as _record_and_maybe_advance,
)
from visual_copilot.routing.tier3_router import run_tier3_fallback as _run_tier3_fallback
from visual_copilot.orchestration.completion import (
    check_if_arrived as _check_if_arrived,
    validate_and_end_mission as _validate_and_end_mission,
)
from visual_copilot.routing.read_only_router import run_read_only_terminal as _run_read_only_terminal

logger = logging.getLogger("vc.orchestration.plan_next_step")
session_stage_logger = logging.getLogger("vc.stage.session")
cross_domain_stage_logger = logging.getLogger("vc.stage.cross_domain")
mission_stage_logger = logging.getLogger("vc.stage.mission")
last_mile_stage_logger = logging.getLogger("vc.stage.last_mile")
router_pre_stage_logger = logging.getLogger("vc.stage.router_pre")
router_lex_stage_logger = logging.getLogger("vc.stage.router_lexical")
detective_stage_logger = logging.getLogger("vc.stage.detective")

from visual_copilot.orchestration.stages.session_stage import (
    build_excluded_ids,
    apply_frontend_amnesia_guard,
    set_pre_mission_context,
)
from visual_copilot.orchestration.stages.intent_stage import (
    resolve_schema_cached,
    parse_schema,
    cache_schema,
    normalize_schema_domain,
    compute_feature_flags,
)
from visual_copilot.orchestration.stages.hive_stage import (
    resolve_hive_response,
    compute_location_guard_candidate,
)
from visual_copilot.orchestration.stages.cross_domain_stage import run_cross_domain_gate
from visual_copilot.orchestration.stages.pre_router_stage import run_pre_router_gate
from visual_copilot.orchestration.stages.page_relevance_gate import run_page_relevance_gate
from visual_copilot.orchestration.stages.terminal_stage import handle_terminal_mission_state
from visual_copilot.orchestration.stages.router_stage import build_router_context
from visual_copilot.orchestration.stages.mission_stage import prepare_mission_and_query
from visual_copilot.orchestration.stages.last_mile_stage import handle_last_mile_stage
from visual_copilot.orchestration.stages.detective_stage import run_detective_stage
from visual_copilot.orchestration.stages.router_execution_stage import run_router_lexical_stage
from visual_copilot.orchestration.stages.router_pre_detective_stage import run_router_pre_detective_stage

async def ultimate_plan_next_step_impl(
    app,
    session_id: str,
    goal: str,
    current_url: str,
    step_number: int = 0,
    action_history: Optional[list] = None,
    previous_goal: Optional[str] = None,
    mission_id: Optional[str] = None,
    screenshot_b64: str = "",
) -> Dict[str, Any]:
    """
    Ultimate TARA pipeline for planning next step.
    
    Flow:
    1. Mind Reader → TacticalSchema
    2. Get Map Hints from existing endpoint (GPS navigation)
    3. Hive Interface → Strategy + Visual Hints
    4. Mission Brain → Create Mission + Sub-goals
    5. Live Graph → Get DOM nodes
    6. Semantic Detective → Score candidates
    7. Mission Brain → Audit action (constraint check)
    8. Return approved action
    """
    start_time = time.time()
    
    # Get Ultimate TARA modules from app state
    mind_reader = app.state.mind_reader
    hive_interface = app.state.hive_interface
    mission_brain = app.state.mission_brain
    semantic_detective = app.state.semantic_detective
    live_graph = app.state.live_graph
    
    if not all([mind_reader, hive_interface, mission_brain, semantic_detective, live_graph]):
        logger.warning("⚠️ Ultimate TARA modules not fully initialized, falling back to legacy")
        return None
    
    try:
        excluded_ids = build_excluded_ids(action_history)
        logger.info(f"📋 action_history: {len(action_history or [])} entries, {len(excluded_ids)} excluded click IDs")

        existing_mission, effective_step, early_response = await apply_frontend_amnesia_guard(
            mission_brain=mission_brain,
            session_id=session_id,
            mission_id=mission_id,
            step_number=step_number,
            current_url=current_url,
            is_last_mile_enabled_for_domain=_is_last_mile_enabled_for_domain,
            logger=session_stage_logger,
        )
        if early_response:
            return early_response

        set_pre_mission_context(
            app_state=app.state,
            session_id=session_id,
            goal=goal,
            current_url=current_url,
            effective_step=effective_step,
        )

        pre_router_decision = None
        skip_hive_prefetch = False
        if ENABLE_PRE_ROUTER_VISION:
            pre_router_response, pre_router_decision = await run_pre_router_gate(
                goal=goal,
                current_url=current_url,
                screenshot_b64=screenshot_b64 or "",
                session_id=session_id,
                logger=cross_domain_stage_logger,
            )
            if pre_router_response:
                return pre_router_response
            if (
                isinstance(pre_router_decision, dict)
                and pre_router_decision.get("route") == "current_domain_last_mile"
                and float(pre_router_decision.get("confidence") or 0.0) >= PRE_ROUTER_VISION_MIN_CONF
            ):
                skip_hive_prefetch = True
                logger.info(
                    f"PRE_ROUTER_VISION_GATE: skipping Hive prefetch "
                    f"(route=current_domain_last_mile conf={float(pre_router_decision.get('confidence') or 0.0):.2f})"
                )

        nodes_task = asyncio.create_task(live_graph.get_visible_nodes(session_id))
        logger.info("👁️ Step 0: Live Graph prefetch started...")

        schema, schema_cached = resolve_schema_cached(
            app_state=app.state,
            session_id=session_id,
            goal=goal,
            effective_step=effective_step,
        )
        if schema_cached:
            logger.info(f"🧠 Step 1: CACHED → {schema.action.value} on '{schema.target_entity}' (0 LLM calls)")
        else:
            prefetched_nodes = None
            try:
                prefetched_nodes = await nodes_task
                nodes = prefetched_nodes
                logger.info(f"   ✅ DOM nodes (prefetch): {len(nodes)}")
            except Exception as prefetch_err:
                logger.warning(f"DOM prefetch failed before intent parse, falling back: {prefetch_err}")

            schema = await parse_schema(
                mind_reader=mind_reader,
                live_graph=live_graph,
                session_id=session_id,
                goal=goal,
                current_url=current_url,
                previous_goal=previous_goal,
                prefetched_nodes=prefetched_nodes,
                logger=logger,
            )
            cache_schema(app_state=app.state, session_id=session_id, goal=goal, schema=schema)

        # Stage 0d: Page Relevance Gate — short-circuit cross-domain before Hive
        page_relevance_response, preserved_target_domain = run_page_relevance_gate(
            schema=schema, current_url=current_url, goal=goal, logger=logger,
        )
        if page_relevance_response:
            return page_relevance_response

        schema = normalize_schema_domain(schema=schema, current_url=current_url, logger=logger)

        (
            schema_domain,
            keyword_direct_v3_active,
            subgoal_hint_query_active,
            verified_advance_active,
            last_mile_enabled,
        ) = compute_feature_flags(
            schema=schema,
            current_url=current_url,
            is_v3_feature_enabled=_is_v3_feature_enabled,
            is_last_mile_enabled_for_domain=_is_last_mile_enabled_for_domain,
            enable_keyword_direct_v3=ENABLE_KEYWORD_DIRECT_V3,
            enable_subgoal_hint_query=ENABLE_SUBGOAL_HINT_QUERY,
            enable_verified_advance=ENABLE_VERIFIED_ADVANCE,
        )
        logger.info(
            f"V3_FEATURES domain={schema_domain or 'unknown'} "
            f"keyword_direct={keyword_direct_v3_active} "
            f"subgoal_hints={subgoal_hint_query_active} "
            f"verified_advance={verified_advance_active}"
        )

        hints = ""
        logger.debug("⏩ Step 2: GPS hints disabled (Hive visual_hints used instead)")

        if effective_step == 0:
            logger.info("🧠 Step 3: Hive Interface retrieving strategy...")
        hive_response, domain_known_for_hive = await resolve_hive_response(
            app_state=app.state,
            hive_interface=hive_interface,
            schema=schema,
            session_id=session_id,
            effective_step=effective_step,
            skip_hive_prefetch=skip_hive_prefetch,
        )
        if effective_step == 0:
            logger.info(f"   ✅ Strategy: {bool(hive_response.strategy)}, Hints: {len(hive_response.visual_hints)}")
            if not domain_known_for_hive:
                logger.info(
                    f"HIVE_BYPASS_UNKNOWN_DOMAIN domain={getattr(schema, 'domain', 'unknown')} "
                    "reason=domain_not_indexed -> zero_shot_local"
                )
        else:
            logger.debug("⏩ Step 3: Using cached Hive response")

        location_guard_candidate = compute_location_guard_candidate(
            effective_step=effective_step,
            action_history=action_history,
            logger=logger,
        )

        # Step 3b: Domain Jump Decision Gate (Co-Domain Hive Jump)
        hive_response, is_zero_shot, cross_domain_response = await run_cross_domain_gate(
            goal=goal,
            action_history=action_history,
            effective_step=effective_step,
            hive_response=hive_response,
            domain_known_for_hive=domain_known_for_hive,
            schema=schema,
            session_id=session_id,
            current_url=current_url,
            app_state=app.state,
            hive_interface=hive_interface,
            logger=cross_domain_stage_logger,
        )
        if cross_domain_response:
            return cross_domain_response

        # Step 4: Get DOM from Live Graph (Moved up for zero-shot!)
        # (Nodes are now fetched before Step 1 to feed the generic Mind Reader)
        if 'nodes' not in locals():
            try:
                nodes = await nodes_task
            except Exception as prefetch_err:
                logger.warning(f"DOM prefetch failed, refetching live graph: {prefetch_err}")
                nodes = await live_graph.get_visible_nodes(session_id)
            logger.info(f"   ✅ DOM nodes: {len(nodes)}")

        # 🛡️ THE PHANTOM FIRE FIX: Prevent acting on an empty page transition
        if len(nodes) == 0:
            logger.warning("   🚫 DOM is empty! Frontend fired prematurely during navigation. Forcing a wait.")
            return {
                "success": True,
                "action": {"type": "wait", "speech": "Waiting for the page to finish loading..."},
                "speech": "Waiting for the page to finish loading...",
                "pipeline": "ultimate_tara",
                "no_legacy_fallback": True,
            }
        current_dom_signature = _build_dom_signature(nodes)
        excluded_ids = _drop_reclick_safe_exclusions(excluded_ids, nodes)

        # ═══════════════════════════════════════════════════════════
        # 🎯 FAST DOM GOAL CHECK — Runs BEFORE mission creation!
        # If the goal is already visible on screen, skip everything.
        # No mission, no ReAct, no detective, no Tier 3.
        # ═══════════════════════════════════════════════════════════
        if is_zero_shot and schema.action in (
            ActionIntent.EXTRACTION, ActionIntent.SEARCH, ActionIntent.NAVIGATION
        ):
            # Count DISTINCT content elements containing ALL goal words.
            # A search dropdown showing "nicole aniston" once ≠ goal achieved.
            # We need 5+ separate elements with the goal text to confirm
            # the page actually has content about the goal (not just a mention).
            goal_lower = schema.target_entity.lower()
            _noise = {'the', 'and', 'for', 'then', 'show', 'find', 'get', 'see', 'with', 'from', 'this', 'that', 'videos', 'pics', 'images', 'photos'}
            goal_words = [w for w in goal_lower.split() if len(w) > 2 and w not in _noise]

            # Only check content elements (skip inputs, search boxes, nav items)
            content_nodes = [
                n for n in nodes
                if n.text and len(n.text) > 5
                and n.tag not in ('input', 'select', 'textarea')
                and n.zone != 'nav'
            ]

            # Count elements where ALL goal words appear
            matching_elements = [
                n for n in content_nodes
                if all(w in n.text.lower() for w in goal_words)
            ]
            elem_count = len(matching_elements)

            logger.info(f"🎯 DOM CHECK: '{schema.target_entity}' found in {elem_count} content elements (need 5+)")

            if elem_count >= 5:
                logger.info(
                    f"🎯 FAST DOM CHECK: Goal '{schema.target_entity}' confirmed in {elem_count} elements. "
                    f"Completing immediately."
                )
                mission_for_fast_check = existing_mission
                if not mission_for_fast_check:
                    mission_for_fast_check = await mission_brain._load_session_mission(session_id)
                if not mission_for_fast_check and mission_id:
                    mission_for_fast_check = await mission_brain._load_mission(mission_id)
                end_result = await _validate_and_end_mission(
                    schema, nodes, mission_for_fast_check, mission_brain, app, start_time
                )
                if end_result:
                    return end_result
                logger.info("🎯 Fast DOM Check: validator said not done, continuing...")

        mission, query, domain_name, excluded_ids, enter_last_mile, last_mile_reason, mission_stage_response = await prepare_mission_and_query(
            session_id=session_id,
            mission_brain=mission_brain,
            hive_response=hive_response,
            schema=schema,
            nodes=nodes,
            app=app,
            is_zero_shot=is_zero_shot,
            existing_mission=existing_mission,
            mission_id=mission_id,
            last_mile_enabled=last_mile_enabled,
            verified_advance_active=verified_advance_active,
            current_url=current_url,
            current_dom_signature=current_dom_signature,
            goal=goal,
            start_time=start_time,
            excluded_ids=excluded_ids,
            logger=mission_stage_logger,
            mission_progress_label_fn=_mission_progress_label,
            validate_and_end_mission_fn=_validate_and_end_mission,
            handle_terminal_mission_state_fn=handle_terminal_mission_state,
            drop_reclick_safe_exclusions_fn=_drop_reclick_safe_exclusions,
            verify_pending_action_effect_fn=_verify_pending_action_effect,
            subgoal_focus_visible_fn=_subgoal_focus_visible,
            register_v3_success_fn=_register_v3_success,
            register_v3_pending_drop_fn=_register_v3_pending_drop,
            classify_subgoal_mode_fn=_classify_subgoal_mode,
            should_enter_last_mile_fn=_should_enter_last_mile,
        )
        if mission_stage_response:
            return mission_stage_response

        mission, last_mile_response = await handle_last_mile_stage(
            last_mile_enabled=last_mile_enabled,
            enter_last_mile=enter_last_mile,
            last_mile_reason=last_mile_reason,
            mission=mission,
            schema=schema,
            goal=goal,
            nodes=nodes,
            excluded_ids=excluded_ids,
            app=app,
            mission_brain=mission_brain,
            logger=last_mile_stage_logger,
            max_attempts=LAST_MILE_MAX_ATTEMPTS,
            validate_last_mile_step_fn=_validate_last_mile_step,
            hash_last_mile_sequence_fn=_hash_last_mile_sequence,
            goal_completion_guard_fn=_goal_completion_guard,
            extract_model_usage_evidence_fn=_extract_model_usage_evidence,
            extract_visible_goal_evidence_fn=_extract_visible_goal_evidence,
            session_id=session_id,
            screenshot_b64=screenshot_b64,
        )
        if last_mile_response:
            return last_mile_response

        pre_router = await run_router_pre_detective_stage(
            app=app,
            session_id=session_id,
            goal=goal,
            current_url=current_url,
            start_time=start_time,
            mission=mission,
            schema=schema,
            nodes=nodes,
            mission_brain=mission_brain,
            semantic_detective=semantic_detective,
            live_graph=live_graph,
            hive_response=hive_response,
            hive_interface=hive_interface,
            query=query,
            domain_name=domain_name,
            excluded_ids=excluded_ids,
            is_zero_shot=is_zero_shot,
            verified_advance_active=verified_advance_active,
            current_dom_signature=current_dom_signature,
            location_guard_candidate=location_guard_candidate,
            keyword_direct_v3_active=keyword_direct_v3_active,
            subgoal_hint_query_active=subgoal_hint_query_active,
            tara_router_v2_enabled=TARA_ROUTER_V2_ENABLED,
            tara_router_v2_shadow=TARA_ROUTER_V2_SHADOW,
            logger=router_pre_stage_logger,
            build_router_context_fn=build_router_context,
            classify_subgoal_mode_fn=_classify_subgoal_mode,
            override_mode_for_labels_fn=_override_mode_for_labels,
            extract_label_candidates_fn=_extract_label_candidates,
            extract_explicit_target_id_fn=_extract_explicit_target_id,
            validate_action_target_fn=_validate_action_target,
            record_and_maybe_advance_fn=_record_and_maybe_advance,
            check_if_arrived_fn=_check_if_arrived,
            run_read_only_terminal_fn=_run_read_only_terminal,
            is_gallery_subgoal_fn=_is_gallery_subgoal,
            find_gallery_click_target_fn=_find_gallery_click_target,
            build_label_policy_fn=_build_label_policy,
            find_hard_keyword_match_fn=_find_hard_keyword_match,
            resolve_clickable_target_id_fn=_resolve_clickable_target_id,
            resolve_clickable_by_label_context_fn=_resolve_clickable_by_label_context,
            extract_type_text_fn=_extract_type_text,
            retarget_click_to_nav_duplicate_if_needed_fn=_retarget_click_to_nav_duplicate_if_needed,
            validate_and_end_mission_fn=_validate_and_end_mission,
            is_canary_domain_fn=_is_canary_domain,
            match_result_cls=MatchResult,
        )
        if pre_router.get("response"):
            return pre_router["response"]

        mission = pre_router["mission"]
        nodes = pre_router["nodes"]
        query = pre_router["query"]
        subgoal_mode = pre_router["subgoal_mode"]
        strategy_authoritative = pre_router["strategy_authoritative"]
        effective_hive_hints = pre_router["effective_hive_hints"]
        label_policy = pre_router["label_policy"]
        should_use_detective_v2 = pre_router["should_use_detective_v2"]
        bypass_reason_v2 = pre_router["bypass_reason_v2"]

        lexical_stage_response = await run_router_lexical_stage(
            app=app,
            goal=goal,
            query=query,
            schema=schema,
            mission=mission,
            mission_brain=mission_brain,
            nodes=nodes,
            excluded_ids=excluded_ids,
            label_policy=label_policy,
            subgoal_mode=subgoal_mode,
            strategy_authoritative=strategy_authoritative,
            is_zero_shot=is_zero_shot,
            domain_name=domain_name,
            current_url=current_url,
            current_dom_signature=current_dom_signature,
            verified_advance_active=verified_advance_active,
            start_time=start_time,
            should_use_detective_v2=should_use_detective_v2,
            bypass_reason_v2=bypass_reason_v2,
            router_v2_enabled=TARA_ROUTER_V2_ENABLED,
            router_v2_shadow=TARA_ROUTER_V2_SHADOW,
            lexical_direct_accept_type=LEXICAL_DIRECT_ACCEPT_TYPE,
            lexical_direct_accept_click=LEXICAL_DIRECT_ACCEPT_CLICK,
            logger=router_lex_stage_logger,
            lexical_ground_candidate_fn=_lexical_ground_candidate,
            is_canary_domain_fn=_is_canary_domain,
            node_matches_strategy_focus_fn=_node_matches_strategy_focus,
            record_and_maybe_advance_fn=_record_and_maybe_advance,
            extract_type_text_fn=_extract_type_text,
            run_tier3_fallback_fn=_run_tier3_fallback,
            find_best_type_target_fn=_find_best_type_target,
            validate_action_target_fn=_validate_action_target,
        )
        if lexical_stage_response:
            return lexical_stage_response

        detective_response = await run_detective_stage(
            app=app,
            semantic_detective=semantic_detective,
            mission_brain=mission_brain,
            mission=mission,
            schema=schema,
            goal=goal,
            query=query,
            session_id=session_id,
            nodes=nodes,
            excluded_ids=excluded_ids,
            effective_hive_hints=effective_hive_hints,
            subgoal_mode=subgoal_mode,
            strategy_authoritative=strategy_authoritative,
            label_policy=label_policy,
            domain_name=domain_name,
            is_zero_shot=is_zero_shot,
            current_url=current_url,
            current_dom_signature=current_dom_signature,
            verified_advance_active=verified_advance_active,
            start_time=start_time,
            hints=hints,
            logger=detective_stage_logger,
            type_tags=_TYPE_TAGS,
            detective_min_score=DETECTIVE_MIN_SCORE,
            detective_ambiguous_band=DETECTIVE_AMBIGUOUS_BAND,
            max_detective_retries_per_subgoal=MAX_DETECTIVE_RETRIES_PER_SUBGOAL,
            tara_router_v2_enabled=TARA_ROUTER_V2_ENABLED,
            node_matches_expected_labels_fn=_node_matches_expected_labels,
            action_tag_compatible_fn=_action_tag_compatible,
            node_matches_strategy_focus_fn=_node_matches_strategy_focus,
            candidate_signature_fn=_candidate_signature,
            canonicalize_label_fn=_canonicalize_label,
            run_tier3_fallback_fn=_run_tier3_fallback,
            record_and_maybe_advance_fn=_record_and_maybe_advance,
            extract_type_text_fn=_extract_type_text,
        )
        return detective_response
        
    except Exception as e:
        logger.error(f"❌ Ultimate TARA pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        return None

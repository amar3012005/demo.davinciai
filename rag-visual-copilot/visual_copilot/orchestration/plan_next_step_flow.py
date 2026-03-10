"""Extracted plan_next_step flow from legacy_core for modular orchestration."""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

from tara_models import ActionIntent
from visual_copilot.tara_models import TacticalSchema, StrategyHint, HiveResponse, VisualHint
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
    ENABLE_PRE_DECISION_GATE,
    PRE_DECISION_MIN_CONF,
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
from visual_copilot.orchestration.stages.pre_decision_stage import run_pre_decision_gate
from visual_copilot.orchestration.stages.page_relevance_gate import run_page_relevance_gate
from visual_copilot.orchestration.stages.terminal_stage import handle_terminal_mission_state
from visual_copilot.orchestration.stages.router_stage import build_router_context
from visual_copilot.orchestration.stages.mission_stage import prepare_mission_and_query
from visual_copilot.orchestration.stages.last_mile_stage import handle_last_mile_stage
from visual_copilot.orchestration.stages.detective_stage import run_detective_stage
from visual_copilot.orchestration.stages.router_execution_stage import run_router_lexical_stage
from visual_copilot.orchestration.stages.router_pre_detective_stage import run_router_pre_detective_stage

def _action_fingerprint(action: dict) -> str:
    import hashlib
    action_type = str(action.get("type", "") or "").strip().lower()
    raw = "|".join([
        action_type,
        str(action.get("target_id", "") or ""),
        str(action.get("text", "") or "")[:120],
        str(int(bool(action.get("press_enter", False)))),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

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
    pre_decision: Optional[Dict[str, Any]] = None,
    route_hint: str = "",
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

        # ═══════════════════════════════════════════════════════════════════
        # 🛡️ HISTORY RESTORATION: If frontend sent empty action_history but
        # Redis has a live mission with history, reconstruct excluded_ids
        # from the backend state. This prevents re-clicking after refresh.
        # ═══════════════════════════════════════════════════════════════════
        if (
            existing_mission
            and existing_mission.status in ("in_progress", "paused")
            and getattr(existing_mission, "action_history", None)
            and (not action_history or len(action_history) < len(existing_mission.action_history))
        ):
            backend_history = existing_mission.action_history
            logger.info(
                f"🛡️ HISTORY_RESTORE: Frontend sent {len(action_history or [])} entries, "
                f"Redis has {len(backend_history)}. Restoring from backend."
            )
            action_history = backend_history
            excluded_ids = build_excluded_ids(action_history)
            logger.info(f"📋 action_history (restored): {len(action_history)} entries, {len(excluded_ids)} excluded click IDs")

        set_pre_mission_context(
            app_state=app.state,
            session_id=session_id,
            goal=goal,
            current_url=current_url,
            effective_step=effective_step,
        )

        verified_advance_active = False # Initialized early to prevent UnboundLocalError during bundled nav
        prefetched_nodes_for_gate = None  # Initialize at function scope to prevent UnboundLocalError

        external_pre_decision = pre_decision if isinstance(pre_decision, dict) else None
        pre_decision = external_pre_decision
        skip_hive_prefetch = False

        # Prefetch LiveGraph + optional quick Hive probe in parallel
        nodes_task = asyncio.create_task(live_graph.get_visible_nodes(session_id))
        logger.info("👁️ Step 0: Live Graph prefetch started...")

        quick_hive_probe = None
        pre_decision_livegraph_ms = 0
        pre_decision_quick_hive_ms = 0
        pre_decision_total_ms = 0
        if pre_decision:
            logger.info(
                f"PRE_DECISION_EXTERNAL_PAYLOAD_USED session={session_id} "
                f"route={route_hint or pre_decision.get('route', '') or 'n/a'} "
                f"mode={pre_decision.get('execution_mode', 'unknown')} "
                f"seq_len={len(pre_decision.get('recommended_strategy_order') or [])}"
            )
        if ENABLE_PRE_DECISION_GATE and effective_step == 0 and not pre_decision:
            pre_decision_start = time.perf_counter()
            _domain_for_probe = ""
            try:
                from urllib.parse import urlparse as _urlparse
                try:
                    _parsed = _urlparse(current_url if "://" in current_url else f"http://{current_url}")
                    _domain_for_probe = (_parsed.netloc or "").replace("www.", "").lower()
                except Exception:
                    pass
            except Exception as qp_err:
                logger.warning(f"Quick Hive probe setup failed: {qp_err}")

            # Wait for nodes (and retry once if empty) before running pre-decision gate
            lg_start = time.perf_counter()
            try:
                prefetched_nodes_for_gate = await nodes_task
            except Exception:
                prefetched_nodes_for_gate = []
            pre_decision_livegraph_ms = int((time.perf_counter() - lg_start) * 1000)
            if not prefetched_nodes_for_gate:
                retry_start = time.perf_counter()
                await asyncio.sleep(0.2)
                try:
                    prefetched_nodes_for_gate = await live_graph.get_visible_nodes(session_id)
                except Exception:
                    prefetched_nodes_for_gate = []
                retry_ms = int((time.perf_counter() - retry_start) * 1000)
                pre_decision_livegraph_ms += retry_ms
                logger.info(
                    f"PRE_DECISION_GATE: livegraph retry "
                    f"nodes={len(prefetched_nodes_for_gate)} retry_ms={retry_ms}"
                )

            try:
                if not prefetched_nodes_for_gate:
                    logger.info(
                        f"PRE_DECISION_GATE_TIMING_PREP session={session_id} "
                        f"livegraph_ms={pre_decision_livegraph_ms} "
                        f"quick_hive_ms=0 nodes=0"
                    )
                    pre_decision = {
                        "execution_mode": "mission",
                        "route": "current_domain_hive",
                        "confidence": 0.60,
                        "reason": "LiveGraph had no nodes after retry; strategy-first fallback.",
                        "goal_evidence_visible": False,
                        "next_control_visible": False,
                        "page_unrelated": False,
                        "hive_strategy_exists": bool((quick_hive_probe or {}).get("strategy_exists")),
                        "hive_hint_score": float((quick_hive_probe or {}).get("hint_score", 0.0)),
                    }
                    pre_decision_response = None
                    logger.info(
                        "PRE_DECISION_GATE: skipped LLM due to empty LiveGraph "
                        "(fallback route=current_domain_hive conf=0.60)"
                    )
                    logger.warning(
                        "PRE_DECISION_GATE_BLOCK: live_graph empty after retry; "
                        "stopping pipeline before Mind Reader/Hive and returning wait_for_dom"
                    )
                    return {
                        "success": True,
                        "action": {"type": "wait", "speech": "Waiting for the page to finish loading..."},
                        "speech": "Waiting for the page to finish loading...",
                        "pipeline": "ultimate_tara",
                        "no_legacy_fallback": True,
                        "route_hint": "wait_for_dom",
                        "pre_decision": pre_decision,
                    }
                else:
                    qh_start = time.perf_counter()
                    try:
                        from visual_copilot.mission.index_traverser import get_path_to_goal
                        import json
                        import os
                        
                        site_map_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "site_map.json")
                        try:
                            with open(site_map_path, "r", encoding="utf-8") as f:
                                site_map_data = json.load(f)
                        except FileNotFoundError:
                            site_map_data = {}
                            
                        idx_result = get_path_to_goal(current_url, goal, site_map_data)
                        strategy_order = idx_result.get("recommended_strategy_order", [f"LAST_MILE: {goal}"])
                        
                        pre_decision = {
                            "execution_mode": "mission" if len(strategy_order) > 1 else "last_mile",
                            "route": "current_domain_hive" if len(strategy_order) > 1 else "current_domain_last_mile",
                            "confidence": 0.90,
                            "start_with_strategy": True,
                            "recommended_strategy_order": strategy_order,
                            "reason": "PageIndex LLM Tree Traversal",
                            "evidence": {
                                "visible_goal_signals": 1 if idx_result.get("target_node") else 0,
                                "obvious_controls": len(strategy_order) - 1,
                                "hive_support_score": 0.90,
                            },
                        }
                    except Exception as qp_err:
                        logger.warning(f"IndexTraverser quick probe failed: {qp_err}")
                        pre_decision = None
                    pre_decision_quick_hive_ms = int((time.perf_counter() - qh_start) * 1000)
                    logger.info(
                        f"PRE_DECISION_GATE_TIMING_PREP session={session_id} "
                        f"livegraph_ms={pre_decision_livegraph_ms} "
                        f"index_traverser_ms={pre_decision_quick_hive_ms} "
                        f"nodes={len(prefetched_nodes_for_gate)}"
                    )
                    pre_decision_response = None
                if pre_decision_response:
                    return pre_decision_response
                if (
                    isinstance(pre_decision, dict)
                    and pre_decision.get("route") == "current_domain_last_mile"
                    and float(pre_decision.get("confidence") or 0.0) >= PRE_DECISION_MIN_CONF
                ):
                    skip_hive_prefetch = True
                    logger.info(
                        f"PRE_DECISION_GATE: skipping Hive prefetch "
                        f"(route=current_domain_last_mile conf={float(pre_decision.get('confidence') or 0.0):.2f})"
                    )
            except Exception as pd_err:
                logger.warning(f"PRE_DECISION_GATE_FALLBACK: {pd_err}")
                pre_decision = {
                    "execution_mode": "last_mile",
                    "route": "current_domain_last_mile",
                    "confidence": 0.55,
                    "start_with_strategy": True,
                    "recommended_strategy_order": [f"LAST_MILE: {goal}"],
                    "reason": f"Pre-decision stage failure fallback ({type(pd_err).__name__})",
                    "evidence": {
                        "visible_goal_signals": 0,
                        "obvious_controls": 0,
                        "hive_support_score": 0.0,
                    },
                }
                logger.info(
                    "PRE_DECISION_GATE_FALLBACK: forcing strategy-first execution with synthetic "
                    "LAST_MILE sequence (no MindReader/Hive fallback)."
                )
            finally:
                pre_decision_total_ms = int((time.perf_counter() - pre_decision_start) * 1000)
                logger.info(
                    f"PRE_DECISION_GATE_TIMING_TOTAL session={session_id} "
                    f"total_ms={pre_decision_total_ms}"
                )

        # If pre-decision gate already fetched nodes, reuse them
        if ENABLE_PRE_DECISION_GATE and 'prefetched_nodes_for_gate' in locals() and prefetched_nodes_for_gate:
            nodes = prefetched_nodes_for_gate

        pre_decision_strategy_sequence = []
        pre_decision_last_mile_goal = ""
        nav_subgoals = []
        
        # Extract the sequence either from Step 0 pre-decision OR Step 1+ existing mission
        if (
            isinstance(pre_decision, dict)
            and pre_decision.get("execution_mode") in ("mission", "last_mile")
            and pre_decision.get("recommended_strategy_order")
            and isinstance(pre_decision.get("recommended_strategy_order"), list)
            and len(pre_decision["recommended_strategy_order"]) > 0
        ):
            pre_decision_strategy_sequence = pre_decision["recommended_strategy_order"]
        elif existing_mission and getattr(existing_mission, "subgoals", None):
            # Reconstruct the sequence from the active mission if it was fast-tracked (contains LAST_MILE:)
            subgoals = existing_mission.subgoals
            if any(s.strip().upper().startswith("LAST_MILE:") for s in subgoals):
                pre_decision_strategy_sequence = subgoals
            
        if pre_decision_strategy_sequence:
            last_mile_entries = [s for s in pre_decision_strategy_sequence if s.strip().upper().startswith("LAST_MILE:")]
            if last_mile_entries:
                pre_decision_last_mile_goal = last_mile_entries[-1].split(":", 1)[-1].strip()
            nav_subgoals = [s for s in pre_decision_strategy_sequence if not s.strip().upper().startswith("LAST_MILE:")]
            logger.info(
                f"⚡ PRE-DECISION STRATEGY: {len(nav_subgoals)} nav subgoals, "
                f"last_mile={'yes' if pre_decision_last_mile_goal else 'no'}"
            )
            if isinstance(pre_decision, dict):
                reasoning_log = pre_decision.get("reasoning") or pre_decision.get("reason")
                if reasoning_log:
                    logger.info(f"🧠 PRE-DECISION REASONING: {reasoning_log}")

        if pre_decision_strategy_sequence or pre_decision_last_mile_goal:
            logger.info("⚡ FAST-TRACK: Bypassing Mind Reader with Pre-Decision optimized strategy.")
            
            # Log the full PageIndex strategy sequence for visibility
            if pre_decision_strategy_sequence:
                logger.info(f"📋 PAGEINDEX STRATEGY SEQUENCE ({len(pre_decision_strategy_sequence)} steps):")
                for idx, step in enumerate(pre_decision_strategy_sequence, 1):
                    # Clean up double "LAST_MILE:" prefix if present
                    clean_step = step
                    if clean_step.upper().startswith("LAST_MILE: LAST_MILE:"):
                        clean_step = "LAST_MILE: " + clean_step.split("LAST_MILE:", 2)[-1].strip()
                        pre_decision_strategy_sequence[idx-1] = clean_step  # Fix in-place
                    step_type = "🎯 LAST_MILE" if clean_step.upper().startswith("LAST_MILE:") else "🔀 NAV"
                    logger.info(f"   {idx}. {step_type}: {clean_step}")
            
            domain_val = _domain_for_probe if '_domain_for_probe' in locals() else ""
            # If only LAST_MILE (no nav subgoals), use EXTRACTION action; otherwise NAVIGATION
            action_intent = ActionIntent.EXTRACTION if not nav_subgoals else ActionIntent.NAVIGATION
            schema = TacticalSchema(
                action=action_intent,
                target_entity=pre_decision_last_mile_goal or goal,
                domain=domain_val,
                constraints={},
                raw_utterance=goal
            )
            schema_cached = False
            if isinstance(pre_decision, dict):
                pre_decision_reasoning = pre_decision.get("reasoning", "") or pre_decision.get("reason", "")
                if pre_decision_reasoning:
                    schema.constraints["pre_decision_context"] = pre_decision_reasoning[:2000]  # Increased from 500 to preserve full navigation reasoning

            # ═══════════════════════════════════════════════════════════
            # ⚡ PAGEINDEX BUNDLED NAVIGATION: If PageIndex provides multiple
            #    nav subgoals, execute them as a BUNDLED PIPELINE for speed.
            #    This avoids round-trips between each click.
            #    CONSTRAINT: Only works if ALL targets are visible in current DOM.
            # ═══════════════════════════════════════════════════════════
            if (
                nav_subgoals
                and len(nav_subgoals) >= 1
                and effective_step == 0
                and isinstance(pre_decision, dict)
                and pre_decision.get("page_index")  # Only for PageIndex-derived strategies
            ):
                # Get nodes
                if 'nodes' not in locals() or not nodes:
                    try:
                        nodes = await nodes_task
                    except Exception:
                        nodes = await live_graph.get_visible_nodes(session_id)
                if not nodes:
                    logger.warning("PAGEINDEX_BUNDLED_NAV: No DOM nodes, returning wait")
                    return {
                        "success": True,
                        "action": {"type": "wait", "speech": "Waiting for the page to finish loading..."},
                        "speech": "Waiting for the page to finish loading...",
                        "pipeline": "ultimate_tara",
                        "no_legacy_fallback": True,
                    }

                # Build bundled action pipeline from nav_subgoals
                # Each "Click X" becomes a click_element action with target found via keyword match
                bundled_actions = []
                excluded_ids: set[str] = set()
                all_resolved = True

                for nav_step in nav_subgoals:
                    # Extract label from "Click [LABEL]" format
                    label = nav_step
                    if label.lower().startswith("click "):
                        label = label[6:].strip()

                    # Find matching clickable node by keyword/token overlap
                    best_id = None
                    best_score = 0
                    label_terms = set(label.lower().split())
                    
                    for n in nodes:
                        if not getattr(n, "interactive", False):
                            continue
                        nid = str(getattr(n, "id", ""))
                        if nid in excluded_ids:
                            continue
                        txt = (getattr(n, "text", "") or "").strip().lower()
                        if not txt:
                            continue
                        
                        # Score by token overlap
                        txt_terms = set(txt.split())
                        overlap = len(label_terms & txt_terms)
                        if overlap > best_score:
                            best_score = overlap
                            best_id = nid
                    
                    if best_id and best_score > 0:
                        bundled_actions.append({
                            "type": "click",
                            "target_id": best_id,
                            "text": label,
                            "speech": f"Clicking {label}...",
                        })
                        excluded_ids.add(best_id)
                        logger.info(f"  ✓ Mapped '{nav_step}' -> {best_id} (score={best_score})")
                    else:
                        logger.warning(f"  ✗ Could not resolve '{nav_step}' in current DOM")
                        all_resolved = False

                # Only use bundled navigation if ALL targets are visible
                # Otherwise fall back to regular subgoal execution
                if not all_resolved:
                    logger.info("PAGEINDEX_BUNDLED_NAV: Not all targets visible, falling back to regular subgoal execution")
                elif not bundled_actions:
                    logger.warning("PAGEINDEX_BUNDLED_NAV: No actions resolved, falling back to normal flow")
                else:
                    # Add wait action at the end to allow page to settle
                    bundled_actions.append({
                        "type": "wait",
                        "seconds": 2,
                        "speech": "Waiting for page to update after navigation...",
                    })

                    # Create mission for tracking
                    mission = await mission_brain.create_mission(
                        session_id=session_id,
                        schema=schema,
                        strategy=None,
                    )
                    mission.subgoals = nav_subgoals + [pre_decision_last_mile_goal] if pre_decision_last_mile_goal else nav_subgoals
                    mission.current_subgoal_index = 0
                    mission.phase = "strategy"
                    mission.status = "in_progress"
                    mission.main_goal = pre_decision_last_mile_goal or goal
                    await mission_brain._save_mission(mission)

                    # Persist bundled nav progress as if earlier clicks were already dispatched
                    # and stage the final navigation click for verified-advance reconciliation.
                    bundled_clicks = [a for a in bundled_actions if a.get("type") == "click" and a.get("target_id")]
                    if bundled_clicks:
                        prefinal_clicks = bundled_clicks[:-1]
                        final_click = bundled_clicks[-1]

                        # 1. Standardize history of pre-final clicks (optimistic)
                        for click in prefinal_clicks:
                            action_key = f"click:{click['target_id']}"
                            if action_key not in mission.action_history:
                                mission.action_history.append(action_key)

                        # Move index to the final nav goal in the bundle (so verifier knows what to check)
                        # Example: [Click Dashboard, Click Usage, LAST_MILE]
                        # Index becomes 1 (Click Usage).
                        mission.current_subgoal_index = min(
                            len(bundled_clicks) - 1, 
                            max(len(mission.subgoals or []) - 1, 0)
                        )
                        
                        await mission_brain._save_mission(mission)

                        # 2. Stage final click for verification logic in next turn
                        if verified_advance_active:
                            logger.info(f"🎯 PAGEINDEX_BUNDLED_NAV: Staging final click {final_click['target_id']} (subgoal {mission.current_subgoal_index}) for verification")
                            await _record_and_maybe_advance(
                                mission_brain=mission_brain,
                                mission=mission,
                                action_type="click",
                                target_id=final_click["target_id"],
                                nodes=nodes,
                                current_url=current_url,
                                dom_signature=_build_dom_signature(nodes),
                                verified_advance_active=True,
                                is_zero_shot=is_zero_shot,
                            )
                            # Reload to get the pending_action
                            mission = await mission_brain._load_mission(mission.mission_id) or mission
                        else:
                            # If no verified advance, optimistically move past the whole bundle
                            action_key = f"click:{final_click['target_id']}"
                            if action_key not in mission.action_history:
                                mission.action_history.append(action_key)
                            mission.current_subgoal_index = min(
                                len(bundled_clicks),
                                max(len(mission.subgoals or []) - 1, 0),
                            )
                            await mission_brain._save_mission(mission)

                    logger.info(
                        f"🎯 PAGEINDEX_BUNDLED_NAV: Returning {len(bundled_actions)}-step pipeline"
                    )
                    # Log the full bundled pipeline for debugging
                    logger.info(f"   📦 BUNDLED PIPELINE ({len(bundled_actions)} actions):")
                    for idx, action in enumerate(bundled_actions, 1):
                        action_type = action.get("type", "unknown")
                        target = action.get("target_id", "N/A")
                        text = action.get("text", "")
                        if action_type == "click":
                            logger.info(f"      {idx}. 🔘 CLICK {text} (id={target})")
                        elif action_type == "wait":
                            logger.info(f"      {idx}. ⏳ WAIT {action.get('seconds', 2)}s")
                        else:
                            logger.info(f"      {idx}. {action_type.upper()} (id={target})")
                    
                    return {
                        "success": True,
                        "blocked": False,
                        "action": bundled_actions,  # Full list - frontend sequences it
                        "speech": f"Executing {len(nav_subgoals)}-step navigation: {' -> '.join(nav_subgoals)}",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "pageindex_bundled_nav",
                        "confidence": "high",
                        "no_legacy_fallback": True,
                    }

            # ═══════════════════════════════════════════════════════════
            # ⚡ INSTANT LAST_MILE: If only LAST_MILE: (no nav subgoals),
            #    trigger compound last_mile IMMEDIATELY. Skip everything.
            # ═══════════════════════════════════════════════════════════
            if not nav_subgoals and pre_decision_last_mile_goal and effective_step == 0:
                logger.info(f"⚡ INSTANT LAST_MILE: No nav subgoals — triggering compound last_mile directly for '{pre_decision_last_mile_goal}'")
                # Get nodes
                if 'nodes' not in locals() or not nodes:
                    try:
                        nodes = await nodes_task
                    except Exception:
                        nodes = await live_graph.get_visible_nodes(session_id)
                if not nodes:
                    logger.warning("INSTANT_LAST_MILE: No DOM nodes, returning wait")
                    return {
                        "success": True,
                        "action": {"type": "wait", "speech": "Waiting for the page to finish loading..."},
                        "speech": "Waiting for the page to finish loading...",
                        "pipeline": "ultimate_tara",
                        "no_legacy_fallback": True,
                    }

                # Create a lightweight mission for compound last_mile
                mission = await mission_brain.create_mission(
                    session_id=session_id,
                    schema=schema,
                    strategy=None,
                )
                mission.subgoals = [pre_decision_last_mile_goal]
                mission.current_subgoal_index = 0
                mission.phase = "last_mile"
                mission.status = "in_progress"
                mission.main_goal = pre_decision_last_mile_goal
                await mission_brain._save_mission(mission)

                # Inject pre-decision reasoning as context into schema constraints
                pre_decision_reasoning = ""
                if isinstance(pre_decision, dict):
                    pre_decision_reasoning = pre_decision.get("reasoning", "") or pre_decision.get("reason", "")
                if pre_decision_reasoning:
                    schema.constraints["pre_decision_context"] = pre_decision_reasoning[:2000]  # Increased from 500 to preserve full navigation reasoning

                from visual_copilot.mission.last_mile import run_compound_last_mile
                try:
                    compound_result = await run_compound_last_mile(
                        schema=schema,
                        mission=mission,
                        nodes=nodes,
                        app=app,
                        session_id=session_id,
                        excluded_ids=excluded_ids or set(),
                    )
                except Exception as e:
                    logger.error(f"INSTANT_LAST_MILE_ERROR: {e}")
                    compound_result = None

                if compound_result:
                    action = compound_result.get("action") or {}
                    status = compound_result.get("status", "action")
                    diagnostics = compound_result.get("diagnostics", {})
                    lm_state = diagnostics.get("last_mile_state", {}) if diagnostics else {}
                    now_ms = int(time.time() * 1000)

                    # Initialize runtime to maintain state across turns
                    mission.last_mile_runtime = {
                        "subgoal_index": mission.current_subgoal_index,
                        "last_overlay_ids": lm_state.get("last_overlay_ids", []),
                        "last_action_dom_signature": _build_dom_signature(nodes),
                        "last_action_ts_ms": now_ms,
                        "vision_bootstrap_done": True,
                    }
                    
                    # ── Handle bundled action pipelines ──
                    if isinstance(action, list):
                        logger.info(
                            f"INSTANT_LAST_MILE: Bundled pipeline ({len(action)} actions) — passing through"
                        )
                        action_type = "action"
                        terminal_answer = None
                        for step in action:
                            if isinstance(step, dict) and step.get("type") not in ("", "wait"):
                                if not terminal_answer:
                                    action_type = step.get("type", "action")
                                if step.get("type") == "answer":
                                    terminal_answer = step
                                    break
                        
                        if status == "complete" or (terminal_answer and status == "complete"):
                            speech = (terminal_answer or {}).get("speech") or (terminal_answer or {}).get("text") or f"Here's what I found."
                            mission.phase = "done"
                            mission.status = "completed"
                            mission.last_mile_runtime = None
                            await mission_brain._save_mission(mission)
                            return {
                                "success": True, "blocked": False,
                                "action": action,
                                "speech": speech, "complete": True,
                                "mission_id": mission.mission_id,
                                "subgoal_index": mission.current_subgoal_index,
                                "pipeline": "instant_last_mile_bundled_terminal",
                                "confidence": 1.0,
                            }
                        
                        if status == "impossible":
                            speech = (terminal_answer or {}).get("speech") or f"I couldn't complete '{pre_decision_last_mile_goal}'."
                            mission.phase = "failed"
                            mission.status = "failed"
                            mission.last_mile_runtime = None
                            await mission_brain._save_mission(mission)
                            return {
                                "success": False, "blocked": True,
                                "action": action,
                                "speech": speech, "complete": True,
                                "reason": speech,
                                "mission_id": mission.mission_id,
                                "pipeline": "instant_last_mile_bundled_impossible",
                                "failure_type": "impossible",
                            }

                        # Normal action pipeline
                        rep_step = next((s for s in action if isinstance(s, dict) and s.get("type") not in ("", "wait", "scroll")), {})
                        if not rep_step and action:
                             rep_step = next((s for s in reversed(action) if isinstance(s, dict) and s.get("type") != "wait"), {})
                        
                        mission.last_mile_runtime.update({
                            "last_action_type": rep_step.get("type", "action") if rep_step else "action",
                            "last_action_fp": _action_fingerprint(rep_step) if rep_step else "",
                            "wait_until_ms": now_ms + 700,
                        })
                        await mission_brain._save_mission(mission)

                        return {
                            "success": True,
                            "blocked": False,
                            "action": action,
                            "speech": f"Executing {len(action)}-step sequence...",
                            "mission_id": mission.mission_id,
                            "subgoal_index": mission.current_subgoal_index,
                            "pipeline": "instant_last_mile_bundled",
                            "confidence": "high",
                        }
                    
                    action_type = action.get("type", "")

                    if not action_type:
                        action = {
                            "type": "clarify",
                            "speech": f"I need help finding information about {pre_decision_last_mile_goal}.",
                        }
                        action_type = "clarify"
                        status = "last_mile_no_evidence"

                    if status in ("stuck_no_progress", "last_mile_stuck", "last_mile_no_evidence", "last_mile_guardrail_blocked"):
                        speech = action.get("speech") or f"I couldn't complete '{pre_decision_last_mile_goal}'."
                        mission.status = "paused"
                        mission.last_mile_runtime = None
                        try:
                            if mission and mission_brain:
                                mission.action_history.append({"result": "last_mile_failed", "status": status})
                                await mission_brain._save_mission(mission)
                        except Exception as _save_err:
                            logger.warning(f"INSTANT_LAST_MILE: could not persist failure marker: {_save_err}")
                        return {
                            "success": False, "blocked": True,
                            "reason": speech,
                            "action": {"type": "clarify", "speech": speech},
                            "pipeline": "instant_last_mile",
                            "no_legacy_fallback": True,
                            "failure_type": status,
                        }

                    if status == "complete":
                        speech = action.get("speech") or action.get("text") or f"Here's what I found about {pre_decision_last_mile_goal}."
                        mission.phase = "done"
                        mission.status = "completed"
                        mission.last_mile_runtime = None
                        await mission_brain._save_mission(mission)
                        return {
                            "success": True, "blocked": False,
                            "action": {"type": "answer", "speech": speech, "text": speech},
                            "speech": speech, "complete": True,
                            "pipeline": "instant_last_mile", "confidence": 1.0,
                        }

                    if action_type in ("click", "select"):
                        target_id = action.get("target_id", "")
                        if target_id:
                            await mission_brain.record_action(mission.mission_id, "click", target_id, True)
                            refreshed = await mission_brain._load_mission(mission.mission_id)
                            if refreshed: mission = refreshed
                        
                        mission.last_mile_runtime.update({
                            "last_action_type": action_type,
                            "last_action_fp": _action_fingerprint(action),
                            "wait_until_ms": now_ms + 700,
                        })
                        await mission_brain._save_mission(mission)
                        return {
                            "success": True, "blocked": False,
                            "action": {"type": "click", "target_id": target_id, "text": action.get("text", "")},
                            "speech": action.get("speech", "Executing last-mile action."),
                            "mission_id": mission.mission_id,
                            "pipeline": "instant_last_mile", "confidence": "high",
                        }

                    if action_type == "type_text":
                        target_id = action.get("target_id", "")
                        text_to_type = action.get("text", "")
                        if target_id:
                            await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text_to_type)
                            refreshed = await mission_brain._load_mission(mission.mission_id)
                            if refreshed: mission = refreshed
                        
                        mission.last_mile_runtime.update({
                            "last_action_type": action_type,
                            "last_action_fp": _action_fingerprint(action),
                            "wait_until_ms": now_ms + (2000 if action.get("press_enter") else 700),
                        })
                        await mission_brain._save_mission(mission)
                        return {
                            "success": True, "blocked": False,
                            "action": {"type": "type_text", "target_id": target_id, "text": text_to_type, "press_enter": bool(action.get("press_enter", False))},
                            "speech": action.get("speech", "Typing..."),
                            "mission_id": mission.mission_id,
                            "pipeline": "instant_last_mile", "confidence": "high",
                        }

                    if action_type in ("scroll", "wait"):
                        mission.last_mile_runtime.update({
                            "last_action_type": action_type,
                            "last_action_fp": _action_fingerprint(action),
                            "wait_until_ms": now_ms + 700,
                        })
                        await mission_brain._save_mission(mission)
                        return {
                            "success": True, "blocked": False,
                            "action": {"type": action_type},
                            "speech": action.get("speech", "Processing..."),
                            "mission_id": mission.mission_id,
                            "pipeline": "instant_last_mile", "confidence": "medium",
                        }

                logger.warning("INSTANT_LAST_MILE: compound returned None, falling through to normal pipeline.")
        else:
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
        
        if pre_decision_strategy_sequence or pre_decision_last_mile_goal:
            subgoal_hint_query_active = False # Bypassing extra redundant Hive lookups on fast-tracked paths!
        logger.info(
            f"V3_FEATURES domain={schema_domain or 'unknown'} "
            f"keyword_direct={keyword_direct_v3_active} "
            f"subgoal_hints={subgoal_hint_query_active} "
            f"verified_advance={verified_advance_active}"
        )

        hints = ""
        logger.debug("⏩ Step 2: GPS hints disabled (Hive visual_hints used instead)")

        if pre_decision_strategy_sequence or pre_decision_last_mile_goal:
            logger.info("⚡ FAST-TRACK: Bypassing Hive Interface with Pre-Decision optimized strategy.")
            
            visual_hints = []
            if quick_hive_probe and "top_hints" in quick_hive_probe:
                for h in quick_hive_probe["top_hints"]:
                    visual_hints.append(VisualHint(
                        selector=h.get("selector", ""),
                        element_type=h.get("element_type", "link"),
                        zone=h.get("zone", "main"),
                        text_pattern=h.get("text_pattern"),
                        confidence=h.get("score", 0.0)
                    ))
            
            conf_val = float(pre_decision.get("confidence", 0.8) if isinstance(pre_decision, dict) else 0.8)
            hive_response = HiveResponse(
                strategy=StrategyHint(
                    sequence=pre_decision_strategy_sequence,
                    constraints_order=[],
                    blocking_rules={},
                    confidence=conf_val,
                    source_url=current_url
                ),
                visual_hints=visual_hints,
                cached=True,
                query_time_ms=0
            )
            hive_response.strategy_accepted = True  # CRITICAL: Ensures the mission stage locks the strategy
            domain_known_for_hive = quick_hive_probe.get("domain_indexed", True) if quick_hive_probe else True
            logger.info(f"   ✅ Fast-Tracked Strategy: True, Hints: {len(hive_response.visual_hints)}")
        else:
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

        # ⚡ PRE-DECISION LAST_MILE OVERRIDE: Force last_mile entry when:
        # 1. Pre-decision says "last_mile" with no navigation subgoals (goal already visible)
        # 2. The CURRENT subgoal query starts with "LAST_MILE:" (reached terminal step)
        # NOTE: The LAST_MILE: prefix check is UNCONDITIONAL — it must override even if
        # the domain is not in the canary list. The pre-decision gate already decided this
        # is a terminal goal; domain gating must not block it.
        should_force_last_mile = False
        if not enter_last_mile:
            # Check 1: LAST_MILE: prefix — always force (domain-independent)
            if query and str(query).strip().upper().startswith("LAST_MILE:"):
                should_force_last_mile = True
                logger.info(f"⚡ LAST_MILE PREFIX DETECTED in query — forcing last_mile regardless of domain canary")
            # Check 2: Pre-decision execution_mode — requires last_mile_enabled
            elif last_mile_enabled and isinstance(pre_decision, dict) and pre_decision.get("execution_mode") == "last_mile" and not nav_subgoals:
                should_force_last_mile = True
            # Check 3: Waterfall Fallacy fix — once in last_mile, NEVER leave!
            elif mission and getattr(mission, "phase", "") == "last_mile":
                should_force_last_mile = True
                logger.info(f"⚡ WATERFALL FALLACY GUARD: Mission already in last_mile phase. Forcing continuation.")

        if should_force_last_mile:
            logger.info(f"⚡ PRE-DECISION LAST_MILE OVERRIDE: Forcing direct last_mile entry. Goal: '{pre_decision_last_mile_goal or goal}'")
            enter_last_mile = True
            last_mile_enabled = True  # Force-enable last_mile for this request
            last_mile_reason = "pre_decision_last_mile_override"
            # Update the mission's main_goal to use the refined last_mile goal
            if pre_decision_last_mile_goal and mission:
                mission.main_goal = pre_decision_last_mile_goal

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
            current_url=current_url,
        )
        if last_mile_response:
            return last_mile_response

        # ═══════════════════════════════════════════════════════════════════
        # 🛡️ LAST_MILE FALLTHROUGH GUARD: Once a mission enters last_mile
        # phase, it must NEVER fall through to Detective/Tier3. If
        # handle_last_mile_stage returned None, force a scroll action to
        # let the compound agent retry on the next cycle.
        # ═══════════════════════════════════════════════════════════════════
        if mission and getattr(mission, "phase", "") == "last_mile":
            logger.warning(
                f"🛡️ LAST_MILE_FALLTHROUGH_GUARD: mission={mission.mission_id} "
                f"is in last_mile phase but handle_last_mile_stage returned None. "
                f"Blocking fallthrough to detective/tier3. Returning scroll to retry."
            )
            return {
                "success": True,
                "blocked": False,
                "action": {"type": "scroll", "speech": "Looking for more content on this page."},
                "speech": "Looking for more content on this page.",
                "mission_id": mission.mission_id,
                "pipeline": "ultimate_tara_last_mile_fallthrough_guard",
                "confidence": "medium",
                "no_legacy_fallback": True,
            }

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

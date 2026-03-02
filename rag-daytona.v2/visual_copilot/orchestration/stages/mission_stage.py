import time
from typing import Any, Dict, Optional, Set, Tuple


async def prepare_mission_and_query(
    *,
    session_id: str,
    mission_brain: Any,
    hive_response: Any,
    schema: Any,
    nodes: list,
    app: Any,
    is_zero_shot: bool,
    existing_mission: Optional[Any],
    mission_id: Optional[str],
    last_mile_enabled: bool,
    verified_advance_active: bool,
    current_url: str,
    current_dom_signature: str,
    goal: str,
    start_time: float,
    excluded_ids: Set[str],
    logger,
    mission_progress_label_fn,
    validate_and_end_mission_fn,
    handle_terminal_mission_state_fn,
    drop_reclick_safe_exclusions_fn,
    verify_pending_action_effect_fn,
    subgoal_focus_visible_fn,
    register_v3_success_fn,
    register_v3_pending_drop_fn,
    classify_subgoal_mode_fn,
    should_enter_last_mile_fn,
) -> Tuple[Optional[Any], Optional[str], Optional[str], Set[str], bool, str, Optional[Dict[str, Any]]]:
    def _is_question_like(text: str) -> bool:
        t = (text or "").strip().lower()
        return ("?" in t) or t.startswith(("what ", "which ", "how ", "why ", "where ", "when "))

    # Step 5: Get or resume mission (persists across requests)
    _effective_mission_id = (existing_mission.mission_id if existing_mission else None) or mission_id
    mission = await mission_brain.get_or_create_mission(
        session_id=session_id,
        schema=schema,
        strategy=hive_response.strategy,
    )

    if getattr(hive_response, "strategy_accepted", False) and not getattr(mission, "strategy_locked", False):
        mission.strategy_locked = True
        mission.strategy_source = "hive_sequence"
        await mission_brain._save_mission(mission)
        logger.info("PLAN_LOCK strategy_locked=true source=hive_sequence")

    subgoal_count = len(mission.subgoals or [])
    strategy_exhausted = mission.current_subgoal_index >= subgoal_count
    should_consider_replan = bool(getattr(schema, "zero_shot_mode", False) or is_zero_shot)
    should_skip_replan_for_last_mile = bool(
        last_mile_enabled and strategy_exhausted and subgoal_count > 0
    )
    should_zero_shot_replan = bool(
        should_consider_replan
        and (subgoal_count == 0 or strategy_exhausted)
        and not should_skip_replan_for_last_mile
    )

    if should_skip_replan_for_last_mile:
        logger.info(
            f"ZERO_SHOT_REPLAN_DEFER mission={mission.mission_id} reason=last_mile_handoff "
            f"subgoal_index={mission.current_subgoal_index} total={subgoal_count}"
        )

    if should_zero_shot_replan:
        new_subgoals = await mission_brain._react_generate_subgoal(schema, nodes, app, mission=mission)
        if not new_subgoals:
            return mission, None, None, excluded_ids, False, "", {
                "success": False,
                "blocked": True,
                "reason": f"I couldn't find a safe next step for '{schema.target_entity}' here on {schema.domain}.",
                "action": {
                    "type": "clarify",
                    "speech": f"I need a more specific page cue to continue toward '{schema.target_entity}'.",
                },
                "pipeline": "ultimate_tara",
                "mission_id": mission.mission_id if mission else None,
                "no_legacy_fallback": True,
            }

        if len(new_subgoals) == 1 and "extract and present" in new_subgoals[0].lower():
            logger.info("🏁 ReAct signals DONE. Validating mission completion...")
            end_result = await validate_and_end_mission_fn(
                schema, nodes, mission, mission_brain, app, start_time
            )
            if end_result:
                return mission, None, None, excluded_ids, False, "", end_result
            logger.info("🔄 Validator says NOT done yet. Continuing mission.")
            new_subgoals = [f"Navigate to {schema.target_entity}"]

        mission.subgoals = new_subgoals
        mission.current_subgoal_index = 0
        mission.status = "in_progress"
        await mission_brain._save_mission(mission)
        logger.info(f"🧭 Zero-Shot re-plan: new subgoal = '{new_subgoals[0]}'")

    logger.info(
        f"   ✅ Mission: {mission.mission_id}, Subgoal(indexed): "
        f"{mission_progress_label_fn(mission)}"
    )

    if is_zero_shot and not mission.subgoals:
        return mission, None, None, excluded_ids, False, "", {
            "success": False,
            "blocked": True,
            "reason": f"I couldn't find '{schema.target_entity}' here on {schema.domain}.",
            "action": {"type": "clarify", "speech": f"I couldn't find a grounded step for '{schema.target_entity}' yet."},
            "pipeline": "ultimate_tara",
            "mission_id": mission.mission_id if mission else None,
            "no_legacy_fallback": True,
        }

    mission, terminal_response = await handle_terminal_mission_state_fn(
        mission=mission,
        last_mile_enabled=last_mile_enabled,
        mission_brain=mission_brain,
        validate_and_end_mission=validate_and_end_mission_fn,
        schema=schema,
        nodes=nodes,
        app=app,
        start_time=start_time,
        logger=logger,
    )
    if terminal_response:
        return mission, None, None, excluded_ids, False, "", terminal_response

    for action_key in mission.action_history:
        if ":" in action_key:
            action_type_str, target = action_key.split(":", 1)
            if action_type_str.strip().lower() == "click":
                excluded_ids.add(target.strip())
    excluded_ids = drop_reclick_safe_exclusions_fn(excluded_ids, nodes)

    if verified_advance_active and getattr(mission, "pending_action", None):
        verified, verify_reason = verify_pending_action_effect_fn(
            mission,
            nodes,
            current_url,
            current_dom_signature,
        )
        pending_action_type = ((mission.pending_action or {}).get("type") or "").lower()
        if (
            verified
            and getattr(mission, "strategy_locked", False)
            and pending_action_type == "click"
            and mission.current_subgoal_index < len(mission.subgoals or [])
        ):
            pending_subgoal = mission.subgoals[mission.current_subgoal_index]
            if not subgoal_focus_visible_fn(pending_subgoal, nodes):
                verified = False
                verify_reason = "effect_without_subgoal_focus"

        logger.info(
            f"PENDING_ACTION_VERIFY success={verified} reason={verify_reason} "
            f"mission={mission.mission_id}"
        )
        if verified:
            await mission_brain.advance_subgoal_verified(
                mission.mission_id,
                is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                verification_reason=verify_reason,
            )
            mission = await mission_brain._load_mission(mission.mission_id) or mission
            register_v3_success_fn(getattr(schema, "domain", ""))
        else:
            if verify_reason == "effect_without_subgoal_focus":
                pending_target = (mission.pending_action or {}).get("target_id", "")
                if pending_target:
                    excluded_ids.add(pending_target)
                mission.pending_action = None
                mission.pending_verify_attempts = 0
                logger.warning(
                    f"PENDING_ACTION_DROP reason=effect_without_subgoal_focus mission={mission.mission_id} "
                    f"target={pending_target or 'none'}"
                )
                await mission_brain._save_mission(mission)
                logger.info(
                    f"   ✅ Mission(after-verify): {mission.mission_id}, Subgoal(indexed): "
                    f"{mission_progress_label_fn(mission)}"
                )
            else:
                mission.pending_verify_attempts = int(getattr(mission, "pending_verify_attempts", 0) or 0) + 1
                if mission.pending_verify_attempts >= 2:
                    pending_target = (mission.pending_action or {}).get("target_id", "")
                    if pending_target:
                        excluded_ids.add(pending_target)
                    mission.pending_action = None
                    mission.pending_verify_attempts = 0
                    logger.warning(
                        f"PENDING_ACTION_DROP reason=max_verify_attempts mission={mission.mission_id} "
                        f"target={pending_target or 'none'}"
                    )
                    register_v3_pending_drop_fn(getattr(schema, "domain", ""))
                await mission_brain._save_mission(mission)

        logger.info(
            f"   ✅ Mission(after-verify): {mission.mission_id}, Subgoal(indexed): "
            f"{mission_progress_label_fn(mission)}"
        )

    if mission and mission.status in ("completed", "paused"):
        if last_mile_enabled and getattr(mission, "phase", "strategy") != "done":
            logger.info(
                f"LAST_MILE_ENTER mission={mission.mission_id} "
                f"status={mission.status} phase={getattr(mission, 'phase', 'strategy')} "
                "reason=verified_advance_terminal_state"
            )
            mission.status = "in_progress"
            mission.phase = "last_mile"
            if not getattr(mission, "last_mile_started_at", None):
                mission.last_mile_started_at = time.time()
            await mission_brain._save_mission(mission)

    current_subgoal = (
        mission.subgoals[mission.current_subgoal_index]
        if mission.current_subgoal_index < len(mission.subgoals)
        else schema.target_entity
    )
    query = current_subgoal
    _subgoal_total = len(mission.subgoals or [])
    _subgoal_display_idx = (
        min(max(0, mission.current_subgoal_index), max(0, _subgoal_total - 1))
        if _subgoal_total > 0
        else mission.current_subgoal_index
    )
    logger.info(f"   🎯 Query (subgoal {_subgoal_display_idx}): '{query}'")
    domain_name = getattr(schema, "domain", "unknown")
    pre_subgoal_mode = classify_subgoal_mode_fn(query)
    total_subgoals_pre = len(mission.subgoals or [])
    on_final_strategy_subgoal_pre = (
        total_subgoals_pre > 0
        and mission.current_subgoal_index >= max(0, total_subgoals_pre - 1)
    )

    if (
        last_mile_enabled
        and pre_subgoal_mode == "cognitive_read"
        and on_final_strategy_subgoal_pre
        and getattr(mission, "phase", "strategy") != "last_mile"
        and not (
            str(getattr(getattr(schema, "action", None), "value", getattr(schema, "action", ""))).lower() in {"search", "purchase"}
            and not _is_question_like(getattr(schema, "raw_utterance", "") or goal or "")
        )
    ):
        logger.info(
            f"LAST_MILE_HANDOFF mission={mission.mission_id} "
            f"reason=final_strategy_read subgoal={min(mission.current_subgoal_index + 1, max(1, total_subgoals_pre))}/{total_subgoals_pre}"
        )
        mission.phase = "last_mile"
        mission.status = "in_progress"
        mission.main_goal = mission.main_goal or goal or schema.target_entity
        mission.current_subgoal_index = len(mission.subgoals or [])
        if not getattr(mission, "last_mile_started_at", None):
            mission.last_mile_started_at = time.time()
        await mission_brain._save_mission(mission)
        query = mission.main_goal or schema.target_entity

    enter_last_mile, last_mile_reason = should_enter_last_mile_fn(mission, query, schema)
    return mission, query, domain_name, excluded_ids, enter_last_mile, last_mile_reason, None

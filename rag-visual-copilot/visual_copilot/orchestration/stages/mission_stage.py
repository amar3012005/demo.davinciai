import time
from typing import Any, Dict, Optional, Set, Tuple

from visual_copilot.navigation.site_map_validator import SiteMapValidator, get_validator

# Site map validator instance (lazy-loaded)
_site_map_validator: Optional[SiteMapValidator] = None


def _get_validator() -> Optional[SiteMapValidator]:
    """Get or create the site map validator instance."""
    global _site_map_validator
    if _site_map_validator is None:
        try:
            _site_map_validator = get_validator()
        except Exception as e:
            logger.warning(f"Failed to initialize SiteMapValidator: {e}")
            return None
    return _site_map_validator


def _validate_click_target(
    current_url: str,
    click_target: str,
    nodes: list
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Validate that a click target is an expected control on the current page.

    Args:
        current_url: Current page URL
        click_target: The target being clicked (e.g., "Usage")
        nodes: List of DOM nodes

    Returns:
        Tuple of (is_valid, message, expected_outcome)
    """
    validator = _get_validator()
    if not validator:
        return True, "validator_unavailable", None

    # Get current node
    current_node = validator.get_node_for_url(current_url)
    if not current_node:
        return True, "url_not_in_site_map", None

    # Get expected controls for this node
    expected_controls = current_node.get("expected_controls", []) or []

    # Check if click target matches any expected control
    click_lower = click_target.lower().strip()
    for control in expected_controls:
        if click_lower in control.lower() or control.lower() in click_lower:
            # Valid control - get expected navigation outcome
            outcome = validator.get_expected_navigation_outcome(
                current_node.get("node_id", ""),
                click_target
            )
            return True, f"valid_control_{control}", outcome.expected_node if outcome else None

    # Check if it's a required control
    required_controls = current_node.get("required_controls", []) or []
    for control in required_controls:
        if click_lower in control.lower() or control.lower() in click_lower:
            return True, f"required_control_{control}", None

    # Not an expected control - provide warning
    return False, (
        f"'{click_target}' is not an expected control on '{current_node.get('title')}'. "
        f"Expected: {', '.join(expected_controls[:5])}"
    ), None


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
                    # ── Optimistic Advance ────────────────────────────────────────────────────
                    # The click was already dispatched to the browser. The verifier could not
                    # detect a DOM signature change (common with flyout/hover menus, lazy-
                    # rendered dropdowns like "Pics", "Tags", etc.).
                    # Treat the action as done and advance so we don't spin on the same
                    # subgoal with a stream of different DOM node IDs forever.
                    # 
                    # CRITICAL: For navigation subgoals (e.g., "Click Usage"), do NOT
                    # optimistically advance. Navigation MUST be verified by URL change
                    # to prevent "ghost cursor" issues where the click doesn't actually
                    # navigate but the mission advances anyway.
                    # ─────────────────────────────────────────────────────────────────────────
                    subgoal_count = len(mission.subgoals or [])
                    current_subgoal_text = (
                        mission.subgoals[mission.current_subgoal_index].lower()
                        if mission.current_subgoal_index < subgoal_count
                        else ""
                    )
                    is_navigation = current_subgoal_text.startswith("click ")
                    
                    if is_navigation:
                        # For navigation subgoals, require successful verification
                        # Do NOT optimistically advance - the navigation must actually happen
                        logger.warning(
                            f"PENDING_ACTION_DROP_NAVIGATION mission={mission.mission_id} "
                            f"subgoal={mission.current_subgoal_index}/{subgoal_count} "
                            f"reason=navigation_requires_verification "
                            f"subgoal='{current_subgoal_text}'"
                        )
                        # Keep the mission at the same subgoal - don't advance
                        # The next iteration will try a different approach
                    elif mission.current_subgoal_index < subgoal_count:
                        logger.warning(
                            f"PENDING_ACTION_DROP_ADVANCE mission={mission.mission_id} "
                            f"subgoal={mission.current_subgoal_index}/{subgoal_count} "
                            f"reason=optimistic_advance_after_max_retries"
                        )
                        await mission_brain.advance_subgoal_verified(
                            mission.mission_id,
                            is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                            verification_reason="optimistic_advance_dom_invisible_effect",
                        )
                        mission = await mission_brain._load_mission(mission.mission_id) or mission
                    else:
                        await mission_brain._save_mission(mission)
                else:
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

    # ── SITE MAP VALIDATION FOR NAVIGATION SUBGOALS ─────────────────────────
    # Before executing a "Click X" subgoal, validate that X is an expected control
    subgoal_lower = (current_subgoal or "").lower().strip()
    if subgoal_lower.startswith("click "):
        click_target = subgoal_lower[6:].strip()
        is_valid, validation_msg, expected_node = _validate_click_target(
            current_url, click_target, nodes
        )

        if is_valid:
            if expected_node:
                logger.info(
                    f"SITE_MAP_CLICK_VALID: '{click_target}' is valid on current page. "
                    f"Expected outcome: '{expected_node.get('title', '?')}' "
                    f"({expected_node.get('node_id', '?')})"
                )
                # Store expected node for post-navigation validation
                mission.expected_navigation_target = expected_node.get("node_id", "")
                await mission_brain._save_mission(mission)
            else:
                logger.info(f"SITE_MAP_CLICK_VALID: '{click_target}' - {validation_msg}")
        else:
            # Click target not recognized as expected control
            logger.warning(f"SITE_MAP_CLICK_WARNING: {validation_msg}")

            # Check if we might be on the wrong page
            validator = _get_validator()
            if validator:
                current_node = validator.get_node_for_url(current_url)
                if current_node:
                    # Suggest backtracking if we have a parent
                    parent = validator.get_parent_node(current_node.get("node_id", ""))
                    if parent:
                        logger.info(
                            f"SITE_MAP_RECOVERY_SUGGESTION: Backtrack to parent "
                            f"'{parent.get('title', '?')}' ({parent.get('node_id', '?')})"
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

    enter_last_mile, last_mile_reason = should_enter_last_mile_fn(mission, query, schema, is_zero_shot=is_zero_shot)
    return mission, query, domain_name, excluded_ids, enter_last_mile, last_mile_reason, None

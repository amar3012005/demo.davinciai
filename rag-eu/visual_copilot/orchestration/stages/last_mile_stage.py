from typing import Any, Dict, Optional, Set, Tuple

from visual_copilot.mission.last_mile import plan_last_mile as modular_plan_last_mile


def _last_mile_dom_signature(nodes: list) -> str:
    # Stable lightweight signature for scroll/yield progress checks.
    parts = []
    for n in nodes[:400]:
        parts.append(
            "|".join(
                [
                    str(getattr(n, "id", "") or ""),
                    str(getattr(n, "tag", "") or ""),
                    str(getattr(n, "zone", "") or ""),
                    str((getattr(n, "text", "") or "")[:80]),
                ]
            )
        )
    return "||".join(parts)


def _has_loading_signals(nodes: list) -> bool:
    markers = {"loading", "please wait", "fetching", "updating", "in progress", "spinner", "skeleton"}
    for n in nodes:
        txt = (getattr(n, "text", "") or "").strip().lower()
        if not txt:
            continue
        if any(m in txt for m in markers):
            return True
    return False


async def handle_last_mile_stage(
    *,
    last_mile_enabled: bool,
    enter_last_mile: bool,
    last_mile_reason: str,
    mission: Any,
    schema: Any,
    goal: str,
    nodes: list,
    excluded_ids: Set[str],
    app: Any,
    mission_brain: Any,
    logger,
    max_attempts: int,
    validate_last_mile_step_fn,
    hash_last_mile_sequence_fn,
    goal_completion_guard_fn,
    extract_model_usage_evidence_fn,
    extract_visible_goal_evidence_fn,
) -> Tuple[Any, Optional[Dict[str, Any]]]:
    if last_mile_enabled and enter_last_mile:
        logger.info(
            f"LAST_MILE_ENTER mission={mission.mission_id} "
            f"status={mission.status} phase={getattr(mission, 'phase', 'strategy')} "
            f"reason={last_mile_reason}"
        )
        mission.phase = "last_mile"
        mission.status = "in_progress"
        mission.main_goal = mission.main_goal or goal or schema.target_entity
        main_goal = mission.main_goal or goal or schema.target_entity
        attempts = int(getattr(mission, "last_mile_attempts", 0) or 0)
        current_dom_sig = _last_mile_dom_signature(nodes)
        previous_dom_sig = getattr(mission, "last_dom_signature", "") or ""
        dom_stagnant = bool(attempts > 0 and previous_dom_sig and previous_dom_sig == current_dom_sig)
        loading_signals = _has_loading_signals(nodes)

        model_usage_evidence = extract_model_usage_evidence_fn(main_goal, nodes)
        visible_goal_evidence = extract_visible_goal_evidence_fn(main_goal, nodes)
        location_guard_ok = bool(goal_completion_guard_fn(main_goal, nodes))
        location_ack = bool(location_guard_ok or model_usage_evidence or visible_goal_evidence)
        logger.info(
            f"LAST_MILE_LOCATION_ACK mission={mission.mission_id} ack={location_ack} "
            f"guard={location_guard_ok} model_evidence={bool(model_usage_evidence)} "
            f"visible_evidence={bool(visible_goal_evidence)} attempts={attempts} "
            f"dom_stagnant={dom_stagnant} loading={loading_signals}"
        )
        mission.last_dom_signature = current_dom_sig

        queue = list(getattr(mission, "last_mile_queue", []) or [])
        while queue:
            step = queue.pop(0)
            ok, reason = validate_last_mile_step_fn(step, nodes, excluded_ids)
            if not ok:
                logger.warning(f"LAST_MILE_STEP_REJECT reason={reason} step={step}")
                continue
            mission.last_mile_queue = queue
            await mission_brain._save_mission(mission)
            action_type = step.get("action")
            target_id = step.get("target_id", "")
            logger.info(f"LAST_MILE_STEP_EXEC action={action_type} id={target_id}")

            if action_type == "answer":
                speech = step.get("text") or step.get("why") or f"I've completed {schema.target_entity}."
                mission.phase = "done"
                mission.status = "completed"
                await mission_brain._save_mission(mission)
                logger.info("LAST_MILE_EXIT done=True reason=queue_answer")
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "answer", "speech": speech, "text": speech},
                    "speech": speech,
                    "complete": True,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": 1.0,
                }

            if action_type in {"click", "select"}:
                await mission_brain.record_action(mission.mission_id, "click", target_id, True)
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "click", "target_id": target_id, "text": step.get("text", "")},
                    "speech": f"Executing last-mile step: {step.get('why') or 'clicking'}",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "high",
                }

            if action_type == "type_text":
                text_to_type = step.get("text") or schema.target_entity
                await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text_to_type)
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "type_text",
                        "target_id": target_id,
                        "text": text_to_type,
                        "press_enter": bool(step.get("press_enter", False)),
                    },
                    "speech": f"Executing last-mile step: typing '{text_to_type}'.",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "high",
                }

            if action_type == "scroll":
                if dom_stagnant:
                    logger.warning("LAST_MILE_SCROLL_REJECT reason=dom_stagnant")
                    break
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "scroll"},
                    "speech": "Executing last-mile step: scrolling.",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "medium",
                }
            if action_type == "wait":
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "wait"},
                    "speech": "Waiting for the page data to finish loading.",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "medium",
                }

        if (
            attempts >= 1
            and location_ack
            and not queue
            and (model_usage_evidence or visible_goal_evidence)
        ):
            speech = model_usage_evidence or visible_goal_evidence
            mission.phase = "done"
            mission.status = "completed"
            await mission_brain._save_mission(mission)
            logger.info("LAST_MILE_EXIT done=True reason=location_ack_evidence_shortcut")
            return mission, {
                "success": True,
                "blocked": False,
                "action": {"type": "answer", "speech": speech, "text": speech},
                "speech": speech,
                "complete": True,
                "pipeline": "ultimate_tara_last_mile",
                "confidence": 0.9,
            }

        if attempts >= max_attempts:
            logger.warning(f"LAST_MILE_EXIT done=False reason=max_attempts attempts={attempts}")
            mission.status = "paused"
            await mission_brain._save_mission(mission)
            return mission, {
                "success": False,
                "blocked": True,
                "reason": f"I reached the target area but couldn't safely complete the final steps for '{mission.main_goal}'.",
                "action": {
                    "type": "clarify",
                    "speech": f"I reached the right area but need guidance to finish '{mission.main_goal}'. Should I continue with a broader search on this page?",
                },
                "pipeline": "ultimate_tara_last_mile",
                "no_legacy_fallback": True,
            }

        if loading_signals and not location_ack:
            mission.last_mile_attempts = attempts + 1
            await mission_brain._save_mission(mission)
            logger.info("LAST_MILE_WAIT reason=loading_signals_detected")
            return mission, {
                "success": True,
                "blocked": False,
                "action": {"type": "wait"},
                "speech": "I can see the page is still loading. Waiting briefly for usage data.",
                "mission_id": mission.mission_id,
                "subgoal_index": mission.current_subgoal_index,
                "pipeline": "ultimate_tara_last_mile",
                "confidence": "medium",
                "no_legacy_fallback": True,
            }

        plan = await modular_plan_last_mile(schema=schema, mission=mission, nodes=nodes, app=app)
        mission.last_mile_attempts = attempts + 1
        mission.last_mile_last_plan_hash = hash_last_mile_sequence_fn(plan.final_sequence)
        logger.info(
            f"LAST_MILE_PLAN steps={len(plan.final_sequence)} done={plan.is_done} "
            f"impossible={plan.is_impossible} attempts={mission.last_mile_attempts} "
            f"hash={mission.last_mile_last_plan_hash}"
        )

        if plan.is_done and (
            goal_completion_guard_fn(mission.main_goal or schema.target_entity, nodes)
            or plan.completion_answer
        ):
            speech = plan.completion_answer or f"I've completed the task for {schema.target_entity}."
            mission.phase = "done"
            mission.status = "completed"
            await mission_brain._save_mission(mission)
            logger.info("LAST_MILE_EXIT done=True reason=planner_done")
            return mission, {
                "success": True,
                "blocked": False,
                "action": {"type": "answer", "speech": speech, "text": speech},
                "speech": speech,
                "complete": True,
                "pipeline": "ultimate_tara_last_mile",
                "confidence": 1.0,
            }

        if plan.is_impossible:
            interactive_count = len([n for n in nodes if getattr(n, "interactive", False)])
            if dom_stagnant:
                mission.status = "paused"
                await mission_brain._save_mission(mission)
                logger.info("LAST_MILE_EXIT done=False reason=dom_stagnant_after_attempts")
                return mission, {
                    "success": False,
                    "blocked": True,
                    "reason": f"I could not find new evidence for '{mission.main_goal}' after repeated checks.",
                    "action": {
                        "type": "clarify",
                        "speech": f"I reached the likely page for '{mission.main_goal}', but the visible data did not update. Do you want me to retry or check a different section?",
                    },
                    "pipeline": "ultimate_tara_last_mile",
                    "no_legacy_fallback": True,
                }
            if interactive_count > 0:
                logger.info(
                    "LAST_MILE_IMPOSSIBLE_OVERRIDE reason=interactive_nodes_available "
                    f"count={interactive_count} action=scroll"
                )
                mission.status = "in_progress"
                await mission_brain._save_mission(mission)
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "scroll"},
                    "speech": "I still have actionable elements here. Scrolling to reveal more relevant results.",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "medium",
                    "no_legacy_fallback": True,
                }
            mission.status = "paused"
            await mission_brain._save_mission(mission)
            logger.info("LAST_MILE_EXIT done=False reason=planner_impossible")
            return mission, {
                "success": False,
                "blocked": True,
                "reason": f"I reached the right area but couldn't complete '{mission.main_goal}'.",
                "action": {
                    "type": "clarify",
                    "speech": f"I can't complete '{mission.main_goal}' from the current page state. Do you want me to try a different path?",
                },
                "pipeline": "ultimate_tara_last_mile",
                "no_legacy_fallback": True,
            }

        valid_steps = []
        for step in plan.final_sequence:
            ok, reason = validate_last_mile_step_fn(step, nodes, excluded_ids)
            if ok:
                valid_steps.append(step)
            else:
                logger.warning(f"LAST_MILE_STEP_REJECT reason={reason} step={step}")

        mission.last_mile_queue = valid_steps
        await mission_brain._save_mission(mission)
        if valid_steps:
            step = valid_steps[0]
            remaining = valid_steps[1:]
            mission.last_mile_queue = remaining
            await mission_brain._save_mission(mission)
            action_type = step.get("action")
            target_id = step.get("target_id", "")
            logger.info(f"LAST_MILE_STEP_EXEC action={action_type} id={target_id} source=fresh_plan")

            if action_type == "answer":
                speech = step.get("text") or step.get("why") or f"I've completed {schema.target_entity}."
                mission.phase = "done"
                mission.status = "completed"
                await mission_brain._save_mission(mission)
                logger.info("LAST_MILE_EXIT done=True reason=fresh_plan_answer")
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "answer", "speech": speech, "text": speech},
                    "speech": speech,
                    "complete": True,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": 1.0,
                }

            if action_type in {"click", "select"}:
                await mission_brain.record_action(mission.mission_id, "click", target_id, True)
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "click", "target_id": target_id, "text": step.get("text", "")},
                    "speech": f"Executing last-mile step: {step.get('why') or 'clicking'}",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "high",
                }

            if action_type == "type_text":
                text_to_type = step.get("text") or schema.target_entity
                await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text_to_type)
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "type_text",
                        "target_id": target_id,
                        "text": text_to_type,
                        "press_enter": bool(step.get("press_enter", False)),
                    },
                    "speech": f"Executing last-mile step: typing '{text_to_type}'.",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "high",
                }

            if action_type == "scroll":
                if dom_stagnant:
                    logger.warning("LAST_MILE_SCROLL_REJECT reason=dom_stagnant_fresh_plan")
                    mission.status = "paused"
                    await mission_brain._save_mission(mission)
                    return mission, {
                        "success": False,
                        "blocked": True,
                        "reason": f"I could not find new evidence for '{mission.main_goal}' after repeated checks.",
                        "action": {
                            "type": "clarify",
                            "speech": f"I reached the likely page for '{mission.main_goal}', but nothing new appeared after scrolling. Should I try a different section?",
                        },
                        "pipeline": "ultimate_tara_last_mile",
                        "no_legacy_fallback": True,
                    }
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "scroll"},
                    "speech": "Executing last-mile step: scrolling.",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "medium",
                }
            if action_type == "wait":
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "wait"},
                    "speech": "Waiting briefly for the dashboard data to populate.",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "medium",
                }
        else:
            evidence = extract_model_usage_evidence_fn(mission.main_goal or schema.target_entity, nodes)
            model_usage_evidence = bool(evidence)
            if not evidence:
                evidence = extract_visible_goal_evidence_fn(mission.main_goal or schema.target_entity, nodes)
            if evidence and (
                model_usage_evidence
                or goal_completion_guard_fn(mission.main_goal or schema.target_entity, nodes)
            ):
                speech = evidence
                mission.phase = "done"
                mission.status = "completed"
                await mission_brain._save_mission(mission)
                logger.info(
                    f"LAST_MILE_EXIT done=True reason={'model_usage_evidence_fallback' if model_usage_evidence else 'visible_evidence_fallback'}"
                )
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "answer", "speech": speech, "text": speech},
                    "speech": speech,
                    "complete": True,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": 0.8,
                }

            logger.info("LAST_MILE_FALLBACK action=scroll reason=no_grounded_plan_steps")
            if dom_stagnant:
                mission.status = "paused"
                await mission_brain._save_mission(mission)
                return mission, {
                    "success": False,
                    "blocked": True,
                    "reason": f"I reached the right area but no new evidence appeared for '{mission.main_goal}'.",
                    "action": {
                        "type": "clarify",
                        "speech": f"I can’t find new data for '{mission.main_goal}' on this view. Should I try another tab or date range?",
                    },
                    "pipeline": "ultimate_tara_last_mile",
                    "no_legacy_fallback": True,
                }
            mission.status = "in_progress"
            await mission_brain._save_mission(mission)
            return mission, {
                "success": True,
                "blocked": False,
                "action": {"type": "scroll"},
                "speech": "I couldn't safely ground a click, so I'm scrolling to reveal more relevant usage details.",
                "mission_id": mission.mission_id,
                "subgoal_index": mission.current_subgoal_index,
                "pipeline": "ultimate_tara_last_mile",
                "confidence": "medium",
                "no_legacy_fallback": True,
            }

    if last_mile_enabled:
        logger.info(
            f"LAST_MILE_DEFER mission={mission.mission_id} "
            f"phase={getattr(mission, 'phase', 'strategy')} reason={last_mile_reason}"
        )

    return mission, None

import hashlib
import os
import time
from typing import Any, Dict, Optional, Set, Tuple

from visual_copilot.mission.last_mile import plan_last_mile as modular_plan_last_mile
from visual_copilot.mission.last_mile import run_compound_last_mile
from visual_copilot.models.contracts import MappedTerminalContext

LAST_MILE_WAIT_GRACE_MS = int(os.getenv("LAST_MILE_WAIT_GRACE_MS", "1800"))
LAST_MILE_DEDUP_WINDOW_MS = int(os.getenv("LAST_MILE_DEDUP_WINDOW_MS", "6000"))
LAST_MILE_DEDUP_MAX_HITS = int(os.getenv("LAST_MILE_DEDUP_MAX_HITS", "2"))


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
                    str(getattr(n, "value", "") or ""),
                    str(getattr(n, "placeholder", "") or ""),
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


def _action_fingerprint(action: Dict[str, Any]) -> str:
    action_type = str(action.get("type", "") or "").strip().lower()
    key = {
        "type": action_type,
        "target_id": str(action.get("target_id", "") or ""),
        "text": str(action.get("text", "") or ""),
        "press_enter": bool(action.get("press_enter", False)),
    }
    raw = "|".join(
        [
            key["type"],
            key["target_id"],
            key["text"][:120],
            str(int(key["press_enter"])),
        ]
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


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
    session_id: str = "",
    screenshot_b64: str = "",
    current_url: str = "",
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
        now_ms = int(time.time() * 1000)
        previous_dom_sig = getattr(mission, "last_dom_signature", "") or ""
        dom_stagnant = bool(attempts > 0 and previous_dom_sig and previous_dom_sig == current_dom_sig)
        loading_signals = _has_loading_signals(nodes)
        runtime = dict(getattr(mission, "last_mile_runtime", {}) or {})

        # If subgoal changed since last-mile runtime was recorded, reset runtime state.
        runtime_subgoal_index = int(runtime.get("subgoal_index", -1) or -1)
        if runtime_subgoal_index != int(getattr(mission, "current_subgoal_index", 0) or 0):
            runtime = {}

        wait_until_ms = int(runtime.get("wait_until_ms", 0) or 0)
        if wait_until_ms > now_ms:
            remaining_ms = max(200, min(wait_until_ms - now_ms, 2000))
            logger.info(
                f"LAST_MILE_RUNTIME_WAIT_GATE mission={mission.mission_id} "
                f"remaining_ms={remaining_ms}"
            )
            mission.last_mile_runtime = runtime
            await mission_brain._save_mission(mission)
            return mission, {
                "success": True,
                "blocked": False,
                "action": {"type": "wait", "wait_ms": remaining_ms},
                "speech": "Waiting for the previous last-mile action to settle.",
                "mission_id": mission.mission_id,
                "subgoal_index": mission.current_subgoal_index,
                "pipeline": "ultimate_tara_compound_last_mile",
                "confidence": "medium",
                "no_legacy_fallback": True,
            }

        last_action_dom_sig = str(runtime.get("last_action_dom_signature", "") or "")
        last_action_ts_ms = int(runtime.get("last_action_ts_ms", 0) or 0)
        last_action_type = str(runtime.get("last_action_type", "") or "")
        dedupe_hits = int(runtime.get("dedupe_hits", 0) or 0)
        same_dom_recent = (
            bool(last_action_dom_sig)
            and last_action_dom_sig == current_dom_sig
            and (now_ms - last_action_ts_ms) <= LAST_MILE_DEDUP_WINDOW_MS
        )
        if (
            same_dom_recent
            and last_action_type in {"type_text", "click", "select"}
            and dedupe_hits < LAST_MILE_DEDUP_MAX_HITS
        ):
            dedupe_hits += 1
            runtime["dedupe_hits"] = dedupe_hits
            runtime["wait_until_ms"] = now_ms + LAST_MILE_WAIT_GRACE_MS
            runtime["subgoal_index"] = int(getattr(mission, "current_subgoal_index", 0) or 0)
            mission.last_mile_runtime = runtime
            await mission_brain._save_mission(mission)
            logger.info(
                f"LAST_MILE_RUNTIME_DEDUP_GATE mission={mission.mission_id} "
                f"action_type={last_action_type} dedupe_hits={dedupe_hits}"
            )
            return mission, {
                "success": True,
                "blocked": False,
                "action": {"type": "wait", "wait_ms": LAST_MILE_WAIT_GRACE_MS},
                "speech": "Waiting for page updates before repeating the same action.",
                "mission_id": mission.mission_id,
                "subgoal_index": mission.current_subgoal_index,
                "pipeline": "ultimate_tara_compound_last_mile",
                "confidence": "medium",
                "no_legacy_fallback": True,
            }
        if not same_dom_recent and runtime.get("dedupe_hits"):
            runtime["dedupe_hits"] = 0

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

        # ═══════════════════════════════════════════════════════════
        # ⚡ COMPOUND AGENTIC LAST MILE — Primary Path
        # ═══════════════════════════════════════════════════════════
        logger.info(
            f"COMPOUND_LAST_MILE_START mission={mission.mission_id} "
            f"attempts={attempts} dom_stagnant={dom_stagnant}"
        )
        force_vision_bootstrap = not bool(runtime.get("vision_bootstrap_done", False))
        if force_vision_bootstrap:
            runtime["vision_bootstrap_done"] = True
            runtime["subgoal_index"] = int(getattr(mission, "current_subgoal_index", 0) or 0)
            mission.last_mile_runtime = runtime
            await mission_brain._save_mission(mission)
            logger.info(
                f"LAST_MILE_VISION_BOOTSTRAP_POLICY mission={mission.mission_id} "
                "mode=one_time_on_last_mile_entry force=True"
            )
        else:
            logger.info(
                f"LAST_MILE_VISION_BOOTSTRAP_POLICY mission={mission.mission_id} "
                "mode=one_time_on_last_mile_entry force=False"
            )

        try:
            # ═══════════════════════════════════════════════════════════
            # MapGuard: Extract mapped_terminal_context from schema constraints
            # ═══════════════════════════════════════════════════════════
            mapped_terminal_ctx = None
            constraints = getattr(schema, "constraints", {}) or {}
            if constraints and "mapped_terminal_context" in constraints:
                ctx_dict = constraints.get("mapped_terminal_context")
                if ctx_dict:
                    mapped_terminal_ctx = MappedTerminalContext.from_dict(ctx_dict)
                    logger.info(
                        f"MAPGUARD_LAST_MILE_STAGE: Loaded mapped_terminal_context "
                        f"node={mapped_terminal_ctx.expected_node_id} "
                        f"valid={mapped_terminal_ctx.is_valid_terminal()}"
                    )

            compound_result = await run_compound_last_mile(
                schema=schema,
                mission=mission,
                nodes=nodes,
                app=app,
                session_id=session_id,
                screenshot_b64=screenshot_b64,
                excluded_ids=excluded_ids,
                current_url=current_url,
                goal_url=str(getattr(mission, "last_url", "") or ""),
                user_goal=goal,
                force_vision_bootstrap=force_vision_bootstrap,
                initial_overlay_ids=set(runtime.get("last_overlay_ids", [])),
                mapped_terminal_context=mapped_terminal_ctx,
            )
        except Exception as e:
            logger.error(f"COMPOUND_LAST_MILE_ERROR: {e}, falling back to legacy planner")
            compound_result = None

        if compound_result:
            raw_action = compound_result.get("action") or {}
            compound_status = compound_result.get("status", "action")
            compound_thought = compound_result.get("thought", "")
            compound_iters = compound_result.get("iterations", 0)
            compound_diagnostics = compound_result.get("diagnostics", {})

            # ── Multi-Action Bundling: action may be a list of steps ──
            # When it's a list, pass it through as-is to the frontend (the WS
            # handler in tara-ws.js executes the array sequentially).
            # For backend routing decisions, derive action_type from the last
            # non-wait item so the click/scroll/wait branches below work correctly.
            if isinstance(raw_action, list):
                compound_action = raw_action  # keeps the list for passthrough
                # Derive routing type from the terminal step in the pipeline
                action_type = "action"  # default
                for step in reversed(raw_action):
                    if isinstance(step, dict):
                        t = step.get("type", "")
                        if t and t != "wait":
                            action_type = t
                            break
                if not action_type or action_type == "action":
                    # All steps are waits — treat as a wait pipeline
                    action_type = "wait"
                logger.info(
                    f"COMPOUND_LAST_MILE_BUNDLED_PIPELINE steps={len(raw_action)} "
                    f"routing_type={action_type}"
                )
            else:
                compound_action = raw_action
                action_type = compound_action.get("type", "") if isinstance(compound_action, dict) else ""

            # ── Null-safety: ensure action is always valid ──
            if not action_type:
                logger.warning(
                    f"COMPOUND_LAST_MILE_NULL_ACTION status={compound_status} "
                    f"fixing null action envelope"
                )
                # Provide meaningful fallback based on status
                if compound_status in ("stuck_no_progress", "last_mile_stuck"):
                    compound_action = {
                        "type": "answer",
                        "speech": f"I found some information about '{mission.main_goal}' but may not be complete. Here's what I found: [See page content for details]",
                        "text": f"Information about {mission.main_goal}",
                    }
                elif compound_status in ("impossible", "max_iterations"):
                    compound_action = {
                        "type": "clarify",
                        "speech": f"I spent {compound_iters} steps searching for '{mission.main_goal}' but couldn't find a complete answer. Would you like me to try a different approach?",
                    }
                else:
                    compound_action = {
                        "type": "clarify",
                        "speech": f"I encountered an issue while working on '{mission.main_goal}'. Let me try a different approach.",
                    }
                action_type = compound_action.get("type", "clarify")
                if compound_status not in ("complete",):
                    compound_status = "last_mile_no_evidence"

            logger.info(
                f"COMPOUND_LAST_MILE_RESULT status={compound_status} "
                f"action_type={action_type} iterations={compound_iters} "
                f"bundled={'yes' if isinstance(raw_action, list) else 'no'}"
            )
            if compound_diagnostics:
                lm_state = compound_diagnostics.get("last_mile_state", {})
                if lm_state:
                    logger.info(
                        f"LAST_MILE_PHASE phase={lm_state.get('phase')} "
                        f"evidence_hits={lm_state.get('evidence_hits')} "
                        f"miss_streak={lm_state.get('evidence_miss_streak')} "
                        f"stall={lm_state.get('stall_count')} "
                        f"exit={lm_state.get('exit_reason')}"
                    )
                    # Sync overlay state for next turn
                    runtime["last_overlay_ids"] = lm_state.get("last_overlay_ids", [])

            mission.last_mile_attempts = attempts + 1

            # ── Complete: The agent answered the goal ──
            if compound_status == "complete":
                # compound_action might be a dict or a list (bundled pipeline)
                if isinstance(compound_action, list):
                    # Use info from the terminal action in the pipeline
                    last_step = compound_action[-1] if compound_action else {}
                    speech = last_step.get("speech") or last_step.get("text") or f"I've completed {schema.target_entity}."
                else:
                    speech = compound_action.get("speech") or compound_action.get("text") or f"I've completed {schema.target_entity}."
                mission.phase = "done"
                mission.status = "completed"
                mission.last_mile_runtime = None
                await mission_brain._save_mission(mission)
                logger.info(f"COMPOUND_LAST_MILE_EXIT done=True iters={compound_iters}")
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "answer", "speech": speech, "text": speech},
                    "speech": speech,
                    "complete": True,
                    "pipeline": "ultimate_tara_compound_last_mile",
                    "confidence": 1.0,
                }

            # ── Stuck / No Progress: Deterministic hard-stop from state machine ──
            if compound_status in ("stuck_no_progress", "last_mile_stuck", "last_mile_no_evidence", "last_mile_guardrail_blocked"):
                if isinstance(compound_action, list):
                    last_step = compound_action[-1] if compound_action else {}
                    speech = last_step.get("speech") or f"I couldn't find the information for '{mission.main_goal}'."
                else:
                    speech = compound_action.get("speech") or f"I couldn't find the information for '{mission.main_goal}'."
                # Evidence rescue attempt
                evidence = extract_model_usage_evidence_fn(mission.main_goal or schema.target_entity, nodes)
                if not evidence:
                    evidence = extract_visible_goal_evidence_fn(mission.main_goal or schema.target_entity, nodes)
                if evidence:
                    mission.phase = "done"
                    mission.status = "completed"
                    mission.last_mile_runtime = None
                    await mission_brain._save_mission(mission)
                    logger.info(f"COMPOUND_LAST_MILE_EXIT done=True reason=evidence_rescue_from_{compound_status}")
                    return mission, {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "answer", "speech": evidence, "text": evidence},
                        "speech": evidence,
                        "complete": True,
                        "pipeline": "ultimate_tara_compound_last_mile",
                        "confidence": 0.75,
                        "diagnostics": compound_diagnostics,
                    }

                mission.status = "paused"
                mission.last_mile_runtime = None
                await mission_brain._save_mission(mission)
                logger.info(f"COMPOUND_LAST_MILE_EXIT done=False reason={compound_status}")
                return mission, {
                    "success": False,
                    "blocked": True,
                    "reason": speech,
                    "action": {"type": "clarify", "speech": speech},
                    "pipeline": "ultimate_tara_compound_last_mile",
                    "no_legacy_fallback": True,
                    "failure_type": compound_status,
                    "diagnostics": compound_diagnostics,
                }

            # ── Impossible / Max iterations: Agent is stuck ──
            if compound_status in ("impossible", "max_iterations"):
                if isinstance(compound_action, list):
                    last_step = compound_action[-1] if compound_action else {}
                    speech = last_step.get("speech") or f"I couldn't complete '{mission.main_goal}'."
                else:
                    speech = compound_action.get("speech") or f"I couldn't complete '{mission.main_goal}'."
                # Before giving up, check for visible evidence one last time
                evidence = extract_model_usage_evidence_fn(mission.main_goal or schema.target_entity, nodes)
                if not evidence:
                    evidence = extract_visible_goal_evidence_fn(mission.main_goal or schema.target_entity, nodes)
                if evidence:
                    mission.phase = "done"
                    mission.status = "completed"
                    mission.last_mile_runtime = None
                    await mission_brain._save_mission(mission)
                    logger.info(f"COMPOUND_LAST_MILE_EXIT done=True reason=evidence_rescue iters={compound_iters}")
                    return mission, {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "answer", "speech": evidence, "text": evidence},
                        "speech": evidence,
                        "complete": True,
                        "pipeline": "ultimate_tara_compound_last_mile",
                        "confidence": 0.8,
                    }

                mission.status = "paused"
                mission.last_mile_runtime = None
                await mission_brain._save_mission(mission)
                logger.info(f"COMPOUND_LAST_MILE_EXIT done=False reason={compound_status} iters={compound_iters}")
                return mission, {
                    "success": False,
                    "blocked": True,
                    "reason": speech,
                    "action": {"type": "clarify", "speech": speech},
                    "pipeline": "ultimate_tara_compound_last_mile",
                    "no_legacy_fallback": True,
                }

            # ── Action: The agent wants to perform a browser action ──
            if compound_status == "action" and action_type:

                # ── Bundled Pipeline fast-path ──
                # When the backend returns an array of steps, pass them directly
                # to the frontend. The WS handler (tara-ws.js) executes the array
                # in sequence and sends back a single execution_complete.
                # We use the last non-wait step to populate mission runtime so the
                # dedup gate works correctly on the next turn.
                if isinstance(compound_action, list):
                    # Extract representative step for runtime tracking and success logging
                    rep_step = {}
                    for step in compound_action:
                        if isinstance(step, dict) and step.get("type", "") not in ("", "wait", "scroll"):
                            rep_step = step
                            break
                    if not rep_step:
                        for step in reversed(compound_action):
                            if isinstance(step, dict) and step.get("type", "") not in ("", "wait"):
                                rep_step = step
                                break
                    rep_type = rep_step.get("type", "action")
                    rep_target = rep_step.get("target_id", "")
                    rep_speech = rep_step.get("speech", "")

                    if rep_target:
                        await mission_brain.record_action(mission.mission_id, rep_type, rep_target, True)
                        refreshed = await mission_brain._load_mission(mission.mission_id)
                        if refreshed:
                            mission = refreshed
                    else:
                        await mission_brain._save_mission(mission)

                    # Incorporate wait time from bundled actions if present
                    pipe_wait_ms = 700
                    for step in compound_action:
                        if isinstance(step, dict) and step.get("type") == "wait":
                            s = float(step.get("seconds", 0) or 0)
                            w = int(step.get("wait_ms", 0) or 0)
                            if s > 0: pipe_wait_ms = max(pipe_wait_ms, int(s * 1000))
                            if w > 0: pipe_wait_ms = max(pipe_wait_ms, w)

                    mission.last_mile_runtime = {
                        **runtime,
                        "last_action_type": rep_type,
                        "last_action_fp": _action_fingerprint(rep_step) if rep_step else "",
                        "last_action_dom_signature": current_dom_sig,
                        "last_action_ts_ms": now_ms,
                        "wait_until_ms": now_ms + pipe_wait_ms,
                        "dedupe_hits": 0,
                        "subgoal_index": int(getattr(mission, "current_subgoal_index", 0) or 0),
                        "last_overlay_ids": runtime.get("last_overlay_ids", []),
                    }
                    await mission_brain._save_mission(mission)

                    logger.info(
                        f"COMPOUND_LAST_MILE_PIPELINE_PASSTHROUGH steps={len(compound_action)} "
                        f"rep_type={rep_type} rep_target={rep_target}"
                    )
                    return mission, {
                        "success": True,
                        "blocked": False,
                        "action": compound_action,   # full list — frontend sequences it
                        "speech": rep_speech or f"Executing {len(compound_action)}-step action pipeline.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_compound_last_mile",
                        "confidence": "high",
                    }

                # ── Single-action path (original logic) ──
                target_id = compound_action.get("target_id", "")
                speech = compound_action.get("speech", "")

                if action_type in {"click", "select"}:
                    if target_id:
                        await mission_brain.record_action(mission.mission_id, "click", target_id, True)
                        # Reload mission after record_action to avoid overwriting the updated action_history
                        refreshed = await mission_brain._load_mission(mission.mission_id)
                        if refreshed:
                            mission = refreshed
                    else:
                        await mission_brain._save_mission(mission)
                    mission.last_mile_runtime = {
                        **runtime,
                        "last_action_type": action_type,
                        "last_action_fp": _action_fingerprint(compound_action),
                        "last_action_dom_signature": current_dom_sig,
                        "last_action_ts_ms": now_ms,
                        "wait_until_ms": now_ms + 700,
                        "dedupe_hits": 0,
                        "subgoal_index": int(getattr(mission, "current_subgoal_index", 0) or 0),
                        "last_overlay_ids": runtime.get("last_overlay_ids", []),
                    }
                    await mission_brain._save_mission(mission)
                    return mission, {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "click", "target_id": target_id, "text": compound_action.get("text", "")},
                        "speech": speech or "Executing compound last-mile click.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_compound_last_mile",
                        "confidence": "high",
                    }

                if action_type == "type_text":
                    text_to_type = compound_action.get("text") or schema.target_entity
                    if target_id:
                        await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text_to_type)
                        refreshed = await mission_brain._load_mission(mission.mission_id)
                        if refreshed:
                            mission = refreshed
                    else:
                        await mission_brain._save_mission(mission)
                    wait_ms = 2000 if bool(compound_action.get("press_enter", False)) else 700
                    mission.last_mile_runtime = {
                        **runtime,
                        "last_action_type": action_type,
                        "last_action_fp": _action_fingerprint(
                            {
                                **compound_action,
                                "text": text_to_type,
                            }
                        ),
                        "last_action_dom_signature": current_dom_sig,
                        "last_action_ts_ms": now_ms,
                        "wait_until_ms": now_ms + wait_ms,
                        "dedupe_hits": 0,
                        "subgoal_index": int(getattr(mission, "current_subgoal_index", 0) or 0),
                        "last_overlay_ids": runtime.get("last_overlay_ids", []),
                    }
                    await mission_brain._save_mission(mission)
                    return mission, {
                        "success": True,
                        "blocked": False,
                        "action": {
                            "type": "type_text",
                            "target_id": target_id,
                            "text": text_to_type,
                            "press_enter": bool(compound_action.get("press_enter", False)),
                        },
                        "speech": speech or f"Typing '{text_to_type}'.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_compound_last_mile",
                        "confidence": "high",
                    }

                if action_type == "scroll":
                    if dom_stagnant:
                        logger.warning("COMPOUND_SCROLL_REJECT reason=dom_stagnant")
                        mission.status = "paused"
                        mission.last_mile_runtime = None
                        await mission_brain._save_mission(mission)
                        return mission, {
                            "success": False,
                            "blocked": True,
                            "reason": f"Page unchanged after scrolling for '{mission.main_goal}'.",
                            "action": {"type": "clarify", "speech": f"The page isn't changing. Should I try a different section?"},
                            "pipeline": "ultimate_tara_compound_last_mile",
                            "no_legacy_fallback": True,
                        }
                    mission.last_mile_runtime = {
                        **runtime,
                        "last_action_type": action_type,
                        "last_action_fp": _action_fingerprint(compound_action),
                        "last_action_dom_signature": current_dom_sig,
                        "last_action_ts_ms": now_ms,
                        "wait_until_ms": now_ms + 600,
                        "dedupe_hits": 0,
                        "subgoal_index": int(getattr(mission, "current_subgoal_index", 0) or 0),
                        "last_overlay_ids": runtime.get("last_overlay_ids", []),
                    }
                    await mission_brain._save_mission(mission)
                    return mission, {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "scroll"},
                        "speech": speech or "Scrolling to reveal more content.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_compound_last_mile",
                        "confidence": "medium",
                    }

                if action_type == "wait":
                    # Extract wait time (support both 'wait_ms' and 'seconds' from LLM)
                    requested_wait_ms = int(compound_action.get("wait_ms", 0) or 0)
                    if requested_wait_ms <= 0:
                        seconds = float(compound_action.get("seconds", 0) or 0)
                        if seconds > 0:
                            requested_wait_ms = int(seconds * 1000)
                    
                    if requested_wait_ms <= 0:
                        requested_wait_ms = LAST_MILE_WAIT_GRACE_MS
                    mission.last_mile_runtime = {
                        **runtime,
                        "last_action_type": action_type,
                        "last_action_fp": _action_fingerprint(compound_action),
                        "last_action_dom_signature": current_dom_sig,
                        "last_action_ts_ms": now_ms,
                        "wait_until_ms": now_ms + requested_wait_ms,
                        "dedupe_hits": 0,
                        "subgoal_index": int(getattr(mission, "current_subgoal_index", 0) or 0),
                        "last_overlay_ids": runtime.get("last_overlay_ids", []),
                    }
                    await mission_brain._save_mission(mission)
                    return mission, {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "wait", "wait_ms": requested_wait_ms},
                        "speech": speech or "Waiting for page data to load.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_compound_last_mile",
                        "confidence": "medium",
                    }

        # ── Compound result was None or unhandled — fall through to legacy ──
        logger.warning("COMPOUND_LAST_MILE_FALLTHROUGH → legacy plan_last_mile")

        plan = await modular_plan_last_mile(schema=schema, mission=mission, nodes=nodes, app=app)
        mission.last_mile_attempts = attempts + 1
        mission.last_mile_last_plan_hash = hash_last_mile_sequence_fn(plan.final_sequence)
        logger.info(
            f"LAST_MILE_PLAN_LEGACY steps={len(plan.final_sequence)} done={plan.is_done} "
            f"impossible={plan.is_impossible} attempts={mission.last_mile_attempts}"
        )

        if plan.is_done and (
            goal_completion_guard_fn(mission.main_goal or schema.target_entity, nodes)
            or plan.completion_answer
        ):
            speech = plan.completion_answer or f"I've completed the task for {schema.target_entity}."
            mission.phase = "done"
            mission.status = "completed"
            mission.last_mile_runtime = None
            await mission_brain._save_mission(mission)
            logger.info("LAST_MILE_EXIT done=True reason=legacy_planner_done")
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
            mission.status = "paused"
            mission.last_mile_runtime = None
            await mission_brain._save_mission(mission)
            logger.info("LAST_MILE_EXIT done=False reason=legacy_planner_impossible")
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

        if valid_steps:
            step = valid_steps[0]
            mission.last_mile_queue = valid_steps[1:]
            await mission_brain._save_mission(mission)
            action_type = step.get("action")
            target_id = step.get("target_id", "")

            if action_type in {"click", "select"}:
                await mission_brain.record_action(mission.mission_id, "click", target_id, True)
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "click", "target_id": target_id, "text": step.get("text", "")},
                    "speech": f"Legacy last-mile: {step.get('why') or 'clicking'}",
                    "mission_id": mission.mission_id,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "high",
                }
            if action_type == "type_text":
                text_to_type = step.get("text") or schema.target_entity
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "type_text", "target_id": target_id, "text": text_to_type, "press_enter": bool(step.get("press_enter", False))},
                    "speech": f"Legacy last-mile: typing '{text_to_type}'.",
                    "mission_id": mission.mission_id,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "high",
                }
            if action_type == "scroll":
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "scroll"},
                    "speech": "Legacy last-mile: scrolling.",
                    "mission_id": mission.mission_id,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "medium",
                }
            if action_type in {"answer", "wait"}:
                speech = step.get("text") or step.get("why") or ""
                return mission, {
                    "success": True,
                    "blocked": False,
                    "action": {"type": action_type, "speech": speech, "text": speech},
                    "speech": speech,
                    "mission_id": mission.mission_id,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "medium",
                }

        # No valid steps from legacy either — scroll or evidence fallback
        evidence = extract_model_usage_evidence_fn(mission.main_goal or schema.target_entity, nodes)
        if not evidence:
            evidence = extract_visible_goal_evidence_fn(mission.main_goal or schema.target_entity, nodes)
        if evidence:
            mission.phase = "done"
            mission.status = "completed"
            await mission_brain._save_mission(mission)
            return mission, {
                "success": True,
                "blocked": False,
                "action": {"type": "answer", "speech": evidence, "text": evidence},
                "speech": evidence,
                "complete": True,
                "pipeline": "ultimate_tara_last_mile",
                "confidence": 0.8,
            }

        if dom_stagnant:
            mission.status = "paused"
            await mission_brain._save_mission(mission)
            return mission, {
                "success": False,
                "blocked": True,
                "reason": f"No new evidence for '{mission.main_goal}'.",
                "action": {"type": "clarify", "speech": f"Should I try another tab or date range for '{mission.main_goal}'?"},
                "pipeline": "ultimate_tara_last_mile",
                "no_legacy_fallback": True,
            }

        mission.status = "in_progress"
        await mission_brain._save_mission(mission)
        return mission, {
            "success": True,
            "blocked": False,
            "action": {"type": "scroll"},
            "speech": "Scrolling to reveal more content.",
            "mission_id": mission.mission_id,
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

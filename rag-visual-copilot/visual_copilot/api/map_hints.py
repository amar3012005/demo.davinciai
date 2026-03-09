"""
visual_copilot/api/map_hints.py

Modular owner for /api/v1/get_map_hints behavior.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from visual_copilot.constants import (
    ENABLE_PRE_DECISION_GATE,
    PRE_DECISION_MIN_CONF,
)
from visual_copilot.orchestration.stages.page_relevance_gate import run_page_relevance_gate
from visual_copilot.orchestration.stages.pre_decision_stage import run_pre_decision_gate

logger = logging.getLogger("vc.api.map_hints")


def _norm_goal_text(text: Optional[str]) -> str:
    return " ".join(str(text or "").strip().lower().split())


async def build_map_hints(
    *,
    goal: str,
    client_id: str,
    current_url: str,
    screenshot_b64: Optional[str],
    mind_reader: Any,
    hive_interface: Any,
    mission_brain: Any = None,
    live_graph: Any = None,
    session_id: str = "",
) -> Dict[str, Any]:
    """
    Build map hints using modular Visual CoPilot components.
    """
    if not mind_reader or not hive_interface:
        logger.warning("get_map_hints: modular services unavailable (mind_reader/hive_interface)")
        return {"hints": ""}

    logger.info(f"Map Hints Request | Goal: '{goal}' | Client: {client_id}")

    try:
        # Mission re-entrancy guard for map-hints:
        # once a mission exists, reuse its subgoal sequence and do not recompute pre-route.
        if mission_brain and session_id:
            try:
                active_mission = await mission_brain._load_session_mission(session_id)
            except Exception as mission_load_err:
                active_mission = None
                logger.warning(
                    f"Map Hints mission-lock lookup failed: {mission_load_err}"
                )

            if active_mission and active_mission.status == "in_progress":
                # Reject stale mission-locks when last action was a hard failure.
                last_action = (
                    active_mission.action_history[-1]
                    if getattr(active_mission, "action_history", None)
                    else None
                )
                last_failed = (
                    isinstance(last_action, dict)
                    and last_action.get("result") in ("blocked", "non_success", "all_tiers_failed")
                )
                if last_failed:
                    logger.warning(
                        "Map Hints mission-lock invalidated: "
                        f"mission={active_mission.mission_id} reason=last_action_failed"
                    )
                    active_mission.status = "failed"
                    try:
                        await mission_brain._save_mission(active_mission)
                    except Exception as save_err:
                        logger.warning(
                            "Map Hints mission-lock invalidation save failed: "
                            f"mission={active_mission.mission_id} err={save_err}"
                        )
                    active_mission = None

            if active_mission and active_mission.status == "in_progress":
                # Mission-lock only when this looks like the same goal context.
                goal_norm = _norm_goal_text(goal)
                mission_goal_candidates = [
                    getattr(active_mission, "main_goal", "") or "",
                    getattr(getattr(active_mission, "schema", None), "raw_utterance", "") or "",
                    getattr(getattr(active_mission, "schema", None), "target_entity", "") or "",
                ]
                mission_goal_norms = {
                    _norm_goal_text(x) for x in mission_goal_candidates if _norm_goal_text(x)
                }
                if goal_norm and mission_goal_norms and goal_norm not in mission_goal_norms:
                    logger.info(
                        "Map Hints mission-lock bypass: "
                        f"mission={active_mission.mission_id} reason=goal_mismatch "
                        f"incoming='{goal_norm[:80]}' mission_goals={list(mission_goal_norms)[:3]}"
                    )
                    active_mission = None

            if active_mission and active_mission.status == "in_progress":
                subgoals = list(active_mission.subgoals or [])
                current_idx = int(active_mission.current_subgoal_index or 0)
                remaining = subgoals[current_idx:] if current_idx < len(subgoals) else []
                if not remaining:
                    logger.info(
                        "Map Hints mission-lock bypass: "
                        f"mission={active_mission.mission_id} reason=no_remaining_subgoals"
                    )
                    active_mission = None

            if active_mission and active_mission.status == "in_progress":
                subgoals = list(active_mission.subgoals or [])
                current_idx = int(active_mission.current_subgoal_index or 0)
                remaining = subgoals[current_idx:] if current_idx < len(subgoals) else []
                is_last_mile_next = bool(
                    remaining and str(remaining[0]).strip().upper().startswith("LAST_MILE:")
                )

                if is_last_mile_next:
                    # ── Terminal subgoal: lock directly to last_mile, skip Pre-Decision Gate ──
                    route_hint = "current_domain_last_mile"
                    pre_decision = {
                        "execution_mode": "last_mile",
                        "route": route_hint,
                        "confidence": 0.99,
                        "start_with_strategy": True,
                        "recommended_strategy_order": remaining,
                        "reason": f"Mission lock active: terminal LAST_MILE step ({active_mission.mission_id})",
                        "evidence": {
                            "visible_goal_signals": 0,
                            "obvious_controls": 0,
                            "hive_support_score": 0.0,
                        },
                    }
                    hints = "STRATEGY SEQUENCE: " + " -> ".join(remaining) if remaining else ""
                    logger.info(
                        "Map Hints mission-lock | "
                        f"mission={active_mission.mission_id} status={active_mission.status} "
                        f"idx={current_idx}/{len(subgoals)} remaining={len(remaining)} route={route_hint}"
                    )
                    return {
                        "hints": hints,
                        "pre_decision": pre_decision,
                        "route_hint": route_hint,
                        "mission_id": active_mission.mission_id,
                        "mission_locked": True,
                    }
                else:
                    # ── Non-terminal nav subgoal: let Pre-Decision Gate re-evaluate the page ──
                    # The agent is mid-mission on a nav step (e.g., "Click Usage").
                    # We must NOT short-circuit — the page changed and needs fresh DOM analysis.
                    logger.info(
                        "Map Hints mission-lock PASSTHROUGH | "
                        f"mission={active_mission.mission_id} "
                        f"idx={current_idx}/{len(subgoals)} "
                        f"next_subgoal='{remaining[0][:60] if remaining else 'none'}' "
                        "→ falling through to Pre-Decision Gate"
                    )
                    # Fall through to Pre-Decision Gate below (active_mission not returned)


        # ── Pre-Decision Gate (LiveGraph + QuickHive, replaces vision pre-router) ──
        pre_decision = None
        if ENABLE_PRE_DECISION_GATE and live_graph and session_id:
            try:
                # Extract domain for quick probe
                domain_for_probe = ""
                try:
                    parsed = urlparse(current_url if "://" in current_url else f"http://{current_url}")
                    domain_for_probe = (parsed.netloc or "").replace("www.", "").lower()
                except Exception:
                    pass

                # Step 1: LiveGraph first. If DOM is empty, return wait_for_dom immediately.
                live_graph_start = time.time()
                nodes = await live_graph.get_visible_nodes(session_id)
                live_graph_ms = int((time.time() - live_graph_start) * 1000)
                quick_hive_ms = 0

                # Gate on non-empty live graph: retry once before spending LLM call.
                if not nodes:
                    await asyncio.sleep(0.2)
                    retry_start = time.time()
                    nodes = await live_graph.get_visible_nodes(session_id)
                    live_graph_retry_ms = int((time.time() - retry_start) * 1000)
                    logger.info(
                        f"Map Hints pre-decision livegraph retry: nodes={len(nodes)} "
                        f"livegraph_ms={live_graph_ms} retry_ms={live_graph_retry_ms}"
                    )
                else:
                    logger.info(
                        f"Map Hints pre-decision timing: nodes={len(nodes)} "
                        f"livegraph_ms={live_graph_ms}"
                    )

                if not nodes:
                    logger.info("Map Hints pre-decision skipped: live_graph empty after retry")
                    pre_decision = {
                        "execution_mode": "mission",
                        "route": "current_domain_hive",
                        "confidence": 0.60,
                        "start_with_strategy": True,
                        "recommended_strategy_order": [],
                        "reason": "LiveGraph had no nodes; using strategy-first fallback route.",
                        "evidence": {
                            "visible_goal_signals": 0,
                            "obvious_controls": 0,
                            "hive_support_score": 0.0,
                        },
                    }
                    result: Dict[str, Any] = {
                        "hints": "ROUTE_HINT: wait_for_dom",
                        "pre_decision": pre_decision,
                        "route_hint": "wait_for_dom",
                    }
                    return result

                # Step 2: PageIndex Fast-Path (Vectorless Reasoning)
                # If the domain has an index, we can skip the expensive Hive probe entirely.
                from visual_copilot.orchestration.stages.pre_decision_stage import _page_index_available

                if _page_index_available(domain_for_probe):
                    logger.info(f"Map Hints: PageIndex AVAILABLE for {domain_for_probe}, skipping Hive probe.")
                    # We can call run_pre_decision_gate with an empty hive_probe because PageIndex doesn't need it.
                    _, pre_decision = await run_pre_decision_gate(
                        goal=goal,
                        current_url=current_url or "",
                        nodes=nodes or [],
                        hive_probe={}, # Empty hive probe is fine for PageIndex
                        session_id=session_id or "map_hints",
                    )
                    
                    if pre_decision and pre_decision.get("confidence", 0.0) >= 0.5:
                        # Success!
                        result: Dict[str, Any] = {
                            "hints": "STRATEGY SEQUENCE: " + " -> ".join(pre_decision.get("recommended_strategy_order") or []),
                            "pre_decision": pre_decision,
                            "route_hint": pre_decision.get("route", "current_domain_hive"),
                        }
                        return result
                    
                    logger.warning("Map Hints: PageIndex found no match, falling back to Hive probe.")

                # Step 3: Quick Hive probe ONLY if PageIndex was not available or failed.
                quick_hive_start = time.time()
                hive_probe = await hive_interface.quick_probe(
                    domain=domain_for_probe,
                    goal=goal,
                    session_id=session_id,
                )
                logger.info(
                    "Map Hints quick_hive_probe | "
                    f"domain={domain_for_probe or 'unknown'} "
                    f"matched={hive_probe.get('matched_domain', '') or 'none'} "
                    f"indexed={bool(hive_probe.get('domain_indexed'))} "
                    f"strategy={bool(hive_probe.get('has_strategy'))} "
                    f"hints={len(hive_probe.get('top_hints') or [])} "
                    f"conf={float(hive_probe.get('probe_confidence') or 0.0):.2f}"
                )
                quick_hive_ms = int((time.time() - quick_hive_start) * 1000)
                logger.info(
                    f"Map Hints pre-decision timing: nodes={len(nodes)} "
                    f"livegraph_ms={live_graph_ms} quick_hive_ms={quick_hive_ms}"
                )

                _, pre_decision = await run_pre_decision_gate(
                    goal=goal,
                    current_url=current_url or "",
                    nodes=nodes or [],
                    hive_probe=hive_probe or {},
                    session_id=session_id or "map_hints",
                    logger=logger,
                )
                if (
                    isinstance(pre_decision, dict)
                    and pre_decision.get("execution_mode") in ("mission", "last_mile")
                    and pre_decision.get("recommended_strategy_order")
                    and isinstance(pre_decision.get("recommended_strategy_order"), list)
                    and len(pre_decision["recommended_strategy_order"]) > 0
                ):
                    route_hint = pre_decision.get("route", "")
                    hints = "STRATEGY SEQUENCE: " + " -> ".join(pre_decision["recommended_strategy_order"])

                    # Inject quick-hive Visual Hints into the fast-track payload when available.
                    if hive_probe and hive_probe.get("top_hints"):
                        rows = []
                        for h in hive_probe["top_hints"][:8]:
                            rows.append(f"- {h.get('text_pattern', '?')} ({h.get('element_type', '?')} in {h.get('zone', '?')}, selector={h.get('selector', '?')})")
                        if rows:
                            hints += "\n\nVISUAL HINTS:\n" + "\n".join(rows)

                    logger.info(
                        "Map Hints (modular) | ⚡ FAST-TRACK MISSION: "
                        "Using pre-decision optimized sequence + quick-hive hints; bypassing Mind Reader/Hive."
                    )
                    return {
                        "hints": hints,
                        "pre_decision": pre_decision,
                        "route_hint": route_hint,
                    }
                if (
                    isinstance(pre_decision, dict)
                    and pre_decision.get("route") == "current_domain_last_mile"
                    and float(pre_decision.get("confidence") or 0.0) >= PRE_DECISION_MIN_CONF
                ):
                    conf = float(pre_decision.get("confidence") or 0.0)
                    logger.info(
                        f"Map Hints (pre-decision) | route=current_domain_last_mile conf={conf:.2f}"
                    )
                    return {
                        "hints": "ROUTE_HINT: current_domain_last_mile",
                        "pre_decision": pre_decision,
                        "route_hint": "current_domain_last_mile",
                    }
            except Exception as pre_err:
                logger.warning(f"Map Hints pre-decision gate failed; forcing pre-decision fallback route: {pre_err}")
                pre_decision = {
                    "execution_mode": "last_mile",
                    "route": "current_domain_last_mile",
                    "confidence": 0.55,
                    "start_with_strategy": True,
                    "recommended_strategy_order": [f"LAST_MILE: {goal}"],
                    "reason": f"Pre-decision failure fallback ({type(pre_err).__name__})",
                    "evidence": {
                        "visible_goal_signals": 0,
                        "obvious_controls": 0,
                        "hive_support_score": 0.0,
                    },
                }
                return {
                    "hints": "STRATEGY SEQUENCE: " + " -> ".join(pre_decision["recommended_strategy_order"]),
                    "pre_decision": pre_decision,
                    "route_hint": pre_decision.get("route", ""),
                }
        else:
            if not ENABLE_PRE_DECISION_GATE:
                logger.info("Map Hints pre-decision gate disabled: ENABLE_PRE_DECISION_GATE=false")
            elif not live_graph:
                logger.info("Map Hints pre-decision gate skipped: live_graph unavailable")

        schema = await mind_reader.translate(
            user_input=goal,
            current_url=current_url or ""
        )

        # Early page relevance gate: if goal clearly targets another domain,
        # skip Hive lookup and return immediate cross-domain guidance.
        gate_response, _ = run_page_relevance_gate(
            schema=schema,
            current_url=current_url or "",
            goal=goal,
            logger=logger,
        )
        if gate_response:
            action = gate_response.get("action", {})
            target_domain = action.get("target_domain", "")
            logger.info(
                f"Map Hints (modular) | cross_domain_shortcut=True target={target_domain or 'unknown'}"
            )
            return {"hints": f"CROSS_DOMAIN_TARGET: {target_domain}"}

        # Domain-index guard: avoid costly multi-query Hive retrieval for unmapped sites.
        domain = getattr(schema, "domain", "") or ""
        if domain:
            indexed = await hive_interface.is_domain_indexed(domain)
            if not indexed:
                logger.info(f"Map Hints (modular) | domain_not_indexed={domain} -> skip_hive")
                result: Dict[str, Any] = {"hints": ""}
                if pre_decision:
                    result["pre_decision"] = pre_decision
                    result["route_hint"] = pre_decision.get("route", "")
                return result

        hive_response = await hive_interface.retrieve(schema)

        hint_parts: List[str] = []
        if hive_response.strategy:
            hint_parts.append(
                "STRATEGY SEQUENCE: " + " -> ".join(hive_response.strategy.sequence)
            )
        if hive_response.visual_hints:
            rows = [
                f"- {h.text_pattern} ({h.element_type} in {h.zone}, selector={h.selector})"
                for h in hive_response.visual_hints[:8]
            ]
            hint_parts.append("VISUAL HINTS:\n" + "\n".join(rows))

        hints = "\n\n".join(hint_parts).strip()
        logger.info(
            f"Map Hints (modular) | strategy={bool(hive_response.strategy)} hints={len(hive_response.visual_hints)}"
        )
        result = {"hints": hints}
        if pre_decision:
            result["pre_decision"] = pre_decision
            result["route_hint"] = pre_decision.get("route", "")
        return result
    except Exception as e:
        logger.warning(f"Map hints fetch failed: {e}")
        return {"hints": ""}

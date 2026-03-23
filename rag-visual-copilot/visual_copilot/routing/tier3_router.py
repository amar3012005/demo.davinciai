import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from tara_models import ActionIntent

from visual_copilot.constants import DEBUG_TRACE_OUTPUTS
from visual_copilot.mission.verified_advance import record_and_maybe_advance
from visual_copilot.routing.action_guard import (
    _node_matches_expected_labels,
    _resolve_clickable_target_id,
    _validate_action_target,
)
from visual_copilot.routing.lexical_router import _find_best_type_target
from visual_copilot.text.tokenization import _extract_type_text

logger = logging.getLogger(__name__)


async def route_tier3(
    *,
    planner: Callable[..., Awaitable[Dict[str, Any]]],
    app: Any,
    mission: Any,
    schema: Any,
    query: str,
    nodes: list,
    excluded_ids: set[str],
) -> Dict[str, Any]:
    result = await planner(
        app=app,
        mission=mission,
        schema=schema,
        query=query,
        nodes=nodes,
        excluded_ids=excluded_ids,
    )
    result["route"] = result.get("route") or "tier3_fallback"
    return result


async def run_tier3_fallback(
    *,
    app: Any,
    goal: str,
    query: str,
    nodes: List[Any],
    mission: Any,
    mission_brain: Any,
    schema: Any,
    is_zero_shot: bool,
    start_time: float,
    forced_reason: str = "",
    excluded_ids: Optional[set[str]] = None,
    expected_labels: Optional[List[str]] = None,
    expected_domain: str = "",
    current_url: str = "",
    dom_signature: str = "",
    verified_advance_active: bool = False,
    strategy_authoritative: bool = False,
) -> Optional[Dict[str, Any]]:
    logger.warning(
        f"   ⚠️ Tier 1+2 FAILED/BYPASSED ({forced_reason or 'low_confidence'}). "
        "Activating Tier 3: V1 Full-DOM Fallback..."
    )

    try:
        tara_tools = [
            {
                "type": "function",
                "function": {
                    "name": "click_element",
                    "description": "Click a specific product, link, dropdown, or button.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "draft_thought": {"type": "string", "description": "A 10-word concise logic draft analyzing the DOM before acting"},
                            "target_id": {"type": "string", "description": "The exact ID of the element from the DOM list"}
                        },
                        "required": ["draft_thought", "target_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "type_text",
                    "description": "Type keywords into a search bar or input field.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "draft_thought": {"type": "string", "description": "A 10-word concise logic draft analyzing the DOM before acting"},
                            "target_id": {"type": "string"},
                            "text_to_type": {"type": "string", "description": "Concise search keywords"}
                        },
                        "required": ["draft_thought", "target_id", "text_to_type"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "answer_user",
                    "description": "Stop navigation and talk to the user if the goal is visible on screen.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "draft_thought": {"type": "string", "description": "A 10-word explanation of why the goal is achieved"},
                            "final_answer": {"type": "string", "description": "The information to speak to the user"}
                        },
                        "required": ["draft_thought", "final_answer"]
                    }
                }
            }
        ]

        interactive_nodes = [
            n for n in nodes
            if n.interactive and (n.text or n.tag == "input" or n.role == "searchbox")
        ]
        if interactive_nodes:
            priority_nodes = [n for n in interactive_nodes if n.tag in ["input", "textarea"] or n.role == "searchbox"]
            other_nodes = [n for n in interactive_nodes if n not in priority_nodes]
            final_nodes = (priority_nodes + other_nodes)[:100]
            compressed_dom = "\n".join([
                f"[ID: {n.id}] tag={n.tag} zone={n.zone} text='{n.text[:60] if n.text else ''}'"
                for n in final_nodes
            ])
        else:
            compressed_dom = "No interactive elements found."

        system_prompt = f"""You are a high-velocity web agent.
GOAL: '{goal}'
SUBGOAL: '{query}'

AVAILABLE NODES:
{compressed_dom}

INSTRUCTIONS:
1. You MUST call a tool.
2. Fill out the 'draft_thought' parameter FIRST to logically deduce your action.
3. If the goal is visible on the screen, use the 'answer_user' tool. Do not click unnecessarily.
4. If you must act, verify the 'target_id' perfectly matches an ID in the AVAILABLE NODES list.
"""

        api_key = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_KEY")
        if not api_key and hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
            api_key = getattr(app.state.mind_reader.llm._client, "api_key", getattr(app.state.mind_reader.llm, "api_key", None))

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "system", "content": system_prompt}],
                    "tools": tara_tools,
                    "tool_choice": "required",
                    "temperature": 0.0
                },
                timeout=10
            )

            if resp.status_code != 200:
                raise Exception(f"Groq API Error: {resp.status_code}")

            message = resp.json()["choices"][0]["message"]
            if DEBUG_TRACE_OUTPUTS:
                logger.info(f"TRACE_OUTPUT tier3_raw\n{json.dumps(message)}")
            if not message.get("tool_calls"):
                return None

            tool_call = message["tool_calls"][0]
            action_name = tool_call["function"]["name"]
            args = json.loads(tool_call["function"]["arguments"])

            logger.info(f"   [BRAIN] Tier 3 Chain of Draft: {args.get('draft_thought')}")
            logger.info(f"   [STARTUP] Tier 3 Tool Call: {action_name}")

            if action_name == "click_element":
                resolved_id, resolve_reason = _resolve_clickable_target_id(
                    args.get("target_id", ""), nodes, excluded_ids=excluded_ids
                )
                if resolved_id and resolved_id != args.get("target_id", ""):
                    logger.info(f"   [ZERO-SHOT] Tier 3 click target remap: {args.get('target_id')} -> {resolved_id}")
                    args["target_id"] = resolved_id
                
                # RELAX: For strategy subgoals, allow re-clicking if it's the explicit target
                # This prevents "target_already_excluded" from blocking legitimate strategy execution
                validation_excluded_ids = excluded_ids
                if strategy_authoritative and expected_labels:
                    # Check if target matches expected label - if so, allow re-click
                    target_node = next((n for n in nodes if getattr(n, "id", "") == args.get("target_id", "")), None)
                    if target_node:
                        target_text = (target_node.text or "").lower()
                        if any(exp_label.lower() in target_text for exp_label in expected_labels):
                            validation_excluded_ids = set()  # Allow re-click for strategy match
                            logger.info(
                                f"   🔄 Tier 3 exclusion relax: strategy subgoal allows re-click "
                                f"target={args.get('target_id')} label='{target_text[:30]}'"
                            )
                
                ok, reason = _validate_action_target("click", args.get("target_id", ""), nodes, excluded_ids=validation_excluded_ids)
                if not ok:
                    logger.warning(f"   [BLOCKED] Tier 3 rejected ungrounded click target: {reason} (resolve={resolve_reason})")
                    return None
                if expected_labels:
                    _candidate = next((n for n in nodes if getattr(n, "id", "") == args.get("target_id", "")), None)
                    if not _node_matches_expected_labels(_candidate, expected_labels, expected_domain):
                        logger.warning("TIER3_REJECT reason=tier3_label_mismatch action=click")
                        return None
                _t3_node = next((n for n in nodes if n.id == args["target_id"]), None)
                _t3_label = (_t3_node.text[:40].strip() if _t3_node and _t3_node.text else "the element")
                _next_idx = await record_and_maybe_advance(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="click",
                    target_id=args["target_id"],
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=dom_signature,
                    text="",
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                )
                return {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "click", "target_id": args["target_id"], "text": _t3_label},
                    "speech": f"Clicking on {_t3_label}...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_tier3",
                    "pending_verification": verified_advance_active,
                }

            if action_name == "type_text":
                _t3_text = args.get("text_to_type", "")
                ok, reason = _validate_action_target("type_text", args.get("target_id", ""), nodes, excluded_ids=excluded_ids)
                if not ok:
                    logger.warning(f"   [BLOCKED] Tier 3 rejected ungrounded type target: {reason}")
                    fallback_type_node = _find_best_type_target(nodes, query, excluded_ids or set())
                    if fallback_type_node:
                        logger.info(f"   [ZERO-SHOT] Tier 3 type fallback: using grounded input {fallback_type_node.id} after rejection={reason}")
                        _next_idx = await record_and_maybe_advance(
                            mission_brain=mission_brain,
                            mission=mission,
                            action_type="type_text",
                            target_id=fallback_type_node.id,
                            nodes=nodes,
                            current_url=current_url,
                            dom_signature=dom_signature,
                            text=_t3_text or _extract_type_text(query, schema.target_entity),
                            verified_advance_active=verified_advance_active,
                            is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                        )
                        return {
                            "success": True,
                            "blocked": False,
                            "action": {
                                "type": "type_text",
                                "target_id": fallback_type_node.id,
                                "text": _t3_text or _extract_type_text(query, schema.target_entity),
                                "press_enter": True
                            },
                            "speech": f"Typing '{_t3_text or _extract_type_text(query, schema.target_entity)}' and searching...",
                            "mission_id": mission.mission_id,
                            "subgoal_index": _next_idx,
                            "confidence": "medium",
                            "timing_ms": int((time.time() - start_time) * 1000),
                            "pipeline": "ultimate_tara_tier3",
                            "pending_verification": verified_advance_active,
                        }
                    return None
                if expected_labels:
                    _candidate = next((n for n in nodes if getattr(n, "id", "") == args.get("target_id", "")), None)
                    if not _node_matches_expected_labels(_candidate, expected_labels, expected_domain):
                        logger.warning("TIER3_REJECT reason=tier3_label_mismatch action=type_text")
                        return None
                logger.info(f"   [STARTUP] V1 Fallback Success: TYPE '{_t3_text}' into {args['target_id']}")
                _next_idx = await record_and_maybe_advance(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="type_text",
                    target_id=args["target_id"],
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=dom_signature,
                    text=_t3_text,
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                )
                return {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "type_text",
                        "target_id": args["target_id"],
                        "text": _t3_text,
                        "press_enter": True
                    },
                    "speech": f"Typing '{_t3_text}' and searching...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_tier3",
                    "pending_verification": verified_advance_active,
                }

            if action_name == "answer_user":
                logger.info("   [BRAIN] Tier 3: LLM says goal is already visible -- no click needed.")
                await mission_brain.advance_subgoal(mission.mission_id, is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot)
                return {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "answer", "speech": args.get("final_answer", "")},
                    "complete": True,
                    "pipeline": "ultimate_tara_tier3",
                    "confidence": 0.80
                }

    except Exception as e:
        logger.error(f"   [ERROR] Tier 3 V1 Fallback failed: {e}")

    return None

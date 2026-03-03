import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from visual_copilot.constants import _CLICK_ROLES, _CLICK_TAGS, _TYPE_ROLES, _TYPE_TAGS
from visual_copilot.routing.action_guard import _is_clickable_node, _is_type_node, _resolve_clickable_by_label_context, _resolve_clickable_target_id
from visual_copilot.text.tokenization import _tokenize

logger = logging.getLogger(__name__)
LOG_FULL_REASONING = (os.getenv("LOG_FULL_REASONING", "true").strip().lower() in {"1", "true", "yes", "on"})


@dataclass
class LastMilePlan:
    is_done: bool
    is_impossible: bool
    thought: str
    final_sequence: List[Dict[str, Any]]
    completion_answer: str = ""


def _should_enter_last_mile(mission: Any, query: str, schema: Any, is_zero_shot: bool = False) -> Tuple[bool, str]:
    if not mission:
        return False, "no_mission"

    phase = getattr(mission, "phase", "strategy")
    if phase == "last_mile":
        return True, "phase_last_mile"
    subgoals = getattr(mission, "subgoals", []) or []
    idx = int(getattr(mission, "current_subgoal_index", 0) or 0)

    # Zero-shot should not bypass strategy execution when strategy subgoals still exist.
    # Only prioritize last-mile once strategy is exhausted / terminal.
    if is_zero_shot and idx >= len(subgoals):
        return True, "zero_shot_vision_priority"

    if idx >= len(subgoals):
        return True, "strategy_exhausted"
    q = (query or "").lower().strip()
    if "extract and present" in q:
        return True, "explicit_extract_subgoal"
    if getattr(schema, "zero_shot_mode", False) and getattr(mission, "status", "") == "completed":
        return True, "zero_shot_completed"
    return False, "strategy_in_progress"


def _goal_completion_guard(main_goal: str, nodes: List[Any]) -> bool:
    goal_tokens = _tokenize(main_goal)
    if not goal_tokens:
        return False
    content_nodes = [n for n in nodes if getattr(n, "text", None) and getattr(n, "zone", "") == "main"]
    hits = 0
    for n in content_nodes:
        text_tokens = _tokenize(getattr(n, "text", ""))
        if len(goal_tokens & text_tokens) >= min(2, max(1, len(goal_tokens) // 3)):
            hits += 1
    return hits >= 2


def _validate_last_mile_step(step: Dict[str, Any], nodes: List[Any], excluded_ids: set[str]) -> Tuple[bool, str]:
    action = (step.get("action") or "").lower()
    target_id = (step.get("target_id") or "").strip()
    original_target_id = target_id
    if action not in {"click", "type_text", "select", "scroll", "answer", "wait"}:
        return False, "unsupported_action"
    if action in {"scroll", "answer", "wait"}:
        return True, "ok"
    if not target_id:
        return False, "missing_target_id"

    node = next((n for n in nodes if getattr(n, "id", "") == target_id and getattr(n, "interactive", False)), None)

    if action in {"click", "select"}:
        resolved_id, resolve_reason = _resolve_clickable_target_id(target_id, nodes, set())
        if not resolved_id:
            raw_node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
            fallback_labels: List[str] = []
            if raw_node and getattr(raw_node, "text", ""):
                fallback_labels.append(getattr(raw_node, "text", ""))
            if step.get("text"):
                fallback_labels.append(step.get("text", ""))
            if fallback_labels:
                fallback_id, fallback_reason = _resolve_clickable_by_label_context(
                    target_id,
                    fallback_labels,
                    "",
                    nodes,
                    excluded_ids=set(),
                )
                if fallback_id:
                    resolved_id = fallback_id
                    resolve_reason = f"label_context:{fallback_reason}"
        if not resolved_id:
            return False, resolve_reason
        if resolved_id != target_id:
            step["target_id"] = resolved_id
            target_id = resolved_id
            logger.info(f"LAST_MILE_STEP_RESOLVE original={original_target_id} resolved={resolved_id}")
        node = next((n for n in nodes if getattr(n, "id", "") == target_id and getattr(n, "interactive", False)), None)

    if not node:
        return False, "target_not_in_live_graph"

    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    if action == "type_text" and not ((tag in _TYPE_TAGS) or (role in _TYPE_ROLES) or _is_type_node(node)):
        return False, "type_target_not_input"
    if action in {"click", "select"} and not ((tag in _CLICK_TAGS) or (role in _CLICK_ROLES) or _is_clickable_node(node)):
        return False, "click_target_not_clickable"
    return True, "ok"


def _hash_last_mile_sequence(seq: List[Dict[str, Any]]) -> str:
    raw = json.dumps(seq, sort_keys=True, ensure_ascii=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


async def _last_mile_llm_reasoning(prompt: str, app: Any = None) -> str:
    """
    Modular last-mile reasoning entrypoint.
    Primary: gpt-oss-20b reasoning.
    Fallback: llama-3.1-8b-instant.
    """
    if app and hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
        llm = app.state.mind_reader.llm
        if hasattr(llm, "generate_with_reasoning"):
            try:
                result = await llm.generate_with_reasoning(
                    prompt,
                    model="openai/gpt-oss-20b",
                    max_completion_tokens=1024,
                    temperature=0.6,
                    reasoning_effort="low",
                    response_format={"type": "json_object"},
                )
                content = result.get("content", "")
                reasoning = result.get("reasoning", "")
                if reasoning:
                    if LOG_FULL_REASONING:
                        logger.info(f"🧠 ReAct CoT FULL:\n{reasoning}")
                    else:
                        logger.info(f"🧠 ReAct CoT: {reasoning[:200]}...")
                if content:
                    return content
                if reasoning:
                    logger.warning("🧠 gpt-oss-20b content empty, using reasoning text")
                    return reasoning
                logger.warning("🧠 gpt-oss-20b returned empty content+reasoning, falling back to 8B")
            except Exception as e:
                logger.warning(f"🧠 gpt-oss-20b failed ({e}), falling back to 8B")
        if hasattr(llm, "generate"):
            return await llm.generate(
                prompt,
                model="llama-3.1-8b-instant",
                temperature=0.1,
                max_tokens=512,
                response_format={"type": "json_object"},
            )
    return json.dumps(
        {
            "is_done": False,
            "is_impossible": False,
            "thought": "No LLM available for last-mile reasoning.",
            "completion_answer": "",
            "final_sequence": [],
        }
    )


async def plan_last_mile(
    *,
    schema: Any,
    mission: Any,
    nodes: list,
    app: Any = None,
) -> LastMilePlan:
    """
    Modular last-mile planner (migrated from mission_brain ownership).
    """
    main_goal = getattr(mission, "main_goal", "") or getattr(schema, "raw_utterance", "") or getattr(schema, "target_entity", "")
    history = getattr(mission, "action_history", [])[-10:] if mission else []
    history_str = "\n".join([f"- {h}" for h in history]) if history else "None"
    prior_subgoals = (getattr(mission, "subgoals", []) or [])[: int(getattr(mission, "current_subgoal_index", 0) or 0) + 1] if mission else []
    prior_subgoals_str = "\n".join([f"- {s}" for s in prior_subgoals]) if prior_subgoals else "None"
    remaining_subgoals = (
        (getattr(mission, "subgoals", []) or [])[int(getattr(mission, "current_subgoal_index", 0) or 0) + 1 :]
        if mission and int(getattr(mission, "current_subgoal_index", 0) or 0) + 1 < len(getattr(mission, "subgoals", []) or [])
        else []
    )
    remaining_subgoals_str = "\n".join([f"- {s}" for s in remaining_subgoals]) if remaining_subgoals else "None"
    current_subgoal = (
        (getattr(mission, "subgoals", []) or [])[int(getattr(mission, "current_subgoal_index", 0) or 0)]
        if mission and int(getattr(mission, "current_subgoal_index", 0) or 0) < len(getattr(mission, "subgoals", []) or [])
        else "(strategy exhausted)"
    )

    interactive_nodes = [
        n for n in nodes
        if getattr(n, "interactive", False) and (getattr(n, "text", None) or getattr(n, "tag", "") in ("input", "textarea", "select"))
    ][:120]
    compressed_dom = "\n".join(
        [f"[ID: {n.id}] tag={n.tag} zone={getattr(n, 'zone', '')} text='{(getattr(n, 'text', '') or '')[:80]}'" for n in interactive_nodes]
    ) or "(no interactive elements found)"

    readable_nodes = [
        n for n in nodes
        if getattr(n, "text", None) and len((getattr(n, "text", "") or "").strip()) >= 8 and getattr(n, "zone", "") in {"main", "content"}
    ][:80]
    readable_content = "\n".join(
        [f"[zone={getattr(n, 'zone', '')}] {(getattr(n, 'text', '') or '')[:180]}" for n in readable_nodes]
    ) or "(no substantial readable content found)"

    goal_terms = {
        t
        for t in re.findall(r"[a-z0-9]+", f"{main_goal} {current_subgoal}".lower())
        if len(t) > 2 and t not in {"click", "open", "select", "navigate", "link", "button", "page", "tab", "the", "and", "for", "with"}
    }

    scored_interactables = []
    for n in interactive_nodes:
        blob = " ".join(
            [
                getattr(n, "text", "") or "",
                getattr(n, "id", "") or "",
                getattr(n, "placeholder", "") or "",
                getattr(n, "name", "") or "",
                getattr(n, "role", "") or "",
                getattr(n, "zone", "") or "",
            ]
        ).lower()
        terms = set(re.findall(r"[a-z0-9]+", blob))
        overlap = len(terms & goal_terms)
        if overlap <= 0:
            continue
        zone = (getattr(n, "zone", "") or "").lower()
        zone_boost = 1 if zone in {"main", "content", "nav", "sidebar"} else 0
        scored_interactables.append((overlap * 2 + zone_boost, overlap, n))
    scored_interactables.sort(key=lambda x: (-x[0], -x[1], getattr(x[2], "id", "")))
    focus_interactables = "\n".join(
        [
            f"[ID: {n.id}] tag={n.tag} zone={getattr(n, 'zone', '')} text='{(getattr(n, 'text', '') or '')[:80]}' overlap={ov}"
            for _, ov, n in scored_interactables[:40]
        ]
    ) or "(no high-confidence interactable matches)"

    scored_evidence = []
    for n in readable_nodes:
        txt = (getattr(n, "text", "") or "").strip()
        if not txt:
            continue
        terms = set(re.findall(r"[a-z0-9]+", txt.lower()))
        overlap = len(terms & goal_terms)
        if overlap > 0:
            scored_evidence.append((overlap, overlap, txt))
    scored_evidence.sort(key=lambda x: (-x[0], -x[1], len(x[2])))
    focus_evidence = "\n".join([txt[:220] for _, _, txt in scored_evidence[:12]]) or "(no high-confidence readable evidence)"

    attempts = int(getattr(mission, "last_mile_attempts", 0) or 0)
    phase = getattr(mission, "phase", "strategy")
    subgoal_total = len(getattr(mission, "subgoals", []) or [])
    subgoal_index = int(getattr(mission, "current_subgoal_index", 0) or 0)
    last_plan_hash = getattr(mission, "last_mile_last_plan_hash", "") or "none"

    prompt = f"""You are TARA's Last-Mile Reasoning Planner.
Main user goal: "{main_goal}"
Target entity: "{getattr(schema, 'target_entity', '')}"
Action type: "{getattr(getattr(schema, 'action', None), 'value', getattr(schema, 'action', ''))}"
Current subgoal in focus: "{current_subgoal}"

Last-mile progression context:
- phase: {phase}
- attempts: {attempts}
- subgoal_index: {subgoal_index}
- subgoal_total: {subgoal_total}
- previous_last_mile_plan_hash: {last_plan_hash}

Strategy steps already attempted:
{prior_subgoals_str}

Remaining strategy steps (if any):
{remaining_subgoals_str}

Recent action history:
{history_str}

Current live interactable DOM:
{compressed_dom}

High-confidence goal-matched interactables:
{focus_interactables}

Current readable page content (for answering):
{readable_content}

High-confidence goal evidence excerpts:
{focus_evidence}

Task:
Return whether the goal is complete. If not complete, return a grounded final sequence of commands.

Hard constraints:
1) Non-answer commands MUST use an ID that exists in Current live interactable DOM.
2) Never invent IDs.
3) Keep final_sequence concise (1-4 steps).
4) Allowed actions: click, type_text, select, scroll, wait, answer.
5) First reason about intent: is the user asking for an answer now, or asking to perform more UI actions first?
6) If the user asked a question (what/how/why/which), prefer answering from visible readable content when evidence exists.
7) If done, set is_done=true AND provide a non-empty completion_answer grounded in visible content.
8) If not done, set is_done=false and provide actionable final_sequence from current DOM only.
9) Never return is_done=true with empty completion_answer.
10) Do NOT do policy/refusal moderation in this planner; this component only plans grounded UI actions.
11) Set is_impossible=true only if there are no usable interactables to progress from current DOM.
12) Decide explicitly between:
    - "goal_reached_answer_now": if visible evidence already answers the main user goal.
    - "goal_not_reached_proceed": if evidence is insufficient and more UI steps are needed.
13) Never claim done based only on URL changes or partial similarity; require visible content evidence.
14) If current page appears off-track vs current subgoal and main goal, prefer corrective navigation/click from current DOM.
15) If user goal is question-like and High-confidence goal evidence excerpts already contain answer candidates, prefer is_done=true with grounded completion_answer.
16) Avoid extra navigation clicks when evidence already supports answering, unless answer-critical evidence is clearly missing.
17) If target ID is not present in the provided DOM list, do NOT invent IDs (no placeholders like "t-?", "unknown", "none").
18) If the page appears to be loading or data panels are empty but expected, use action "wait" first before declaring impossible.

Reply in strict JSON:
{{
  "is_done": false,
  "is_impossible": false,
  "thought": "brief reasoning",
  "completion_answer": "",
  "final_sequence": [
    {{"action": "click", "target_id": "ID", "text": "", "press_enter": false, "why": "brief"}}
  ]
}}"""
    try:
        raw = await _last_mile_llm_reasoning(prompt, app=app)
        parsed = None
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                parsed = None
        if not parsed:
            return LastMilePlan(False, False, "Failed to parse last-mile plan JSON", [])

        seq = parsed.get("final_sequence", []) or []
        if not isinstance(seq, list):
            seq = []

        allowed = {"click", "type_text", "select", "scroll", "wait", "answer"}
        cleaned: List[Dict[str, Any]] = []
        invalid_id_seen = False
        for step in seq[:6]:
            if not isinstance(step, dict):
                continue
            action = (step.get("action") or "").strip().lower()
            if action not in allowed:
                continue
            target_id = (step.get("target_id") or "").strip()
            if action in {"click", "type_text", "select"}:
                bad_id = (
                    not target_id
                    or "?" in target_id
                    or "unknown" in target_id.lower()
                    or "none" == target_id.lower()
                    or "missing" in target_id.lower()
                    or "id" == target_id.lower()
                )
                if bad_id:
                    invalid_id_seen = True
                    continue
            cleaned.append(
                {
                    "action": action,
                    "target_id": target_id,
                    "text": (step.get("text") or "").strip(),
                    "press_enter": bool(step.get("press_enter", False)),
                    "why": (step.get("why") or "").strip(),
                }
            )

        is_done = bool(parsed.get("is_done", False))
        is_impossible = bool(parsed.get("is_impossible", False))
        thought = (parsed.get("thought") or "").strip()
        completion_answer = (parsed.get("completion_answer") or "").strip()
        thought_l = thought.lower()
        if invalid_id_seen:
            thought = (thought + " | normalized:invalid_or_missing_target_id").strip(" |")
            if not cleaned and not completion_answer and not is_done:
                is_impossible = True

        refusal_markers = ("not allowed", "cannot help", "can't help", "policy", "illegal", "harmful", "adult content", "refuse")
        if is_impossible and any(m in thought_l for m in refusal_markers):
            is_impossible = False
            thought = (thought + " | normalized:policy_refusal_ignored").strip(" |")

        if is_done and not completion_answer:
            best_line = ""
            best_hits = 0
            for n in readable_nodes:
                txt = (getattr(n, "text", "") or "").strip()
                if not txt:
                    continue
                tokens = set(re.findall(r"[a-z0-9]+", txt.lower()))
                hits = len(tokens & goal_terms)
                if hits > best_hits:
                    best_hits = hits
                    best_line = txt
            if best_line and best_hits >= 1:
                completion_answer = best_line[:320]
                thought = (thought + " | normalized: answer_from_visible_content").strip(" |")
            else:
                is_done = False
                thought = (thought + " | normalized: done_without_answer").strip(" |")

        return LastMilePlan(
            is_done=is_done,
            is_impossible=is_impossible,
            thought=thought,
            final_sequence=cleaned,
            completion_answer=completion_answer,
        )
    except Exception as e:
        logger.error(f"Last-mile planning failed: {e}")
        return LastMilePlan(
            is_done=False,
            is_impossible=False,
            thought=f"Planner exception: {e}",
            final_sequence=[],
        )


# ═══════════════════════════════════════════════════════════════════════
# ⚡ COMPOUND AGENTIC LAST MILE — Multi-turn Tool Calling Loop
# ═══════════════════════════════════════════════════════════════════════

COMPOUND_MAX_INTERNAL_ITERATIONS = 10
COMPOUND_STAGNANCY_THRESHOLD = 3  # consecutive unchanged DOM hashes before forcing stop

COMPOUND_SYSTEM_PROMPT = """You are TARA's Last-Mile Execution Agent. The navigation phase is COMPLETE — you are now ON the target page.

Your job: Complete the user's goal through precise DOM interactions using the tools provided.

## Chain-of-Thought Strategy:
1. Carefully read the provided DOM elements to understand the current page state.
2. Reason step-by-step: What does the goal require? What elements are available? What is the single best next action?
3. Execute ONE action at a time via tool calls — never assume success without verification.
4. If an element is not visible in the provided DOM, use scroll_page to reveal more content.
5. Use request_vision ONLY when the DOM text is ambiguous or you need to interpret visual elements (charts, icons).
6. Call complete_mission when you have gathered enough evidence to answer the user OR when you are stuck.

## Anti-Loop & Amnesia Rules:
- READ BEFORE CLICK: If you just arrived at a new page (Action History shows a recent click), you MUST read and analyze the "Current Readable Page Content" to see if your goal is already met before you click anything else.
- DO NOT CLICK THE SAME ID TWICE: If your action history shows you just clicked an element, or if your internal tool call history shows you clicked an element and the page state did not change, you MUST NOT click it again. Try a different approach or call complete_mission.

## Decision Rules:
- ONLY use target_id values that appear in the DOM list provided. NEVER invent IDs.
- Prefer answering from visible readable content when the user asked an information question.
- If the page is loading, use wait_for_ui before attempting actions.
- Be efficient — do not repeat actions that already worked.

## You MUST reason before every tool call.
"""


def _compress_dom_for_compound(nodes: list) -> str:
    """Build a compact DOM representation for the compound loop context."""
    interactive = [
        n for n in nodes
        if getattr(n, "interactive", False)
        and (getattr(n, "text", None) or getattr(n, "tag", "") in ("input", "textarea", "select"))
    ][:120]
    lines = []
    for n in interactive:
        text = (getattr(n, "text", "") or "")[:80]
        tag = getattr(n, "tag", "")
        zone = getattr(n, "zone", "")
        role = getattr(n, "role", "")
        parts = f"[ID: {n.id}] tag={tag}"
        if zone:
            parts += f" zone={zone}"
        if role:
            parts += f" role={role}"
        if text:
            parts += f" text='{text}'"
        lines.append(parts)
    return "\n".join(lines) or "(no interactive elements found)"


def _compress_readable_for_compound(nodes: list) -> str:
    """Build a compact readable-content representation."""
    readable = [
        n for n in nodes
        if getattr(n, "text", None)
        and len((getattr(n, "text", "") or "").strip()) >= 8
        and getattr(n, "zone", "") in {"main", "content"}
    ][:60]
    lines = []
    for n in readable:
        text = (getattr(n, "text", "") or "").strip()[:180]
        zone = getattr(n, "zone", "")
        lines.append(f"[zone={zone}] {text}")
    return "\n".join(lines) or "(no substantial readable content found)"


def _dom_signature_hash(nodes: list) -> str:
    """Quick hash of DOM state for stagnancy detection."""
    parts = []
    for n in nodes[:300]:
        parts.append(
            f"{getattr(n, 'id', '')}|{getattr(n, 'tag', '')}|{(getattr(n, 'text', '') or '')[:40]}"
        )
    raw = "||".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _prune_old_dom_context(messages: list) -> list:
    """
    Context Pruning: Remove old large DOM/readable content injections from
    tool result messages, keeping only the most recent one. This prevents
    token bloat as the loop iterates.
    """
    dom_result_indices = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" and "[ID:" in (msg.get("content") or ""):
            dom_result_indices.append(i)
    # Keep only the last DOM injection, truncate older ones
    if len(dom_result_indices) > 1:
        for idx in dom_result_indices[:-1]:
            old_content = messages[idx].get("content", "")
            # Replace with a short summary instead of deleting (to keep conversation flow)
            messages[idx]["content"] = "(previous DOM state — pruned for context efficiency)"
    return messages


async def run_compound_last_mile(
    *,
    schema: Any,
    mission: Any,
    nodes: list,
    app: Any = None,
    screenshot_b64: str = "",
    session_id: str = "",
    excluded_ids: set = None,
) -> Dict[str, Any]:
    """
    Compound Agentic Last-Mile Loop.

    Instead of a single-pass JSON plan, this function runs an internal
    multi-turn tool-calling loop with the Groq LLM. The LLM reasons,
    picks a tool, gets feedback (or an error for hallucinated IDs), and
    re-reasons until it either:
      - Issues a physical browser action (click/type/scroll/wait) → returned to frontend
      - Calls complete_mission → final answer returned
      - Hits the iteration limit → graceful fallback

    Returns a dict with keys:
      action: dict describing the browser action or answer
      thought: str of the LLM's last reasoning
      iterations: int of internal loop turns used
      status: "action" | "complete" | "impossible" | "max_iterations"
    """
    from visual_copilot.mission.last_mile_tools import LAST_MILE_TOOLS
    from visual_copilot.mission.tool_executor import execute_internal_tool

    main_goal = (
        getattr(mission, "main_goal", "")
        or getattr(schema, "raw_utterance", "")
        or getattr(schema, "target_entity", "")
    )
    history = getattr(mission, "action_history", [])[-8:] if mission else []
    history_str = "\n".join([f"- {h}" for h in history]) if history else "None"

    # Build excluded IDs warning for the LLM
    excluded_ids = excluded_ids or set()
    excluded_str = ""
    if excluded_ids:
        excluded_str = (
            f"\n**⚠️ ALREADY-CLICKED IDs (DO NOT click these again):**\n"
            + ", ".join(sorted(excluded_ids))
            + "\n"
        )

    compressed_dom = _compress_dom_for_compound(nodes)
    readable_content = _compress_readable_for_compound(nodes)
    current_dom_hash = _dom_signature_hash(nodes)

    target_entity = getattr(schema, "target_entity", "")
    action_type_str = str(
        getattr(getattr(schema, "action", None), "value", getattr(schema, "action", ""))
    )

    messages = [
        {"role": "system", "content": COMPOUND_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"## Mission Brief\n"
                f"**Main Goal:** {main_goal}\n"
                f"**Target Entity:** {target_entity}\n"
                f"**Action Type:** {action_type_str}\n\n"
                f"**Recent Action History:**\n{history_str}\n"
                f"{excluded_str}\n"
                f"**Current Interactable DOM:**\n{compressed_dom}\n\n"
                f"**Current Readable Page Content:**\n{readable_content}\n\n"
                f"Begin executing the goal. Analyze the DOM, then take the best next action."
            ),
        },
    ]

    # Resolve the LLM client
    llm = None
    if app and hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
        llm = app.state.mind_reader.llm

    if not llm or not hasattr(llm, "generate_with_tools"):
        logger.warning("⚠️ LLM does not support generate_with_tools, falling back to legacy plan_last_mile")
        plan = await plan_last_mile(schema=schema, mission=mission, nodes=nodes, app=app)
        return {
            "action": {
                "type": "answer" if plan.is_done else ("scroll" if not plan.is_impossible else "clarify"),
                "speech": plan.completion_answer or plan.thought,
                "text": plan.completion_answer or plan.thought,
            },
            "thought": plan.thought,
            "iterations": 1,
            "status": "complete" if plan.is_done else ("impossible" if plan.is_impossible else "action"),
            "final_sequence": plan.final_sequence,
        }

    # ── Internal Compound Loop ──
    stagnancy_counter = 0
    last_dom_hash = current_dom_hash
    last_thought = ""

    for iteration in range(1, COMPOUND_MAX_INTERNAL_ITERATIONS + 1):
        try:
            message = await llm.generate_with_tools(
                messages=messages,
                tools=LAST_MILE_TOOLS,
                model="llama-3.3-70b-versatile",
                tool_choice="auto",
                max_tokens=1024,
                temperature=0.1,
            )
        except Exception as e:
            logger.error(f"🔴 Compound LLM call failed at iteration {iteration}: {e}")
            return {
                "action": {"type": "scroll", "speech": "Internal reasoning error, scrolling to retry."},
                "thought": f"LLM error: {e}",
                "iterations": iteration,
                "status": "action",
            }

        # Extract content for logging
        content = getattr(message, "content", None) or ""
        tool_calls = getattr(message, "tool_calls", None)
        last_thought = content

        if content:
            logger.info(f"🧠 COMPOUND iter={iteration} thought: {content[:300]}")

        # No tool calls → model decided it's done reasoning
        if not tool_calls:
            logger.info(f"🏁 COMPOUND EXIT iter={iteration} reason=no_tool_calls")
            # Attempt to parse the content as a completion answer
            return {
                "action": {"type": "answer", "speech": content, "text": content},
                "thought": content,
                "iterations": iteration,
                "status": "complete",
            }

        # Serialize the assistant message for history
        assistant_msg = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        # Process each tool call
        for tc in tool_calls:
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                tool_args = {}

            logger.info(f"⚙️ COMPOUND TOOL iter={iteration} name={tool_name} args={tool_args}")

            is_terminal, tool_result_str, frontend_action = await execute_internal_tool(
                tool_name=tool_name,
                args=tool_args,
                nodes=nodes,
                screenshot_b64=screenshot_b64 or None,
                app=app,
                session_id=session_id,
                excluded_ids=excluded_ids,
            )

            if is_terminal and frontend_action:
                # This is a physical action to send to the browser
                action_type = frontend_action.get("type", "")

                if action_type == "answer":
                    status = frontend_action.get("status", "success")
                    return {
                        "action": frontend_action,
                        "thought": last_thought,
                        "iterations": iteration,
                        "status": "complete" if status == "success" else "impossible",
                    }

                # For click/type/scroll/wait → return to frontend for execution
                logger.info(
                    f"🎯 COMPOUND ACTION iter={iteration} type={action_type} "
                    f"target={frontend_action.get('target_id', 'N/A')}"
                )
                return {
                    "action": frontend_action,
                    "thought": last_thought,
                    "iterations": iteration,
                    "status": "action",
                }

            # Non-terminal: feed the tool result back into the conversation
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": tool_result_str}
            )

        # ── Stagnancy Guard ──
        new_dom_hash = _dom_signature_hash(nodes)
        if new_dom_hash == last_dom_hash:
            stagnancy_counter += 1
        else:
            stagnancy_counter = 0
            last_dom_hash = new_dom_hash

        if stagnancy_counter >= COMPOUND_STAGNANCY_THRESHOLD:
            logger.warning(
                f"🛡️ COMPOUND STAGNANCY iter={iteration} "
                f"unchanged_turns={stagnancy_counter} → forcing exit"
            )
            return {
                "action": {
                    "type": "clarify",
                    "speech": f"I've been trying to progress on '{main_goal}' but the page isn't changing. Could you help guide me?",
                },
                "thought": f"Stagnancy detected after {stagnancy_counter} unchanged turns.",
                "iterations": iteration,
                "status": "impossible",
            }

        # ── Context Pruning ──
        messages = _prune_old_dom_context(messages)

    # Max iterations reached
    logger.warning(f"⏱️ COMPOUND MAX_ITER reached ({COMPOUND_MAX_INTERNAL_ITERATIONS})")
    return {
        "action": {
            "type": "clarify",
            "speech": f"I spent {COMPOUND_MAX_INTERNAL_ITERATIONS} reasoning steps on '{main_goal}' but couldn't complete it. Would you like me to try a different approach?",
        },
        "thought": last_thought or "Max iterations reached without resolution.",
        "iterations": COMPOUND_MAX_INTERNAL_ITERATIONS,
        "status": "max_iterations",
    }

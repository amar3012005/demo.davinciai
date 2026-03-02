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


def _should_enter_last_mile(mission: Any, query: str, schema: Any) -> Tuple[bool, str]:
    if not mission:
        return False, "no_mission"
    phase = getattr(mission, "phase", "strategy")
    if phase == "last_mile":
        return True, "phase_last_mile"
    subgoals = getattr(mission, "subgoals", []) or []
    idx = int(getattr(mission, "current_subgoal_index", 0) or 0)
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

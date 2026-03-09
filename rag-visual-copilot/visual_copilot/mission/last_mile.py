import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from visual_copilot.constants import _CLICK_ROLES, _CLICK_TAGS, _TYPE_ROLES, _TYPE_TAGS
from visual_copilot.routing.action_guard import _is_clickable_node, _is_type_node, _resolve_clickable_by_label_context, _resolve_clickable_target_id
from visual_copilot.text.tokenization import _tokenize

logger = logging.getLogger(__name__)
LOG_FULL_REASONING = (os.getenv("LOG_FULL_REASONING", "true").strip().lower() in {"1", "true", "yes", "on"})

# ═══════════════════════════════════════════════════════════════════════
# Feature flags for last-mile hardening (can be overridden via env vars)
# ═══════════════════════════════════════════════════════════════════════
LAST_MILE_READ_FIRST_ENFORCED = os.getenv("LAST_MILE_READ_FIRST_ENFORCED", "true").strip().lower() in {"1", "true", "yes", "on"}
LAST_MILE_SEMANTIC_GUARD = os.getenv("LAST_MILE_SEMANTIC_GUARD", "true").strip().lower() in {"1", "true", "yes", "on"}
LAST_MILE_VISION_POLICY_TRIGGER = os.getenv("LAST_MILE_VISION_POLICY_TRIGGER", "true").strip().lower() in {"1", "true", "yes", "on"}

# Thresholds
EVIDENCE_RELEVANCE_THRESHOLD = 3  # min goal-term overlap to consider evidence "strong"
SEMANTIC_REPEAT_MAX = 3  # max consecutive concept-neighbor clicks before hard-stop
EVIDENCE_MISS_MAX = 3  # max consecutive iterations without evidence improvement before vision trigger
PROGRESS_STALL_MAX = 4  # max actions without progress improvement before stuck


# ═══════════════════════════════════════════════════════════════════════
# Last-Mile State Machine
# ═══════════════════════════════════════════════════════════════════════

class LastMilePhase(str, Enum):
    READ_EVIDENCE = "read_evidence"
    DECIDE_ACTION = "decide_action"
    EXECUTE_ACTION = "execute_action"
    VERIFY_PROGRESS = "verify_progress"
    COMPLETE = "complete"


@dataclass
class LastMileState:
    """Tracks progress within a single compound last-mile invocation."""
    phase: LastMilePhase = LastMilePhase.READ_EVIDENCE
    iteration: int = 0
    evidence_hits: int = 0
    evidence_miss_streak: int = 0
    semantic_repeat_streak: int = 0
    last_3_actions: List[str] = field(default_factory=list)
    last_answer_candidate: str = ""
    last_clicked_labels: List[str] = field(default_factory=list)
    progress_score: float = 0.0
    best_progress_score: float = 0.0
    stall_count: int = 0
    vision_used: bool = False
    exit_reason: str = ""
    action_pipeline: List[Dict[str, Any]] = field(default_factory=list)
    semantic_summaries: List[str] = field(default_factory=list)

    def record_action(self, action_label: str) -> None:
        self.last_3_actions.append(action_label)
        if len(self.last_3_actions) > 3:
            self.last_3_actions = self.last_3_actions[-3:]

    def record_click_label(self, label: str) -> None:
        self.last_clicked_labels.append(label.lower().strip())
        if len(self.last_clicked_labels) > 6:
            self.last_clicked_labels = self.last_clicked_labels[-6:]

    def is_semantic_repeat(self, new_label: str) -> bool:
        """Detect if clicking concept-neighbor links repeatedly.

        Uses token overlap ratio (shared / min_size) to catch cases where
        labels share the same core concept even if wording differs.
        E.g., "API Reference" and "API Documentation" share the concept "api".
        """
        if not LAST_MILE_SEMANTIC_GUARD:
            return False
        noise = {"click", "the", "and", "for", "with", "this", "that", "from", "more", "about", "view"}
        new_tokens = set(re.findall(r"[a-z0-9]+", new_label.lower())) - noise
        if not new_tokens or len(new_tokens) < 2:
            return False
        consecutive_similar = 0
        for prev in reversed(self.last_clicked_labels):
            prev_tokens = set(re.findall(r"[a-z0-9]+", prev)) - noise
            if not prev_tokens:
                break
            overlap = len(new_tokens & prev_tokens)
            # Use overlap / min(sizes) — catches partial overlap well
            min_size = min(len(new_tokens), len(prev_tokens))
            similarity = overlap / max(min_size, 1)
            if similarity >= 0.4 and overlap >= 2:
                consecutive_similar += 1
            else:
                break
        return consecutive_similar >= SEMANTIC_REPEAT_MAX

    def update_progress(self, new_evidence_hits: int) -> None:
        """Update progress score based on evidence improvement."""
        if new_evidence_hits > self.evidence_hits:
            self.evidence_hits = new_evidence_hits
            self.evidence_miss_streak = 0
            self.progress_score = float(new_evidence_hits)
            if self.progress_score > self.best_progress_score:
                self.best_progress_score = self.progress_score
            self.stall_count = 0
        else:
            self.evidence_miss_streak += 1
            self.stall_count += 1

    def should_trigger_vision(self) -> bool:
        """Policy-based vision trigger: only after repeated evidence misses AND semantic repeats."""
        if not LAST_MILE_VISION_POLICY_TRIGGER:
            return False
        if self.vision_used:
            return False
        return (
            self.evidence_miss_streak >= EVIDENCE_MISS_MAX
            and self.semantic_repeat_streak >= max(1, SEMANTIC_REPEAT_MAX - 1)
        )

    def should_hard_stop(self) -> bool:
        """Deterministic hard-stop when no progress is being made."""
        return self.stall_count >= PROGRESS_STALL_MAX

    def to_diagnostic(self) -> Dict[str, Any]:
        return {
            "phase": self.phase.value,
            "iteration": self.iteration,
            "evidence_hits": self.evidence_hits,
            "evidence_miss_streak": self.evidence_miss_streak,
            "semantic_repeat_streak": self.semantic_repeat_streak,
            "progress_score": self.progress_score,
            "stall_count": self.stall_count,
            "vision_used": self.vision_used,
            "exit_reason": self.exit_reason,
            "semantic_summaries": self.semantic_summaries,
        }

def _get_page_index_node(current_url: str) -> Optional[Dict[str, Any]]:
    import json, re, os
    try:
        sm_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "site_map.json"
        )
        if not os.path.exists(sm_path):
            return None
        with open(sm_path, "r") as f:
            site_map = json.load(f)
        def _walk(node):
            if "path_regex" in node and node["path_regex"]:
                try:
                    if re.search(node["path_regex"], current_url):
                        return node
                except Exception:
                    pass
            for child in node.get("children", []):
                res = _walk(child)
                if res: return res
            return None
        return _walk(site_map.get("root", {}))
    except Exception:
        return None

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
    
    # CRITICAL: If this is the LAST subgoal and it's an extraction/question (not "Click X"),
    # trigger compound last-mile immediately for intelligent answer extraction.
    if idx == len(subgoals) - 1:
        current_subgoal = subgoals[idx].strip()
        # Check if it's NOT a navigation subgoal (doesn't start with "Click ")
        if not current_subgoal.lower().startswith("click "):
            # This is an extraction/question subgoal — trigger compound last-mile
            logger.info(
                f"LAST_MILE_TRIGGER: Final subgoal is extraction (not nav) — "
                f"subgoal='{current_subgoal[:60]}'"
            )
            return True, "final_subgoal_extraction"
    
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


def _extract_ids_from_vision_brief(text: str) -> List[str]:
    """Extract candidate target IDs from plain-text vision brief, preserving order."""
    if not text:
        return []
    ids = []
    seen = set()
    for m in re.finditer(r"\b(?:id=)?(t-[a-z0-9]+)\b", text, flags=re.IGNORECASE):
        tid = (m.group(1) or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        ids.append(tid)
    return ids


def _fallback_action_from_vision_context(
    *,
    messages: List[Dict[str, Any]],
    nodes: List[Any],
    excluded_ids: set[str],
) -> Optional[Dict[str, Any]]:
    """
    Deterministic fallback when LLM returns no tool calls:
    pick first clickable vision-suggested ID that passes DOM validation.
    """
    vision_text = ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "")
        if "VISION BOOTSTRAP" in content or "Vision Strategic Brief" in content:
            vision_text = content
            break
    if not vision_text:
        return None

    candidate_ids = _extract_ids_from_vision_brief(vision_text)
    for tid in candidate_ids:
        if tid in excluded_ids:
            continue
        node = next((n for n in nodes if str(getattr(n, "id", "")) == tid), None)
        if not node:
            continue
        if not bool(getattr(node, "interactive", False)):
            continue
        if not _is_clickable_node(node):
            continue
        step = {"action": "click", "target_id": tid, "text": "", "press_enter": False, "why": "Fallback from vision brief after no-tool response."}
        ok, _ = _validate_last_mile_step(step, nodes, excluded_ids)
        if not ok:
            continue
        return {
            "type": "click",
            "target_id": tid,
            "text": (getattr(node, "text", "") or "").strip()[:120],
            "speech": "Proceeding with the best validated target from vision guidance.",
            "force_click": True,
        }
    return None


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

COMPOUND_SYSTEM_PROMPT = """You are TARA's Last-Mile Execution Agent. You understand that humans speak in free, natural language. 
Your mission is to translate human intent (slang, questions, implied needs) into precise browser actions and insightful answers.

## Human-Centric Understanding:
- Humans say "how much money did I spend" → They mean total cost/usage.
- Humans say "show me the thing" → They want you to find and extract the specific entity.
- Focus on THE SOUL of the request.
- **Deductive Extraction**: If the user asks "which is best?" or "what should I use?" and the page doesn't say it explicitly, EVALUATE the visible data. If you see context windows, pricing, or model names, reason through them to provide a "Best Effort" answer. 
- Example: "Based on the specs here, Model X is the strongest because it has the largest context window, even though the page doesn't explicitly rank them."

## MANDATORY Read → Verify → Act Loop:
Every iteration you MUST follow this exact sequence:
1. **READ**: Analyze everything. Does the human's desire (or the raw data to satisfy it) exist in the "Readable Content"?
2. **VERIFY**: Can you answer the human right now? 
   - **Reasoning over Evidence**: If the data is there but the "answer" isn't pre-written, you are REQUIRED to reason and synthesize the answer.
   - If YES (Direct or Inferred) → call complete_mission.
   - If NO → state clearly why. "I see a list of models, but no pricing data yet to determine 'cheapest'."
3. **ACT**: Execute ONE targeted action. State what new evidence you expect to see.

## Tool Priority (STRICT ORDER — higher priority tools MUST be considered first):
1. **complete_mission** — ALWAYS prefer this when readable content contains evidence that answers the goal. Include the specific text evidence.
2. **read_page_content** — Use to re-examine visible content against the goal before clicking.
3. **type_text** — Only when you need to search/filter for missing information.
4. **click_element** — Only when current page content is genuinely insufficient AND the target will lead to new evidence. You MUST state WHY current evidence is not enough.
5. **scroll_page** — Only when content may exist below the fold.
6. **wait_for_ui** — Only when the page is visibly loading.
7. **request_vision** — NEVER call this proactively. It is triggered automatically by the system when needed.

## Anti-Loop & Drift Prevention Rules:
- **READ BEFORE CLICK**: You MUST evaluate readable content FIRST every iteration. If you skip this and click immediately, the system will reject your action.
- **DO NOT CLICK THE SAME ID TWICE**: Already-clicked IDs are listed in the brief. Clicking them again will be blocked.
- **NO CONCEPT-NEIGHBOR DRIFT**: Do NOT click links that are "related to" but not "required for" the goal. If you clicked "API Reference" and it didn't help, do NOT then click "API Documentation", "API Guide", "Developer Docs" etc. — these are concept-neighbors and will not help.
- **COMPLETION OVER EXPLORATION**: If you have ANY reasonable answer from visible content, prefer complete_mission over clicking more links. A partial answer grounded in evidence is better than clicking into a rabbit hole.

## Multi-Action Pipelining (Latency Optimization):
- You may execute multiple UI actions in a row (e.g., open a dropdown AND click an option, or click a field and type text). 
- To do this, simply output multiple action tool calls in the same response.
- When you are finished with your action sequence, you MUST call `wait_for_ui` to pause and allow the page to visibly render before you can read new evidence.
- Physical actions (click, type, scroll) are QUEUED until you call `wait_for_ui` or `complete_mission` to execute the queue.
- If the latest tool observation includes "Vision Action Plan", treat it as a suggestion set, not absolute truth.
- DOM STATE OVERRIDE: if current DOM/readable content indicates you are already in a section (for example Usage/Dashboard active), you are FORBIDDEN from clicking that same sidebar/nav section again. Instead, act on in-page controls (filters, tabs, dropdowns, date ranges, table rows).
- Validate every vision-suggested click against current DOM state before executing.

## Decision Record (include in your reasoning before EVERY tool call):
- goal_status: found | partial | not_found
- evidence_summary: what relevant text you see right now
- why_not_complete: (required if you're NOT calling complete_mission) specific missing information
- expected_gain: what the proposed action will add

## Strict Metric Rule & Extraction Checklist:
- The answer MUST match the exact metric/unit the user asked for.
- If the goal is to find a specific number (Usage/Tokens/Price/Cost), you are NOT allowed to call `complete_mission` until that specific number is present in the `Current Readable Content`.
- If the user asks for TOKENS, do NOT return DOLLARS or COST.
- If the visible content shows a different metric than requested, do NOT treat it as the answer.
  Instead: look for tabs, toggles, dropdowns, or filters that switch the view to the correct metric.
- Example: User asks "how many Whisper tokens did I use?" → "Total Spend $0.84" is WRONG. You must find token usage, not dollar amounts.

## ANTI-RECURSION RULE:
- If URL/title/readable content already indicates the target section is open (e.g., Usage, Dashboard, Billing page already visible), do NOT click the same nav/sidebar link again.
- Re-clicking active section navigation is considered non-progress and should be avoided.

## Strict Entity Guard (LogicCritic Rule):
- Before calling complete_mission, you MUST verify that the data matches the specific model or target entity requested!
- **ENTITY ANCHOR REQUIRED**: You are FORBIDDEN from calling complete_mission unless the target entity name (e.g. "Whisper", "Llama-3", "Usage Dashboard") appears explicitly in your answer or evidence.
- If the screen shows "General Docs" but you need "API Keys", you are NOT done. Do NOT extract data from a different entity's section.
- You must use `scroll_page` or click tabs to find the specific entity the user requested.

## STRICT RULE: MENU PERSISTENCE:
- If you open a dropdown, combobox, or menu, you are PROHIBITED from calling `complete_mission` or ending your turn without calls until you have either clicked a selection inside that menu or closed it.
- CRITICAL: If a dropdown menu or combobox is OPEN (verifiable by evidence hits like 'Last 7 days' or 'Last month'), you are FORBIDDEN from calling `scroll_page` or clicking unrelated sidebar links. Your NEXT action MUST be selecting an item from that menu.
- Do not claim success just because you see a target option inside an open menu; you must select it and wait for the page to update.
- CRITICAL: When clicking ANY element inside a dropdown, menu popup, sub-menu, or overlay that just appeared, you MUST ALWAYS set `force_click=true` in your `click_element` action. If you don't, the simulated mouse animation will cause the menu to vanish before it clicks!

## STRICT RULE: SEARCH RESULT VERIFICATION:
- If you perform a SEARCH action (type_text with press_enter=True OR clicking a search button), you are PROHIBITED from calling complete_mission until you have CONFIRMED the results have loaded.
- After a search, your VERY NEXT action MUST be either:
  a) `wait_for_ui` — if the page is still loading (spinners, skeleton screens visible)
  b) `read_page_content` — to verify the results loaded and contain the target entity
- You are NOT allowed to call complete_mission with "search performed" as the answer. The answer is the RESULT of the search, not the action of searching.
- If results have NOT loaded yet (page looks the same as before), call `wait_for_ui` with a reason like "waiting for search results to load".

## Hard Rules:
- ONLY use target_id values from the DOM list. NEVER invent IDs.
- If the user asked a question (what/how/why/which), ALWAYS check readable content first.
- If readable content answers the goal, call complete_mission with the answer text.
- Be concise and decisive. Fewer actions = better.

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
    tool result messages.
    """
    dom_result_indices = []
    for i, msg in enumerate(messages):
        content = msg.get("content") or ""
        if msg.get("role") == "tool" and ("[ID:" in content or "Page Content" in content):
            dom_result_indices.append(i)
    # Keep the last TWO DOM injections, summarize older ones so agent remembers failures
    if len(dom_result_indices) > 2:
        for idx in dom_result_indices[:-2]:
            messages[idx]["content"] = "(previous UI state pruned to save tokens, but remember you already explored this state)"
    return messages


def _goal_focus_terms(main_goal: str) -> Tuple[set[str], set[str], set[str]]:
    """
    Split goal into entity/qualifier focus:
    - entity: before "in/with/wearing/on/from"
    - qualifier: after split marker
    Returns (entity_terms, qualifier_terms, all_terms)
    """
    goal = (main_goal or "").strip().lower()
    if not goal:
        return set(), set(), set()
    goal = re.sub(
        r"^(show me|find me|find|show|open|click|select|choose|go to|navigate to|take me to)\s+",
        "",
        goal,
    )
    parts = re.split(r"\b(?:in|with|wearing|on|from)\b", goal, maxsplit=1)
    entity_part = parts[0] if parts else goal
    qualifier_part = parts[1] if len(parts) > 1 else ""

    stop = {
        "click", "open", "select", "navigate", "link", "button", "page", "tab",
        "the", "and", "for", "with", "show", "find", "get", "see", "profile",
    }
    entity_terms = {t for t in re.findall(r"[a-z0-9]+", entity_part) if len(t) > 2 and t not in stop}
    qualifier_terms = {t for t in re.findall(r"[a-z0-9]+", qualifier_part) if len(t) > 2 and t not in stop}
    all_terms = set(entity_terms) | set(qualifier_terms)
    return entity_terms, qualifier_terms, all_terms


def _score_evidence_relevance(main_goal: str, nodes: list) -> Tuple[int, str, bool]:
    """Score goal relevance in readable content. Returns (hit_count, best_excerpt, has_entity_evidence)."""
    entity_terms, qualifier_terms, goal_terms = _goal_focus_terms(main_goal)
    if not goal_terms and not entity_terms:
        return 0, "", False

    readable = [
        n for n in nodes
        if getattr(n, "text", None)
        and len((getattr(n, "text", "") or "").strip()) >= 8
        and getattr(n, "zone", "") in {"main", "content"}
    ]
    best_excerpt = ""
    best_hits = 0
    total_hits = 0
    has_entity_evidence = False
    for n in readable:
        txt = (getattr(n, "text", "") or "").strip()
        tokens = set(re.findall(r"[a-z0-9]+", txt.lower()))
        entity_hits = len(tokens & entity_terms) if entity_terms else 0
        qualifier_hits = len(tokens & qualifier_terms) if qualifier_terms else 0
        base_hits = len(tokens & goal_terms)

        # For person/entity goals, don't treat generic qualifier-only matches as "strong evidence".
        if entity_terms and entity_hits == 0:
            hits = 0
        else:
            hits = base_hits
            if entity_hits > 0:
                hits += 2
                has_entity_evidence = True
            if qualifier_terms and qualifier_hits > 0:
                hits += 1

        if hits > 0:
            total_hits += hits
        if hits > best_hits:
            best_hits = hits
            best_excerpt = txt[:300]
    return total_hits, best_excerpt, has_entity_evidence


def _build_mission_state_context(
    mission: Any,
    schema: Any,
    current_url: str = "",
    excluded_ids: Optional[set] = None,
) -> str:
    """
    Build a compact, human-readable state context block.
    Injected into both the Last-Mile LLM prompt and Vision prompt so that
    both tools have full awareness of:
      - What the original user goal is
      - Which subgoal is currently active
      - Which subgoals have already been completed
      - What the last 3 successful actions were
      - Which IDs / nav sections to avoid repeating
    """
    lines: List[str] = ["## ─── Mission State Context ───"]

    # ── User goal vs main_goal ───────────────────────────────────────────────
    main_goal = (
        getattr(mission, "main_goal", "")
        or getattr(schema, "raw_utterance", "")
        or getattr(schema, "target_entity", "")
        or "unknown"
    )
    lines.append(f"**Original user goal:** {main_goal}")

    # ── Subgoal progress ─────────────────────────────────────────────────────
    subgoals: List[str] = list(getattr(mission, "subgoals", []) or [])
    idx: int = int(getattr(mission, "current_subgoal_index", 0) or 0)
    if subgoals:
        completed = subgoals[:idx]
        current = subgoals[idx] if idx < len(subgoals) else "(final — last-mile)"
        pending = subgoals[idx + 1:] if idx + 1 < len(subgoals) else []

        if completed:
            lines.append(
                "**Completed subgoals:** "
                + " → ".join([f"[{i+1}] {s}" for i, s in enumerate(completed)])
            )
        lines.append(f"**Current subgoal ({idx + 1}/{len(subgoals)}):** {current}")
        if pending:
            lines.append(
                "**Remaining subgoals:** "
                + " → ".join([f"[{idx+2+i}] {s}" for i, s in enumerate(pending)])
            )
    else:
        lines.append("**Subgoals:** none (zero-shot last-mile)")

    # ── Current URL + inferred active section ────────────────────────────────
    lines.append(f"**Current URL:** {current_url or getattr(mission, 'last_url', '') or 'unknown'}")

    # ── Last 3 successful actions from mission history ────────────────────────
    action_history: List[str] = list(getattr(mission, "action_history", []) or [])
    recent = [a for a in action_history if a][-5:]
    if recent:
        recent_str = "; ".join(recent[-3:])
        lines.append(f"**Last 3 actions:** {recent_str}")
    else:
        lines.append("**Last 3 actions:** none yet")

    # ── Already-clicked / blocked IDs ────────────────────────────────────────
    excl = sorted(excluded_ids or set())
    if excl:
        lines.append(f"**DO NOT click again (already visited):** {', '.join(excl)}")
    else:
        lines.append("**Already-clicked IDs:** none")

    # ── Visited URLs (do-not-repeat navigation guard) ────────────────────────
    visited: List[str] = list(getattr(mission, "visited_urls", []) or [])
    if len(visited) > 1:
        # Show last 3; first is usually the starting page
        lines.append(f"**Recently visited URLs:** {' → '.join(visited[-3:])}")

    # ── Map-Aware Context Injection ──────────────────────────────────────────
    try:
        from visual_copilot.mission.index_traverser import _find_current_node, _normalize_path
        import os, json
        from urllib.parse import urlparse
        site_map_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "site_map.json")
        if os.path.exists(site_map_path):
            with open(site_map_path, "r", encoding="utf-8") as f:
                site_map_data = json.load(f)
            root = site_map_data.get("root")
            if root:
                parsed = urlparse(current_url if "://" in current_url else f"http://{current_url}")
                path = _normalize_path(parsed.path)
                current_node = _find_current_node(root, path)
                if current_node:
                    lines.append(f"**Logical Map Node:** {current_node.get('title', 'Unknown')} ({current_node.get('node_id', '')})")
                    expected = current_node.get("expected_controls", [])
                    if expected:
                        lines.append(f"**Expected Controls:** {', '.join(expected)}")
                    terminal = current_node.get("terminal_capabilities", [])
                    if terminal:
                        lines.append(f"**Terminal Capabilities:** {', '.join(terminal)}")
                    lines.append("**Logical Verification Rule:** Before calling complete_mission, verify you have landed on the correct logical node defined in the map.")
                    # Backtracking rule
                    lines.append("**Backtracking Rule:** If you are at a leaf node but cannot find the answer, you must backtrack to the parent node to try a different branch instead of halting.")
    except Exception as e:
        pass

    lines.append("## ─────────────────────────────────")
    return "\n".join(lines)


async def run_compound_last_mile(
    *,
    schema: Any,
    mission: Any,
    nodes: list,
    app: Any = None,
    screenshot_b64: str = "",
    session_id: str = "",
    excluded_ids: set = None,
    current_url: str = "",
    goal_url: str = "",
    user_goal: str = "",
    force_vision_bootstrap: bool = True,
) -> Dict[str, Any]:
    """
    Compound Agentic Last-Mile Loop with State Machine.

    Enforces a strict Read → Verify → Act loop with:
    - Read-first mandate (reject clicks when evidence already sufficient)
    - Semantic rabbit-hole guardrails (block concept-neighbor drift)
    - Progress-aware controls (hard-stop on stall)
    - Policy-triggered vision fallback (not default)
    - Deterministic exit reasons

    Returns a dict with keys:
      action: dict describing the browser action or answer
      thought: str of the LLM's last reasoning
      iterations: int of internal loop turns used
      status: "action" | "complete" | "impossible" | "max_iterations" | "stuck_no_progress"
      diagnostics: optional LastMileState diagnostic info
    """
    from visual_copilot.mission.last_mile_tools import LAST_MILE_TOOLS
    from visual_copilot.mission.tool_executor import execute_internal_tool

    main_goal = (
        getattr(mission, "main_goal", "")
        or getattr(schema, "raw_utterance", "")
        or getattr(schema, "target_entity", "")
    )
    user_goal_text = (user_goal or getattr(schema, "raw_utterance", "") or main_goal or "").strip()
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

    # ── Build compact mission state context block ─────────────────────────────
    mission_ctx = _build_mission_state_context(
        mission=mission,
        schema=schema,
        current_url=current_url,
        excluded_ids=excluded_ids,
    )

    # ── Initialize State Machine ──
    state = LastMileState()
    target_node = _get_page_index_node(current_url)
    target_node_ctx = f"You are currently at: {target_node.get('title', 'Unknown')} ({target_node.get('url', 'Unknown')})" if target_node else "Current node unknown."

    initial_evidence_hits, initial_best_excerpt, has_entity_evidence = _score_evidence_relevance(main_goal, nodes)
    state.evidence_hits = initial_evidence_hits
    state.progress_score = float(initial_evidence_hits)
    state.best_progress_score = state.progress_score

    logger.info(
        f"LAST_MILE_PHASE phase={state.phase.value} "
        f"initial_evidence_hits={initial_evidence_hits} "
        f"goal='{main_goal[:80]}' target_node={target_node.get('title') if target_node else 'None'}"
    )

    # ── Read-First Check: If evidence is already strong, push toward completion ──
    evidence_hint = ""
    if (
        LAST_MILE_READ_FIRST_ENFORCED
        and initial_evidence_hits >= EVIDENCE_RELEVANCE_THRESHOLD
        and (has_entity_evidence or not _goal_focus_terms(main_goal)[0])
    ):
        evidence_hint = (
            f"\n\n**⚠️ STRONG EVIDENCE ALREADY VISIBLE** (relevance_score={initial_evidence_hits}):\n"
            f"The page content already contains information relevant to your goal. "
            f"Best excerpt: \"{initial_best_excerpt[:200]}\"\n"
            f"**You SHOULD call complete_mission with this evidence rather than clicking more links.**\n"
        )
        logger.info(
            f"LAST_MILE_READ_FIRST_HINT evidence_hits={initial_evidence_hits} "
            f"excerpt='{initial_best_excerpt[:100]}'"
        )

    messages = [
        {"role": "system", "content": COMPOUND_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{mission_ctx}\n\n"
                f"## Mission Brief\n"
                f"**Main Goal:** {main_goal}\n"
                f"**Target Entity:** {target_entity}\n"
                f"**Action Type:** {action_type_str}\n"
                f"**Goal URL (if known):** {goal_url or 'unknown'}\n"
                f"**Page Index:** {target_node_ctx}\n\n"
                f"{excluded_str}\n"
                f"**Current Interactable DOM:**\n{compressed_dom}\n\n"
                f"**Current Readable Page Content:**\n{readable_content}\n"
                f"{evidence_hint}\n"
                f"Based on the Mission State Context above, you know which subgoals are done.\n"
                f"Do NOT re-suggest already-completed navigation steps.\n"
                f"Begin by analyzing the readable content. "
                f"State your goal_status (found/partial/not_found) FIRST, then decide your action."
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

    # ── Forced Vision Bootstrap: only when stage explicitly requests it ──
    # This provides visual grounding before any click/type decisions.
    force_action_after_vision_bootstrap = False
    if force_vision_bootstrap:
        try:
            logger.info("👁️ LAST_MILE_FORCE_VISION_BOOTSTRAP start")
            _, vision_tool_result, _ = await execute_internal_tool(
                tool_name="request_vision",
                args={
                    "reason": (
                        f"Bootstrap last-mile visual grounding for goal '{main_goal}'.\n"
                        f"{mission_ctx}\n"
                        "Given the mission state above, identify the best visible target on the current page, "
                        "missing evidence, and the most likely next action. "
                        "Do NOT suggest navigation to sections already listed as completed subgoals."
                    ),
                    "_current_url": current_url,
                    "_goal_url": goal_url,
                    "_user_goal": user_goal_text,
                    "_already_clicked_ids": sorted(excluded_ids),
                },
                nodes=nodes,
                screenshot_b64=screenshot_b64 or None,
                app=app,
                session_id=session_id,
                excluded_ids=excluded_ids,
            )
            state.vision_used = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "**VISION BOOTSTRAP (MANDATORY, SYSTEM-INJECTED)**\n"
                        "Use this visual observation as primary evidence for your next tool choice:\n\n"
                        f"{vision_tool_result}"
                    ),
                }
            )
            force_action_after_vision_bootstrap = True
            logger.info("👁️ LAST_MILE_FORCE_VISION_BOOTSTRAP done")
        except Exception as vision_bootstrap_err:
            logger.warning(f"⚠️ LAST_MILE_FORCE_VISION_BOOTSTRAP failed: {vision_bootstrap_err}")

    # ── Internal Compound Loop ──
    stagnancy_counter = 0
    last_dom_hash = current_dom_hash
    last_thought = ""
    forbid_scroll_after_blocked_click = False

    for iteration in range(1, COMPOUND_MAX_INTERNAL_ITERATIONS + 1):
        state.iteration = iteration
        state.phase = LastMilePhase.READ_EVIDENCE

        # ── Progress-Aware Hard Stop ──
        if state.should_hard_stop():
            state.exit_reason = "stuck_no_progress"
            logger.warning(
                f"🛡️ LAST_MILE_PROGRESS_SCORE hard_stop iter={iteration} "
                f"stall_count={state.stall_count} evidence_miss_streak={state.evidence_miss_streak}"
            )
            # Try vision once before giving up
            if not state.vision_used and LAST_MILE_VISION_POLICY_TRIGGER:
                state.vision_used = True
                logger.info("LAST_MILE_RABBIT_HOLE_GUARD: forcing vision before hard-stop")
                messages.append({
                    "role": "user",
                    "content": (
                        "**SYSTEM OVERRIDE**: No progress detected after multiple actions. "
                        "The system is requesting a visual scan. After reviewing the vision result, "
                        "you MUST either complete_mission with whatever evidence exists, or acknowledge stuck state."
                    ),
                })
                # Don't hard-stop yet, give one more iteration with vision nudge
                state.stall_count = PROGRESS_STALL_MAX - 1
            else:
                return {
                    "action": {
                        "type": "clarify",
                        "speech": f"I've been unable to make progress on '{main_goal}'. The page content doesn't seem to have what I need.",
                    },
                    "thought": f"Hard-stop: {state.stall_count} actions without progress improvement.",
                    "iterations": iteration,
                    "status": "stuck_no_progress",
                    "diagnostics": {"last_mile_state": state.to_diagnostic()},
                }

        state.phase = LastMilePhase.DECIDE_ACTION

        try:
            message = await llm.generate_with_tools(
                messages=messages,
                tools=LAST_MILE_TOOLS,
                model="openai/gpt-oss-20b",
                tool_choice="auto",
                max_tokens=1024,
                temperature=0.1,
            )
        except Exception as e:
            logger.error(f"🔴 Compound LLM call failed at iteration {iteration}: {e}")
            state.exit_reason = "llm_error"
            return {
                "action": {"type": "scroll", "speech": "Internal reasoning error, scrolling to retry."},
                "thought": f"LLM error: {e}",
                "iterations": iteration,
                "status": "action",
                "diagnostics": {"last_mile_state": state.to_diagnostic()},
            }

        # Extract content for logging
        content = getattr(message, "content", None) or ""
        tool_calls = getattr(message, "tool_calls", None)
        last_thought = content

        if content:
            logger.info(f"🧠 COMPOUND iter={iteration} thought: {content[:300]}")

        # No tool calls → model returned text without using a tool.
        # This is the "narrative completion" trap — the LLM writes an essay
        # instead of calling complete_mission. Re-prompt ONCE to force a tool call.
        if not tool_calls:
            if not getattr(state, '_no_tool_retry_done', False):
                state._no_tool_retry_done = True
                logger.warning(
                    f"🛡️ COMPOUND NO_TOOL_CALLS iter={iteration} — "
                    f"re-prompting with forced tool_choice. Text: {content[:150]}"
                )
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "**SYSTEM OVERRIDE**: You returned text without calling any tool. "
                        "This is NOT allowed. You MUST call a tool every turn.\n\n"
                        "If you believe the goal is answered, call `complete_mission` with "
                        "the SPECIFIC data extracted from the page (not a narrative summary).\n"
                        "If the goal is NOT answered yet, call an action tool "
                        "(click_element, type_text, scroll_page, read_page_content).\n\n"
                        "DO NOT respond with text only. You MUST call a tool NOW."
                    ),
                })
                # Retry with forced tool_choice
                try:
                    message = await llm.generate_with_tools(
                        messages=messages,
                        tools=LAST_MILE_TOOLS,
                        model="openai/gpt-oss-20b",
                        tool_choice="required",
                        max_tokens=1024,
                        temperature=0.1,
                    )
                    content = getattr(message, "content", None) or ""
                    tool_calls = getattr(message, "tool_calls", None)
                    last_thought = content
                    if content:
                        logger.info(f"🧠 COMPOUND retry iter={iteration} thought: {content[:300]}")
                except Exception as retry_err:
                    logger.error(f"🔴 Compound retry failed: {retry_err}")
                    tool_calls = None  # Fall through to exit below

            if not tool_calls:
                fallback_action = _fallback_action_from_vision_context(
                    messages=messages,
                    nodes=nodes,
                    excluded_ids=excluded_ids,
                )
                if fallback_action:
                    state.exit_reason = "no_tool_calls_fallback_action"
                    logger.warning(
                        f"🛡️ COMPOUND NO_TOOL_CALLS fallback selected target={fallback_action.get('target_id')}"
                    )
                    return {
                        "action": [fallback_action, {"type": "wait", "seconds": 2, "speech": "Waiting briefly for the page to update."}],
                        "thought": content or "No tool call produced; used validated vision fallback action.",
                        "iterations": iteration,
                        "status": "action",
                        "diagnostics": {"last_mile_state": state.to_diagnostic()},
                    }
                state.exit_reason = "no_tool_calls"
                state.phase = LastMilePhase.DECIDE_ACTION
                logger.info(f"🏁 COMPOUND EXIT iter={iteration} reason=no_tool_calls (after retry)")
                return {
                    "action": {
                        "type": "clarify",
                        "speech": (
                            f"I still need one concrete interaction step to finish '{main_goal}'. "
                            "Please let me continue."
                        ),
                    },
                    "thought": content or "No tool call produced after forced retry.",
                    "iterations": iteration,
                    "status": "stuck_no_progress",
                    "diagnostics": {"last_mile_state": state.to_diagnostic()},
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

        state.phase = LastMilePhase.EXECUTE_ACTION

        # Process each tool call
        for tc in tool_calls:
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                tool_args = {}

            logger.info(f"⚙️ COMPOUND TOOL iter={iteration} name={tool_name} args={tool_args}")

            # ── Post-Vision Bootstrap Execution Gate ──
            # After forced vision bootstrap, first turn should check if vision suggested actions.
            # If vision provided an Action Plan, EXECUTE IT instead of immediately completing.
            if (
                force_action_after_vision_bootstrap
                and iteration == 1
                and tool_name == "complete_mission"
            ):
                # Check if vision provided actionable suggestions that were ignored
                vision_brief_text = ""
                for msg in reversed(messages[:-1]):  # Exclude current tool result
                    if msg.get("role") == "user" and "Vision Strategic Brief" in str(msg.get("content", "")):
                        vision_brief_text = str(msg.get("content", ""))
                        break
                
                has_vision_actions = "Vision Action Plan" in vision_brief_text or "Next steps:" in vision_brief_text
                
                # Only block completion if vision provided specific clickable actions that were ignored
                if has_vision_actions and state.evidence_hits < EVIDENCE_RELEVANCE_THRESHOLD:
                    logger.warning(
                        "LAST_MILE_VISION_BOOTSTRAP_GATE blocked complete_mission at iter=1; "
                        "vision provided action plan that was ignored"
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": (
                            "BLOCKED: Vision bootstrap provided specific action suggestions that you ignored. "
                            "Review the Vision Strategic Brief and execute the suggested actions FIRST, "
                            "then call complete_mission with comprehensive evidence from the updated page."
                        ),
                    })
                    state.record_action("complete_blocked:vision_actions_ignored")
                    continue
                else:
                    # Vision didn't provide actions OR evidence is already strong - allow completion
                    logger.info("LAST_MILE_VISION_BOOTSTRAP_GATE allowing completion (no vision actions or strong evidence)")

            # ── Read-First Enforcement: Reject clicks when evidence is strong ──
            # This gate should ONLY block clicks, NOT completions
            if (
                LAST_MILE_READ_FIRST_ENFORCED
                and tool_name == "click_element"  # Only block CLICKS, not completions
                and iteration == 1
                and initial_evidence_hits >= EVIDENCE_RELEVANCE_THRESHOLD
            ):
                why = tool_args.get("why", "")
                # Check if the LLM acknowledged the evidence and has a good reason to click
                has_justification = any(kw in why.lower() for kw in [
                    "insufficient", "missing", "not found", "need more", "partial",
                    "doesn't contain", "not enough", "incomplete", "only list",
                    "placeholder", "not showing", "bio page", "profile page",
                    "thumbnail", "index", "navigate to", "click to see", "expand",
                    "deep link", "listing only", "not the image", "results only",
                    "does not contain", "does not show", "not on the current page",
                    "entity not present", "target not present"
                ])
                if not has_justification:
                    logger.warning(
                        f"LAST_MILE_COMPLETION_GATE BLOCKED click at iter=1 "
                        f"(evidence_hits={initial_evidence_hits}, no justification)"
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": (
                            f"BLOCKED: The page already contains strong evidence (relevance={initial_evidence_hits}). "
                            f"Best excerpt: \"{initial_best_excerpt[:200]}\". "
                            f"You must call complete_mission with this evidence, or provide a specific reason "
                            f"why this evidence does NOT answer the goal '{main_goal}'."
                        ),
                    })
                    state.record_action(f"click_blocked:read_first")
                    continue

            # ── Semantic Repeat Guard: Block concept-neighbor drift ──
            if (
                LAST_MILE_SEMANTIC_GUARD
                and tool_name == "click_element"
            ):
                click_label = tool_args.get("why", "") + " " + str(tool_args.get("target_id", ""))
                # Look up text of the target node for better label matching
                target_id = tool_args.get("target_id", "")
                target_node = next(
                    (n for n in nodes if getattr(n, "id", "") == target_id),
                    None,
                )
                if target_node:
                    click_label += " " + (getattr(target_node, "text", "") or "")

                if state.is_semantic_repeat(click_label):
                    state.semantic_repeat_streak += 1
                    logger.warning(
                        f"LAST_MILE_RABBIT_HOLE_GUARD BLOCKED concept-neighbor click "
                        f"iter={iteration} streak={state.semantic_repeat_streak} "
                        f"label='{click_label[:80]}'"
                    )

                    # Check if vision should be triggered
                    if state.should_trigger_vision():
                        state.vision_used = True
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": (
                                "BLOCKED: You are clicking concept-neighbor links repeatedly without progress. "
                                "The system is now requesting a visual scan. Call request_vision to see the page, "
                                "then either complete_mission or acknowledge you are stuck."
                            ),
                        })
                    else:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": (
                                f"BLOCKED: This click appears to be a concept-neighbor of your previous clicks "
                                f"(semantic_repeat_streak={state.semantic_repeat_streak}). "
                                f"Stop clicking related links. Instead, read the CURRENT page content "
                                f"and call complete_mission with whatever evidence exists, OR try a completely "
                                f"different approach (type_text to search, scroll to find new content)."
                            ),
                        })
                    state.record_action(f"click_blocked:semantic_repeat")
                    continue
                else:
                    state.record_click_label(click_label)
                    # Reset semantic streak on non-similar click
                    state.semantic_repeat_streak = 0

            # ── Blocked-click recovery: do not allow immediate scroll fallback ──
            if forbid_scroll_after_blocked_click and tool_name == "scroll_page":
                logger.warning(
                    "LAST_MILE_BLOCK_RECOVERY_GUARD blocked scroll after blocked click; "
                    "forcing read/re-target"
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (
                        "BLOCKED: Your previous click was rejected by a guardrail. "
                        "You cannot use scroll_page as the immediate fallback. "
                        "Use read_page_content to identify an in-page filter/tab/dropdown/date-range control, "
                        "then click that target."
                    ),
                })
                state.record_action("scroll_blocked:after_click_reject")
                continue

            is_terminal, tool_result_str, frontend_action = await execute_internal_tool(
                tool_name=tool_name,
                args={
                    **tool_args,
                    "_main_goal": main_goal,
                    "_user_goal": user_goal_text,
                    "_last_mile_iteration": iteration,
                    "_schema_action": str(getattr(getattr(schema, "action", None), "value", getattr(schema, "action", ""))),
                    "_current_url": current_url,
                    "_goal_url": goal_url,
                    "_already_clicked_ids": sorted(excluded_ids),
                    "_mission_ctx": mission_ctx,  # full state context for vision prompt
                    "_target_entity": target_entity, # pass target_entity for verification
                },
                nodes=nodes,
                screenshot_b64=screenshot_b64 or None,
                app=app,
                session_id=session_id,
                excluded_ids=excluded_ids,
            )


            if (
                tool_name == "click_element"
                and not is_terminal
                and isinstance(tool_result_str, str)
                and tool_result_str.strip().startswith("REJECTED:")
            ):
                forbid_scroll_after_blocked_click = True
            elif tool_name in {"read_page_content", "type_text", "request_vision"}:
                forbid_scroll_after_blocked_click = False

            if is_terminal and frontend_action:
                action_type = frontend_action.get("type", "")
                state.phase = LastMilePhase.COMPLETE if action_type == "answer" else LastMilePhase.EXECUTE_ACTION
                state.record_action(f"{tool_name}:{action_type}")

                # --- BUNDLE QUEUED ACTIONS ---
                bundled_action = frontend_action
                if state.action_pipeline:
                    bundled_action = state.action_pipeline + [frontend_action]
                    state.action_pipeline = []

                if action_type == "answer":
                    status = frontend_action.get("status", "success")
                    state.exit_reason = "completed" if status == "success" else "impossible"

                    # ── Completion Gate: Verify answer has evidence ──
                    answer_text = frontend_action.get("speech", "") or frontend_action.get("text", "")
                    if status == "success" and len(answer_text.strip()) < 10:
                        logger.warning(
                            f"LAST_MILE_COMPLETION_GATE weak answer (len={len(answer_text)}), "
                            f"attempting evidence rescue"
                        )
                        # Try to strengthen the answer with visible evidence
                        _, best_rescue, _ = _score_evidence_relevance(main_goal, nodes)
                        if best_rescue and len(best_rescue) > len(answer_text):
                            frontend_action["speech"] = best_rescue[:500]
                            frontend_action["text"] = best_rescue[:500]

                    return {
                        "action": bundled_action,
                        "thought": last_thought,
                        "iterations": iteration,
                        "status": "complete" if status == "success" else "impossible",
                        "diagnostics": {"last_mile_state": state.to_diagnostic()},
                    }

                # For wait actions returned as terminal, attach bundled items and return to frontend
                logger.info(
                    f"🎯 COMPOUND ACTION iter={iteration} type={action_type} "
                    f"target={frontend_action.get('target_id', 'N/A')}"
                )

                state.exit_reason = f"action:{action_type}"
                return {
                    "action": bundled_action,
                    "thought": last_thought,
                    "iterations": iteration,
                    "status": "action",
                    "diagnostics": {"last_mile_state": state.to_diagnostic()},
                }

            # Non-terminal: feed the tool result back into the conversation
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": tool_result_str}
            )
            
            # If it's a physical action returning False, queue it!
            if not is_terminal and frontend_action and tool_name in {"click_element", "type_text", "scroll_page"}:
                state.action_pipeline.append(frontend_action)
                state.record_action(f"{tool_name}:queued")
                
                # Auto-inject wait if search
                if tool_name == "type_text" and tool_args.get("press_enter", False):
                    state.action_pipeline.append({"type": "wait", "seconds": 2})
            else:
                state.record_action(f"{tool_name}:non_terminal")

        # ── Verify Progress Phase ──
        state.phase = LastMilePhase.VERIFY_PROGRESS
        new_evidence_hits, _, _ = _score_evidence_relevance(main_goal, nodes)
        state.update_progress(new_evidence_hits)

        logger.info(
            f"LAST_MILE_PROGRESS_SCORE iter={iteration} "
            f"evidence_hits={state.evidence_hits} miss_streak={state.evidence_miss_streak} "
            f"stall_count={state.stall_count} semantic_repeat={state.semantic_repeat_streak}"
        )

        # ── Early Vision Escalation: Trigger when evidence is weak after 2+ misses ──
        if (
            LAST_MILE_VISION_POLICY_TRIGGER
            and not state.vision_used
            and state.evidence_miss_streak >= 2
            and state.evidence_hits < EVIDENCE_RELEVANCE_THRESHOLD
        ):
            state.vision_used = True
            logger.info(
                f"👁️ LAST_MILE_VISION_ESCALATION iter={iteration} "
                f"miss_streak={state.evidence_miss_streak} evidence_hits={state.evidence_hits} "
                f"→ injecting vision request"
            )
            messages.append({
                "role": "user",
                "content": (
                    "**SYSTEM OVERRIDE — VISION ESCALATION**: You have failed to find relevant evidence "
                    "after multiple iterations. The text DOM may not contain what you need. "
                    "Call request_vision NOW to visually scan the page for the correct data, "
                    "tabs, toggles, or UI elements that might reveal the answer. "
                    "After reviewing the vision result, either complete_mission with the correct answer "
                    "or try a different interaction (click a tab/toggle, type in a search field)."
                ),
            })

        # ── Stagnancy Guard ──
        # Don't trigger stagnancy if we already have strong evidence - the answer is HERE
        new_dom_hash = _dom_signature_hash(nodes)
        if new_dom_hash == last_dom_hash:
            # Only increment stagnancy if evidence is weak
            if state.evidence_hits < EVIDENCE_RELEVANCE_THRESHOLD:
                stagnancy_counter += 1
            else:
                stagnancy_counter = 0  # Reset - we have evidence, just need to extract it
        else:
            stagnancy_counter = 0
            last_dom_hash = new_dom_hash

        if stagnancy_counter >= COMPOUND_STAGNANCY_THRESHOLD:
            # Before giving up, check if we have strong evidence that wasn't extracted
            if state.evidence_hits >= EVIDENCE_RELEVANCE_THRESHOLD:
                logger.warning(
                    f"🛡️ COMPOUND STAGNANCY OVERRIDE: Have strong evidence (hits={state.evidence_hits}) "
                    "but LLM failed to extract — forcing evidence rescue"
                )
                # Force one more iteration with explicit extraction instruction
                messages.append({
                    "role": "user",
                    "content": (
                        "**SYSTEM OVERRIDE**: You have been stuck but the page CONTAINS the answer "
                        f"(evidence_hits={state.evidence_hits}).\n\n"
                        "Your task is SIMPLE:\n"
                        "1. Look at the 'Current Readable Page Content' section above.\n"
                        "2. Find sentences that directly answer: {main_goal}\n"
                        "3. Call complete_mission with those exact sentences as your response.\n\n"
                        "DO NOT click anything. DO NOT scroll. JUST EXTRACT THE ANSWER."
                    ),
                })
                stagnancy_counter = 0  # Reset counter for extraction attempt
                continue
            else:
                state.exit_reason = "stagnancy"
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
                    "diagnostics": {"last_mile_state": state.to_diagnostic()},
                }

        # ── Context Pruning & Semantic Memory ──
        if tool_calls:
            actions_summary = ", ".join([tc.function.name for tc in tool_calls])
            state.semantic_summaries.append(f"Turn {iteration}: Planned {actions_summary}. Best excerpt: {initial_best_excerpt[:50]}")
            # Instead of completely pruning DOM, we retain a semantic summary trace
            messages.append({"role": "system", "content": f"Semantic Summary: {state.semantic_summaries[-1]}"})
        messages = _prune_old_dom_context(messages)

    # Max iterations reached
    state.exit_reason = "max_iterations"
    logger.warning(f"⏱️ COMPOUND MAX_ITER reached ({COMPOUND_MAX_INTERNAL_ITERATIONS})")
    return {
        "action": {
            "type": "clarify",
            "speech": f"I spent {COMPOUND_MAX_INTERNAL_ITERATIONS} reasoning steps on '{main_goal}' but couldn't complete it. Would you like me to try a different approach?",
        },
        "thought": last_thought or "Max iterations reached without resolution.",
        "iterations": COMPOUND_MAX_INTERNAL_ITERATIONS,
        "status": "max_iterations",
        "diagnostics": {"last_mile_state": state.to_diagnostic()},
    }

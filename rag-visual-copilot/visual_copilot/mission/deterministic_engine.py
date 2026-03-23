"""
Deterministic Last-Mile Engine for Known Sites — Subgoal Injection.

Instead of doing its own DOM grounding, this engine:
  1. ONE 8b call to reason over site_map tree → produces control sequence
  2. Converts controls into subgoals: ["Click Model Filter", "Select Whisper", ...]
  3. Injects them into the mission BEFORE the LAST_MILE subgoal
  4. The existing mission stage + keyword router executes them through the proven path

The compound last_mile is the last_hope — only for reading results after all controls
are clicked, or when this engine can't resolve.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Goal Parameter Parser (one 8b call, reasons over site_map tree only)
# ═══════════════════════════════════════════════════════════════════════

def _build_goal_parse_prompt(
    goal: str,
    node: Dict[str, Any],
    parent_node: Optional[Dict[str, Any]] = None,
) -> str:
    """Build prompt for 8b to reason over site_map tree knowledge.

    The LLM reads ONLY the site_map node structure (summaries, controls,
    capabilities, children) — NOT the live DOM.
    """
    node_title = node.get("title", "unknown")
    summary = node.get("summary_of_contents", "")
    expected_controls = node.get("expected_controls", [])
    terminal_capabilities = node.get("terminal_capabilities", [])
    children = node.get("children", [])

    tree_ctx = f"CURRENT PAGE: {node_title}\n"
    tree_ctx += f"  Summary: {summary}\n"
    tree_ctx += f"  Controls: {', '.join(expected_controls)}\n"
    tree_ctx += f"  Capabilities: {', '.join(terminal_capabilities)}\n"

    if children:
        tree_ctx += f"\n  CHILD PAGES (can navigate deeper):\n"
        for child in children:
            child_title = child.get("title", "?")
            child_summary = (child.get("summary_of_contents", "") or "")[:100]
            child_controls = child.get("expected_controls", [])
            child_caps = child.get("terminal_capabilities", [])
            tree_ctx += f"  - {child_title}: {child_summary}\n"
            if child_controls:
                tree_ctx += f"    Controls: {', '.join(child_controls)}\n"
            if child_caps:
                tree_ctx += f"    Capabilities: {', '.join(child_caps[:4])}\n"

    if parent_node:
        parent_title = parent_node.get("title", "?")
        siblings = [
            c.get("title", "?") for c in parent_node.get("children", [])
            if c.get("node_id") != node.get("node_id")
        ]
        tree_ctx += f"\n  PARENT PAGE: {parent_title}\n"
        if siblings:
            tree_ctx += f"  SIBLING PAGES: {', '.join(siblings)}\n"

    return f"""You are a site navigation reasoner. Read the page tree below and decide what controls to use.

USER GOAL: "{goal}"

SITE MAP:
{tree_ctx}

TASK: Map the user's goal to a sequence of control interactions on this page.

Rules:
- Time references ("last 7 days", "this month") → Date Picker or Date Range control
- Model/entity names ("whisper", "llama") → Model Filter or entity selector
- View types ("activity", "cost", "tokens") → Tab controls
- Simple actions ("create", "delete") → Action buttons
- If this page doesn't have the right capability, say navigate_to a child/sibling page

Reply in strict JSON:
{{
  "control_sequence": [
    {{"control": "exact name from Controls list", "value": "what to select/click", "reason": "why"}}
  ],
  "navigate_to": null,
  "reasoning": "brief explanation"
}}

If navigation to another page is needed:
{{
  "control_sequence": [],
  "navigate_to": "page title from CHILD PAGES or SIBLING PAGES",
  "reasoning": "why navigate there"
}}"""


async def parse_goal_to_controls(
    goal: str,
    current_node: Dict[str, Any],
    parent_node: Optional[Dict[str, Any]],
    llm: Any,
) -> Optional[Dict[str, Any]]:
    """One 8b call to reason over site_map tree. Returns parsed plan or None."""
    prompt = _build_goal_parse_prompt(goal, current_node, parent_node)

    try:
        raw = await llm.generate(
            prompt,
            model="llama-3.1-8b-instant",
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            logger.warning(f"⚠️ [GRAPH_WALKER] 8b parse failed: no JSON")
            return None

        parsed = json.loads(match.group())
        sequence = parsed.get("control_sequence", [])
        navigate_to = parsed.get("navigate_to")
        reasoning = parsed.get("reasoning", "")

        logger.info(
            f"🧠 [GRAPH_WALKER] goal_parsed controls={len(sequence)} "
            f"navigate_to={navigate_to or 'none'} "
            f"reason='{reasoning[:60]}'"
        )
        return parsed

    except Exception as e:
        logger.warning(f"⚠️ [GRAPH_WALKER] 8b call failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# Subgoal Injection — convert control sequence to mission subgoals
# ═══════════════════════════════════════════════════════════════════════

def controls_to_subgoals(control_sequence: List[Dict[str, str]]) -> List[str]:
    """Convert 8b control sequence to subgoal strings for the mission stage.

    Each control becomes a "Click X" or "Select X" subgoal that the existing
    keyword router can execute through the proven path.

    Example:
        [{"control": "Model Filter", "value": "Whisper"}, {"control": "Date Picker", "value": "Last 7 days"}]
        → ["Click Model Filter", "Select Whisper", "Click Date Picker", "Select Last 7 days"]
    """
    subgoals = []
    for step in control_sequence:
        control = step.get("control", "")
        value = step.get("value", "click")

        if not control:
            continue

        # Tag with [CTRL] prefix so the execution path uses site_map grounding
        # instead of the keyword router's strict threshold
        subgoals.append(f"[CTRL] Click {control}")

        # If there's a specific value to select (not just "click"), add a select step
        if value and value.lower() not in ("click", "press", "open", "toggle"):
            subgoals.append(f"[CTRL] Select {value}")

    return subgoals


# ═══════════════════════════════════════════════════════════════════════
# Simple Action Match (Phase 1, no LLM)
# ═══════════════════════════════════════════════════════════════════════

_ACTION_VERBS = frozenset({
    "create", "add", "delete", "remove", "click", "press",
    "open", "enable", "disable", "toggle", "make", "generate",
    "submit", "confirm", "start", "launch", "new",
})

_NOISE_WORDS = frozenset({
    "me", "my", "the", "a", "an", "to", "in", "on", "at", "of",
    "for", "with", "from", "please", "i", "want", "need", "can",
    "you", "this", "that", "is", "are", "it", "its", "show",
})


def try_simple_action_subgoal(
    goal: str,
    expected_controls: List[str],
    terminal_capabilities: List[str],
) -> Optional[str]:
    """Try to resolve simple action goals into a single subgoal string without LLM.

    Returns a subgoal like "Click Create API Key Button" or None.
    """
    words = re.findall(r"[a-z0-9]+", goal.lower())
    verb = ""
    for w in words:
        if w in _ACTION_VERBS:
            verb = w
            break
    if not verb:
        return None

    object_tokens = set(w for w in words if w not in _NOISE_WORDS and w != verb and len(w) > 1)
    if not object_tokens:
        return None

    # Score each expected_control
    _SYNS = {"add": {"create", "new"}, "create": {"add", "new"}, "delete": {"remove"}, "remove": {"delete"}}
    scored = []
    for control in expected_controls:
        control_tokens = set(re.findall(r"[a-z0-9]+", control.lower()))
        score = 0.0
        overlap = len(object_tokens & control_tokens)
        if overlap:
            score += overlap / max(len(object_tokens), len(control_tokens))
        if verb in control_tokens:
            score += 0.35
        if verb not in control_tokens:
            for syn in _SYNS.get(verb, set()):
                if syn in control_tokens:
                    score += 0.25
                    break
        if score > 0:
            scored.append((score, control))

    if not scored:
        return None

    scored.sort(key=lambda x: -x[0])
    best_score, best_control = scored[0]

    if best_score < 0.4:
        return None

    logger.info(f"🎯 [GRAPH_WALKER] simple_action control='{best_control}' score={best_score:.2f}")
    return f"[CTRL] Click {best_control}"


# ═══════════════════════════════════════════════════════════════════════
# Tree Helpers
# ═══════════════════════════════════════════════════════════════════════

def _find_parent_node(
    root: Dict[str, Any],
    target_node_id: str,
    parent: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Walk site_map tree to find parent of target node."""
    if root.get("node_id") == target_node_id:
        return parent
    for child in root.get("children", []):
        result = _find_parent_node(child, target_node_id, parent=root)
        if result is not None:
            return result
    return None


def _load_site_map_root() -> Optional[Dict[str, Any]]:
    """Load the site_map.json root node."""
    try:
        sm_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "site_map.json"
        )
        if os.path.exists(sm_path):
            with open(sm_path, "r") as f:
                return json.load(f).get("root")
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════

async def try_inject_control_subgoals(
    *,
    goal: str,
    current_url: str,
    mission: Any,
    mission_brain: Any,
    site_map_validator: Any,
    app: Any = None,
) -> Optional[str]:
    """Try to expand the LAST_MILE subgoal into concrete control subgoals.

    If successful, modifies mission.subgoals in-place by inserting control
    subgoals before the LAST_MILE entry. Returns the first new subgoal
    to execute, or None if expansion failed.

    This does NOT execute anything — it just plans. The existing mission
    stage + keyword router handles execution through the proven path.

    Args:
        goal: The LAST_MILE goal string (without prefix)
        current_url: Current page URL
        mission: MissionState object (modified in-place)
        mission_brain: MissionBrain for persistence
        site_map_validator: SiteMapValidator instance
        app: FastAPI app (for LLM access)

    Returns:
        First subgoal string to execute (e.g., "Click Model Filter"), or None.
    """
    if not site_map_validator or not goal:
        return None

    # Don't re-inject controls for "read result" subgoals — these should go to compound last_mile
    if "read result" in goal.lower():
        return None

    # ── Locate current site_map node ──
    current_node = site_map_validator.get_node_for_url(current_url)
    if not current_node:
        logger.debug(f"[GRAPH_WALKER] no site_map node for url={current_url[:60]}")
        return None

    node_id = current_node.get("node_id", "")
    node_title = current_node.get("title", "unknown")
    expected_controls = current_node.get("expected_controls", [])
    terminal_capabilities = current_node.get("terminal_capabilities", [])

    if not expected_controls:
        logger.debug(f"[GRAPH_WALKER] no expected_controls for page='{node_title}'")
        return None

    # ── Phase 1: Try simple action match (no LLM) ──
    simple_subgoal = try_simple_action_subgoal(goal, expected_controls, terminal_capabilities)
    if simple_subgoal:
        _inject_subgoals(mission, [simple_subgoal])
        await mission_brain._save_mission(mission)
        logger.info(f"⚡ [GRAPH_WALKER] injected 1 simple subgoal: '{simple_subgoal}'")
        return simple_subgoal

    # ── Phase 2: Reason over site_map tree with 8b ──
    if not app or not hasattr(app, "state") or not hasattr(getattr(app.state, "mind_reader", None), "llm"):
        logger.debug("[GRAPH_WALKER] no LLM available for goal parsing")
        return None

    # Find parent node for tree context
    root = _load_site_map_root()
    parent_node = _find_parent_node(root, node_id) if root else None

    parsed = await parse_goal_to_controls(
        goal=goal,
        current_node=current_node,
        parent_node=parent_node,
        llm=app.state.mind_reader.llm,
    )

    if not parsed:
        return None

    control_sequence = parsed.get("control_sequence", [])
    navigate_to = parsed.get("navigate_to")

    # If 8b says navigate to a different page, inject as nav subgoal
    if navigate_to and not control_sequence:
        nav_subgoal = f"Click {navigate_to}"
        _inject_subgoals(mission, [nav_subgoal])
        await mission_brain._save_mission(mission)
        logger.info(f"🔀 [GRAPH_WALKER] injected nav subgoal: '{nav_subgoal}'")
        return nav_subgoal

    if not control_sequence:
        return None

    # ── Convert to subgoals and inject ──
    new_subgoals = controls_to_subgoals(control_sequence)
    if not new_subgoals:
        return None

    _inject_subgoals(mission, new_subgoals)
    await mission_brain._save_mission(mission)

    logger.info(
        f"⚡ [GRAPH_WALKER] injected {len(new_subgoals)} control subgoals: "
        f"{new_subgoals}"
    )
    return new_subgoals[0]


def _inject_subgoals(mission: Any, new_subgoals: List[str]) -> None:
    """Insert new subgoals into mission BEFORE the current LAST_MILE entry.

    Before: ["Click Dashboard", "Click Usage", "LAST_MILE: show whisper usage..."]
                                                  ^ current_subgoal_index = 2

    After:  ["Click Dashboard", "Click Usage", "Click Model Filter", "Select Whisper",
             "Click Date Picker", "Select Last 7 days", "LAST_MILE: read result"]
                                                  ^ current_subgoal_index = 2 (now points to first new subgoal)
    """
    idx = mission.current_subgoal_index
    subgoals = list(mission.subgoals)

    # Replace the current LAST_MILE subgoal with the new control subgoals + a reading LAST_MILE
    original_goal = ""
    if idx < len(subgoals):
        original = subgoals[idx]
        if original.upper().startswith("LAST_MILE:"):
            original_goal = original.split(":", 1)[-1].strip()

    # Build the new list: everything before current + new subgoals + reading last_mile
    reading_subgoal = f"LAST_MILE: read result for {original_goal}" if original_goal else "LAST_MILE: read result"
    replacement = new_subgoals + [reading_subgoal]

    subgoals[idx:idx + 1] = replacement
    mission.subgoals = subgoals

    logger.debug(
        f"[GRAPH_WALKER] subgoals after injection: {subgoals} "
        f"current_idx={idx}"
    )

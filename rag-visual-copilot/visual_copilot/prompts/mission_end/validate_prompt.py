"""Mission end prompt builders for Visual CoPilot."""

from typing import Any

"""
mission_end.py

PURPOSE: Mission completion validator and end-of-mission dialogue generator.
         When the agent finishes the user's goal, this prompt:
         1. Validates that the mission is truly complete (goal visible on screen)
         2. Generates a natural closing statement about what was accomplished
         3. Asks the user what they'd like to do next

USED BY:
    - ultimate_api.py: After ReAct signals is_done or goal detected on screen
"""

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# MISSION VALIDATION PROMPT — "Did we actually succeed?"
# ═══════════════════════════════════════════════════════════════

MISSION_VALIDATE_PROMPT = """You are a mission completion validator for a web navigation agent.

=== MISSION ===
User's Goal: "{main_goal}"
Goal Essence: "{goal_essence}"
Action Type: {action_type}
Domain: {domain}
Actions Taken: {step_count}

=== ACTIONS PERFORMED ===
{action_history}

=== CURRENT SCREEN (visible elements) ===
{visible_content}

=== TASK ===
Determine if the user's goal has been ACHIEVED based on what is currently visible on screen.

Rules:
- For "show"/"find"/"search" goals: SUCCESS if matching items are visible on screen
- For "navigate"/"go to" goals: SUCCESS if the target page/section is reached
- For "buy"/"purchase" goals: SUCCESS only if item is in cart or checkout reached
- For "click"/"interact" goals: SUCCESS if the target element was interacted with
- Do NOT require perfection — if the user asked to "show black lingerie" and black lingerie items are visible, that's SUCCESS even if there are also other items

Reply in JSON:
{{
    "is_complete": true,
    "confidence": 0.95,
    "evidence": "Black lingerie items are now visible on screen after search",
    "what_was_done": "Searched for black lingerie and found matching results"
}}"""


# ═══════════════════════════════════════════════════════════════
# MISSION END DIALOGUE — "Here's what I did + what's next?"
# ═══════════════════════════════════════════════════════════════

MISSION_END_PROMPT = """You are TARA, a friendly and expressive web co-pilot. The user's mission just completed successfully.

=== COMPLETED MISSION ===
User's Goal: "{main_goal}"
What Was Done: "{what_was_done}"
Domain: {domain}
Steps Taken: {step_count}

=== VISIBLE ON SCREEN ===
{visible_content}

=== TASK ===
Generate a SHORT, natural closing statement (2-3 sentences max) that:
1. Confirms what you accomplished (be specific, mention what's visible)
2. Asks what they'd like to do next

Style rules:
- Be warm and conversational, like a helpful friend
- Do NOT be robotic or overly formal
- Do NOT list technical details or IDs
- Keep it under 50 words
- End with an open question about next steps

Reply in JSON:
{{
    "speech": "Your closing statement here"
}}"""


def build_validate_prompt(
    schema: Any,
    nodes: list,
    mission: Optional[Any] = None,
) -> str:
    """Build the mission validation prompt."""
    main_goal = getattr(schema, 'raw_utterance', None) or schema.target_entity
    goal_essence = schema.target_entity
    action_type = schema.action.value if hasattr(schema.action, 'value') else str(schema.action)
    domain = getattr(schema, 'domain', 'unknown')

    # Visible content — text from DOM nodes
    visible_content = "\n".join([
        f"- {n.text[:80]}"
        for n in nodes[:60]
        if n.text and len(n.text.strip()) > 2
    ]) or "(no visible text content)"

    # Action history
    history_lines: List[str] = []
    if mission and hasattr(mission, 'action_history') and mission.action_history:
        history_lines = mission.action_history[-10:]
    elif hasattr(schema, 'action_history') and schema.action_history:
        history_lines = schema.action_history[-10:]

    action_history = "\n".join(history_lines) if history_lines else "None recorded"
    step_count = len(history_lines)

    return MISSION_VALIDATE_PROMPT.format(
        main_goal=main_goal,
        goal_essence=goal_essence,
        action_type=action_type,
        domain=domain,
        step_count=step_count,
        action_history=action_history,
        visible_content=visible_content,
    )


def build_mission_end_prompt(
    schema: Any,
    nodes: list,
    what_was_done: str = "",
    mission: Optional[Any] = None,
) -> str:
    """Build the mission end dialogue prompt."""
    main_goal = getattr(schema, 'raw_utterance', None) or schema.target_entity
    domain = getattr(schema, 'domain', 'unknown')

    # Visible content summary
    visible_content = "\n".join([
        f"- {n.text[:80]}"
        for n in nodes[:40]
        if n.text and len(n.text.strip()) > 2
    ]) or "(no visible content)"

    step_count = 0
    if mission and hasattr(mission, 'action_history'):
        step_count = len(mission.action_history)
    elif hasattr(schema, 'action_history') and schema.action_history:
        step_count = len(schema.action_history)

    return MISSION_END_PROMPT.format(
        main_goal=main_goal,
        what_was_done=what_was_done,
        domain=domain,
        step_count=step_count,
        visible_content=visible_content,
    )

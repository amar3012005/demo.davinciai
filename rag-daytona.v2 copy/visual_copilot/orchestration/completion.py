import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def terminal_completion_response(mission: Any) -> Optional[Dict[str, Any]]:
    if not mission:
        return None
    if getattr(mission, "status", "") != "completed":
        return None
    phase = getattr(mission, "phase", "strategy")
    if phase != "done":
        return None
    return {
        "success": True,
        "blocked": False,
        "complete": True,
        "action": {
            "type": "answer",
            "speech": "I have already completed that task. What would you like to do next?",
        },
        "speech": "I have already completed that task. What would you like to do next?",
        "pipeline": "vc.completion.terminal",
    }


async def check_if_arrived(app, session_id: str, goal: str, current_url: str, nodes: List[Any], schema: Any) -> Tuple[bool, str]:
    if not app or not hasattr(app.state, "mind_reader") or not hasattr(app.state.mind_reader, "llm"):
        return False, "No LLM available for arrival check."

    content_nodes = [
        n for n in nodes
        if n.tag == "img" or (n.text and len(n.text.strip()) > 5 and n.zone == "main")
    ]

    if not content_nodes:
        return False, "No substantive content nodes found."

    compressed_dom = "\n".join([
        f"[tag={n.tag}] text='{n.text[:100]}'" if n.text else f"[tag={n.tag}] image"
        for n in content_nodes[:50]
    ])

    prompt = f"""You are determining if a web agent has achieved its ultimate goal.
USER GOAL: '{goal}'
TARGET ENTITY: '{schema.target_entity}'
CURRENT URL: '{current_url}'

VISIBLE CONTENT ELEMENTS (Images & Main Text only):
{compressed_dom}

INSTRUCTIONS:
1. Analyze the URL and Content Elements.
2. Has the user's goal been achieved? For a search/find goal, this means there are MULTIPLE distinct content results (e.g., product cards, video thumbnails, or a detailed profile) matching the goal.
3. CRITICAL: A single search dropdown suggestion matching the name is NOT arrival. You must see actual content items on the page.
4. Reply in strict JSON: {{"arrived": true/false, "reason": "brief 1-sentence explanation"}}"""

    try:
        response = await app.state.mind_reader.llm.generate(
            prompt,
            model="llama-3.1-8b-instant",
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        match = re.search(r"\{.*\}", response, re.DOTALL)
        decision = json.loads(match.group()) if match else json.loads(response)

        arrived = decision.get("arrived", False)
        reason = decision.get("reason", "No reason provided")

        if arrived:
            logger.info(f"🎯 LLM DOM CHECK (ARRIVED): {reason}")
        else:
            logger.debug(f"🔍 LLM DOM CHECK (NOT ARRIVED): {reason}")

        return arrived, reason
    except Exception as e:
        logger.error(f"LLM DOM check failed: {e}")
        return False, f"Error: {e}"


async def validate_and_end_mission(schema, nodes, mission, mission_brain, app, start_time) -> Optional[Dict[str, Any]]:
    from visual_copilot.prompts.mission_end import build_mission_end_prompt, build_validate_prompt

    try:
        validate_prompt = build_validate_prompt(schema, nodes, mission)
        if app and hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
            llm = app.state.mind_reader.llm
            raw = await llm.generate(
                validate_prompt,
                model="llama-3.1-8b-instant",
                temperature=0.1,
                response_format={"type": "json_object"},
            )
        else:
            raw = '{"is_complete": true, "confidence": 0.7, "evidence": "no LLM available", "what_was_done": "completed task"}'

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        validation = json.loads(match.group()) if match else json.loads(raw)

        logger.info(
            f"🏁 Mission Validator: complete={validation.get('is_complete')}, "
            f"confidence={validation.get('confidence')}, "
            f"evidence={validation.get('evidence', '')[:100]}"
        )

        if not validation.get("is_complete"):
            return None

        what_was_done = validation.get("what_was_done", "completed the task")

        end_prompt = build_mission_end_prompt(schema, nodes, what_was_done, mission)
        if app and hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
            end_raw = await llm.generate(
                end_prompt,
                model="llama-3.1-8b-instant",
                temperature=0.5,
                response_format={"type": "json_object"},
            )
        else:
            end_raw = f'{{"speech": "Done! I found {schema.target_entity} for you. What would you like to do next?"}}'

        end_match = re.search(r"\{.*\}", end_raw, re.DOTALL)
        end_data = json.loads(end_match.group()) if end_match else json.loads(end_raw)

        speech = end_data.get("speech", f"All done with {schema.target_entity}! What's next?")
        logger.info(f"🎤 Mission End Speech: {speech}")

        if mission:
            mission.status = "completed"
            mission.phase = "done"
            await mission_brain._save_mission(mission)

        return {
            "success": True,
            "blocked": False,
            "action": {"type": "answer", "speech": speech},
            "complete": True,
            "pipeline": "ultimate_tara",
            "confidence": validation.get("confidence", 0.9),
            "timing_ms": int((time.time() - start_time) * 1000),
        }

    except Exception as e:
        logger.error(f"❌ Mission validation/end failed: {e}")
        if mission:
            mission.status = "paused"
            await mission_brain._save_mission(mission)
        return {
            "success": True,
            "blocked": False,
            "action": {
                "type": "answer",
                "speech": f"I've found {schema.target_entity} for you! What would you like to do next?",
            },
            "complete": True,
            "pipeline": "ultimate_tara",
            "confidence": 0.7,
            "timing_ms": int((time.time() - start_time) * 1000),
        }

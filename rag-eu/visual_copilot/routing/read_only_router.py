import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from tara_models import ActionIntent

from visual_copilot.mission.verified_advance import record_and_maybe_advance
from visual_copilot.text.tokenization import _tokenize

logger = logging.getLogger(__name__)


async def maybe_route_read_only(
    *,
    schema: Any,
    mission: Any,
    run_read_only: Callable[..., Awaitable[Dict[str, Any]]],
    app: Any,
    session_id: str,
    goal: str,
    current_url: str,
    nodes: list,
    action_history: list,
) -> Optional[Dict[str, Any]]:
    action = getattr(getattr(schema, "action", None), "value", "")
    if action != "extraction":
        return None
    phase = getattr(mission, "phase", "strategy")
    if phase not in {"done", "last_mile", "strategy"}:
        return None
    return await run_read_only(
        app=app,
        session_id=session_id,
        goal=goal,
        current_url=current_url,
        nodes=nodes,
        action_history=action_history,
    )


async def run_read_only_terminal(
    *,
    session_id: str,
    app: Any,
    goal: str,
    query: str,
    schema: Any,
    nodes: List[Any],
    mission: Any,
    mission_brain: Any,
    semantic_detective: Any,
    hive_hints: Optional[List[Any]],
    excluded_ids: set[str],
    start_time: float,
    is_zero_shot: bool,
    current_url: str = "",
    dom_signature: str = "",
    verified_advance_active: bool = False,
) -> Dict[str, Any]:
    main_text_nodes = [
        n for n in nodes
        if getattr(n, "text", None) and len((n.text or "").strip()) >= 4 and getattr(n, "zone", "") == "main"
    ]
    aux_text_nodes = [
        n for n in nodes
        if getattr(n, "text", None) and len((n.text or "").strip()) >= 4 and getattr(n, "zone", "") in {"nav", "sidebar"}
    ]
    source_nodes = (main_text_nodes[:80] + aux_text_nodes[:20])[:100]
    visible_text = "\n".join((n.text or "")[:120] for n in source_nodes if n.text)

    if not visible_text.strip():
        return {
            "success": False,
            "blocked": True,
            "reason": "I could not find readable usage content on screen yet. Please wait for the page to render.",
            "action": {"type": "wait"},
            "pipeline": "ultimate_tara_read_only",
        }

    speech = ""
    try:
        if hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
            prompt = (
                f"You are extracting an answer from visible web content.\n"
                f"User goal: {goal}\n"
                f"Target: {schema.target_entity}\n\n"
                f"Visible content:\n{visible_text[:5000]}\n\n"
                "Return strict JSON with keys:\n"
                '{"found": true/false, "answer": "short answer", "evidence": "short evidence"}\n'
                "Rules:\n"
                "1) If usage numbers/timeframe are not visible, set found=false.\n"
                "2) Do not invent values."
            )
            raw = await app.state.mind_reader.llm.generate(
                prompt,
                model="llama-3.1-8b-instant",
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(match.group()) if match else json.loads(raw)
            if data.get("found"):
                speech = data.get("answer", "").strip()
                if not speech:
                    logger.info("🧠 READ MODE: model returned found=true but empty answer, treating as not-found.")
            else:
                logger.info("🧠 READ MODE: no clear stats yet, attempting constrained semantic assist.")
    except Exception as e:
        logger.warning(f"Read-only extraction failed, using safe fallback: {e}")

    if not speech:
        usage_terms = {"usage", "token", "tokens", "activity", "analytics", "billing", "dashboard", "model", "models"}
        goal_terms = _tokenize(f"{goal} {schema.target_entity} {query}")
        if "usage" in goal_terms or "token" in goal_terms:
            assist_query = "Click Usage in sidebar"
        elif "activity" in goal_terms:
            assist_query = "Click Activity in sidebar"
        elif "dashboard" in goal_terms:
            assist_query = "Click Dashboard in sidebar"
        else:
            assist_query = "Click Usage or Dashboard in sidebar"

        try:
            detective_report = await semantic_detective.investigate(
                session_id=session_id,
                query=assist_query,
                hive_hints=hive_hints or [],
                action_intent=ActionIntent.NAVIGATION,
                excluded_ids=list(excluded_ids),
                subgoal_mode="literal_click",
            )
            cand = detective_report.best_match
            if cand:
                text = (cand.text or "").lower()
                zone = (cand.zone or "").lower()
                tag = (cand.tag or "").lower()
                score = getattr(cand, "final_score", cand.hybrid_score)
                lexical = getattr(cand, "lexical_score", 0.0)
                overlap = any(t in text for t in usage_terms) or bool(goal_terms & _tokenize(text))
                zone_ok = zone in {"sidebar", "nav", "header"} or (zone == "main" and overlap)
                tag_ok = tag in {"a", "button", "summary"}
                strong_match = ("focus_exact_match" in (cand.reasons or [])) or lexical >= 0.55
                if tag_ok and zone_ok and overlap and strong_match and score >= 0.62 and cand.node_id not in excluded_ids:
                    logger.info(f"🧠 READ MODE semantic assist: click '{cand.text[:40]}' (zone={cand.zone}, score={score:.2f}, lex={lexical:.2f})")
                    _next_idx = await record_and_maybe_advance(
                        mission_brain=mission_brain,
                        mission=mission,
                        action_type="click",
                        target_id=cand.node_id,
                        nodes=nodes,
                        current_url=current_url,
                        dom_signature=dom_signature,
                        verified_advance_active=verified_advance_active,
                        is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                    )
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "click", "target_id": cand.node_id, "text": cand.text[:60] if cand.text else ""},
                        "speech": f"Opening {(cand.text[:40].strip() if cand.text else 'the relevant section')}...",
                        "mission_id": mission.mission_id if mission else None,
                        "subgoal_index": _next_idx if mission else 0,
                        "pipeline": "ultimate_tara_read_assist",
                        "confidence": "medium",
                        "pending_verification": verified_advance_active,
                        "timing_ms": int((time.time() - start_time) * 1000),
                    }
                logger.info(f"🧠 READ MODE semantic assist rejected: text='{cand.text[:40] if cand and cand.text else ''}' zone={zone} score={score:.2f}")
        except Exception as e:
            logger.warning(f"READ MODE semantic assist failed: {e}")

        return {
            "success": False,
            "blocked": True,
            "reason": "Usage stats are not clearly visible yet, and no safe closest navigation target was found.",
            "action": {"type": "wait"},
            "pipeline": "ultimate_tara_read_only",
        }

    if mission:
        await mission_brain.advance_subgoal(
            mission.mission_id,
            is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
        )
        _updated = await mission_brain._load_mission(mission.mission_id)
        _complete = bool(_updated and _updated.status == "completed")
    else:
        _complete = True

    return {
        "success": True,
        "blocked": False,
        "action": {"type": "answer", "speech": speech, "text": speech},
        "speech": speech,
        "complete": _complete,
        "pipeline": "ultimate_tara_read_only",
        "confidence": 0.85,
        "timing_ms": int((time.time() - start_time) * 1000),
    }

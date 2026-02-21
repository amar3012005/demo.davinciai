"""
Ultimate TARA API Endpoints

New pipeline using:
- Mind Reader (intent parsing)
- Hive Interface (Qdrant retrieval)
- Mission Brain (constraint enforcement)
- Semantic Detective (hybrid scoring)
- Live Graph (Redis DOM mirror)
"""

import logging
import time
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


async def ultimate_plan_next_step(
    app,
    session_id: str,
    goal: str,
    current_url: str,
    step_number: int = 0,
    action_history: Optional[list] = None
) -> Dict[str, Any]:
    """
    Ultimate TARA pipeline for planning next step.
    
    Flow:
    1. Mind Reader → TacticalSchema
    2. Get Map Hints from existing endpoint (GPS navigation)
    3. Hive Interface → Strategy + Visual Hints
    4. Mission Brain → Create Mission + Sub-goals
    5. Live Graph → Get DOM nodes
    6. Semantic Detective → Score candidates
    7. Mission Brain → Audit action (constraint check)
    8. Return approved action
    """
    start_time = time.time()
    
    # Get Ultimate TARA modules from app state
    mind_reader = app.state.mind_reader
    hive_interface = app.state.hive_interface
    mission_brain = app.state.mission_brain
    semantic_detective = app.state.semantic_detective
    live_graph = app.state.live_graph
    
    if not all([mind_reader, hive_interface, mission_brain, semantic_detective, live_graph]):
        logger.warning("⚠️ Ultimate TARA modules not fully initialized, falling back to legacy")
        return None
    
    try:
        # ── Build excluded IDs from action_history (anti-loop) ──
        # action_history from orchestrator: list of {"action": "click", "target_id": "t-xxx", ...}
        excluded_ids = set()
        if action_history:
            for entry in action_history:
                if isinstance(entry, dict) and entry.get("target_id"):
                    excluded_ids.add(entry["target_id"].strip())
                elif isinstance(entry, str) and ":" in entry:
                    # "click:t-xxx" format from mission brain
                    excluded_ids.add(entry.split(":", 1)[1].strip())
        logger.info(f"📋 action_history: {len(action_history or [])} entries, {len(excluded_ids)} excluded IDs")

        # Step 1: Parse intent with Mind Reader
        logger.info("🧠 Step 1: Mind Reader parsing intent...")
        schema = await mind_reader.translate(
            user_input=goal,
            current_url=current_url
        )
        logger.info(f"   ✅ Intent: {schema.action.value} on '{schema.target_entity}'")

        # Step 2: Get GPS navigation hints from existing endpoint
        logger.info("🗺️ Step 2: Getting GPS navigation hints...")
        try:
            from visual_orchestrator import VisualOrchestrator
            if 'visual_orchestrator' in dir(app.state):
                hints = await app.state.visual_orchestrator.get_navigation_hints(
                    goal=goal,
                    client_id="demo",
                    current_url=current_url
                )
                logger.info(f"   ✅ GPS Hints: {hints[:100] if hints else 'None'}")
            else:
                hints = ""
        except Exception as e:
            logger.warning(f"⚠️ GPS hints failed: {e}")
            hints = ""

        # Step 3: Retrieve strategy + hints from Hive
        logger.info("🧠 Step 3: Hive Interface retrieving strategy...")
        hive_response = await hive_interface.retrieve(schema)
        logger.info(f"   ✅ Strategy: {bool(hive_response.strategy)}, Hints: {len(hive_response.visual_hints)}")

        # ═══════════════════════════════════════════════════════════
        # 🎯 LOCATION GUARD — "Am I already there?"
        # If the current URL already contains the target entity,
        # the agent has ARRIVED. Stop clicking. Start talking.
        # This fires BEFORE mission creation to prevent the
        # "subgoal 0 reset" loop entirely.
        # ═══════════════════════════════════════════════════════════
        target_slug = schema.target_entity.lower().replace(" ", "-")
        target_words = schema.target_entity.lower().split()
        url_lower = current_url.lower()

        url_matches_target = (
            target_slug in url_lower or                           # "prompt-caching" in URL
            all(w in url_lower for w in target_words if len(w) > 3)  # all significant words match
        )

        if url_matches_target and step_number > 0:
            logger.info(f"🎯 LOCATION GUARD: URL '{current_url}' matches target '{schema.target_entity}'. Forcing answer.")

            # Get DOM text for a richer answer
            nodes = await live_graph.get_visible_nodes(session_id)
            page_text = " ".join([n.text for n in nodes if n.text])[:500]

            speech_text = f"I've reached the {schema.target_entity} page. What would you like to know?"
            if schema.target_entity.lower() in page_text.lower():
                speech_text = f"I'm now on the {schema.target_entity} documentation. How can I help you with this?"

            return {
                "success": True,
                "blocked": False,
                "action": {
                    "type": "answer",
                    "speech": speech_text
                },
                "complete": True,
                "pipeline": "ultimate_tara",
                "confidence": 1.0
            }

        # Step 3b: Cross-Domain Fallback — if zero hints on current domain, search globally
        if not hive_response.visual_hints:
            logger.info("🌐 Step 3b: Zero hints on current domain — triggering cross-domain search...")
            cross_response = await hive_interface.retrieve_cross_domain(schema)
            if cross_response and getattr(cross_response, 'cross_domain_target', None):
                bridge_domain = cross_response.cross_domain_target
                logger.info(f"🌐 Cross-Domain Bridge detected: routing to '{bridge_domain}'")
                return {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "cross_domain_navigate",
                        "target_domain": bridge_domain,
                        "target_entity": schema.target_entity,
                        "hints": [h.__dict__ for h in cross_response.visual_hints],
                        "speech": f"I found '{schema.target_entity}' on {bridge_domain}. Navigating there now."
                    },
                    "pipeline": "ultimate_tara_cross_domain",
                    "confidence": 0.85
                }

        # Step 4: Get or resume mission (persists across requests)
        logger.info("🧠 Step 4: Mission Brain get/create mission...")
        mission = await mission_brain.get_or_create_mission(
            session_id=session_id,
            schema=schema,
            strategy=hive_response.strategy
        )
        logger.info(f"   ✅ Mission: {mission.mission_id}, Subgoal: {mission.current_subgoal_index}/{len(mission.subgoals)}")

        # 🏁 TERMINAL STATE: If mission was already completed, don't create a new one.
        # This catches the case where advance_subgoal set status="completed"
        # and the next request hits get_or_create_mission which MUST return it.
        if mission and mission.status == "completed":
            logger.info("🏁 Mission already complete. Transitioning to Extraction.")

            nodes = await live_graph.get_visible_nodes(session_id)
            page_text = " ".join([n.text for n in nodes if n.text])

            speech_text = f"I've completed the navigation to {schema.target_entity}."
            if schema.target_entity.lower() in page_text.lower():
                speech_text = f"I'm now on the page for {schema.target_entity}."

            return {
                "success": True,
                "blocked": False,
                "action": {
                    "type": "answer",
                    "speech": speech_text
                },
                "complete": True,
                "pipeline": "ultimate_tara",
                "confidence": 1.0
            }

        # Merge mission's own action_history into excluded_ids
        for action_key in mission.action_history:
            if ":" in action_key:
                excluded_ids.add(action_key.split(":", 1)[1].strip())

        # Step 5: Get DOM from Live Graph
        logger.info("👁️ Step 5: Live Graph getting DOM nodes...")
        nodes = await live_graph.get_visible_nodes(session_id)
        logger.info(f"   ✅ DOM nodes: {len(nodes)}")

        # Determine query: use current subgoal if available, else target_entity
        current_subgoal = (
            mission.subgoals[mission.current_subgoal_index]
            if mission.current_subgoal_index < len(mission.subgoals)
            else schema.target_entity
        )
        query = current_subgoal
        logger.info(f"   🎯 Query (subgoal {mission.current_subgoal_index}): '{query}'")

        # Step 6: Investigate with Semantic Detective (with exclusions)
        logger.info(f"🔍 Step 6: Semantic Detective investigating (excluding {len(excluded_ids)} IDs)...")
        detective_report = await semantic_detective.investigate(
            session_id=session_id,
            query=query,
            hive_hints=hive_response.visual_hints,
            action_intent=schema.action,
            excluded_ids=list(excluded_ids)
        )
        logger.info(f"   ✅ Best match: {detective_report.best_match.text if detective_report.best_match else 'none'} "
                   f"(score: {detective_report.best_match.hybrid_score if detective_report.best_match else 0:.2f})")

        # Step 7: Audit action with Mission Brain (constraint enforcement)
        if detective_report.best_match:

            # ═══════════════════════════════════════════════════════════
            # HARD CONFIDENCE GUARDRAIL: Block garbage guesses immediately
            # The detective may return a "best match" but with a terrible
            # score (e.g., 0.24 for "Continue with Google" when looking
            # for "Dashboard"). Without this gate, the Mission Brain
            # would blindly approve it because it only checks constraints,
            # not semantic quality.
            # ═══════════════════════════════════════════════════════════
            if detective_report.confidence == "low" or detective_report.best_match.hybrid_score < 0.35:
                logger.warning(
                    f"   🚫 Action BLOCKED: Confidence too low "
                    f"({detective_report.best_match.hybrid_score:.2f}, "
                    f"confidence={detective_report.confidence})"
                )
                return {
                    "success": False,
                    "blocked": True,
                    "reason": f"I cannot confidently find '{query}' on this screen.",
                    "action": None
                }

            logger.info("🛡️ Step 7: Mission Brain auditing action...")
            approved, reason = await mission_brain.audit_action(
                mission_id=mission.mission_id,
                action_type=detective_report.recommended_action,
                target_id=detective_report.best_match.node_id,
                target_text=detective_report.best_match.text,
                detective_report=detective_report
            )

            if not approved:
                logger.warning(f"   🚫 Action BLOCKED: {reason}")
                # Advance subgoal on block (try next subgoal instead of repeating)
                await mission_brain.advance_subgoal(mission.mission_id)
                return {
                    "success": False,
                    "blocked": True,
                    "reason": reason,
                    "action": None
                }

            logger.info(f"   ✅ Action APPROVED: {detective_report.recommended_action}")

            # Record action in mission history (prevents future repeats)
            await mission_brain.record_action(
                mission_id=mission.mission_id,
                action_type=detective_report.recommended_action,
                target_id=detective_report.best_match.node_id,
                success=True
            )

            # ═══════════════════════════════════════════════════════════
            # BUG FIX: Advance to next subgoal after successful action!
            # Without this, the mission brain replays the same subgoal
            # forever ("Groundhog Day" loop).
            # ═══════════════════════════════════════════════════════════
            await mission_brain.advance_subgoal(mission.mission_id)

        # Build response
        elapsed_ms = int((time.time() - start_time) * 1000)

        return {
            "success": True,
            "blocked": False,
            "action": {
                "type": detective_report.recommended_action,
                "target_id": detective_report.best_match.node_id if detective_report.best_match else "",
                "text": detective_report.best_match.text if detective_report.best_match else "",
            },
            "mission_id": mission.mission_id,
            "subgoal_index": mission.current_subgoal_index,
            "confidence": detective_report.confidence,
            "timing_ms": elapsed_ms,
            "pipeline": "ultimate_tara",
            "gps_hints": hints
        }
        
    except Exception as e:
        logger.error(f"❌ Ultimate TARA pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        return None


async def ultimate_update_constraint(
    app,
    mission_id: str,
    constraint_name: str,
    value: str
) -> Dict[str, Any]:
    """
    Update constraint value (e.g., user selected size).
    """
    mission_brain = app.state.mission_brain
    
    if not mission_brain:
        return {"success": False, "error": "Mission Brain not available"}
    
    try:
        from tara_models import ConstraintStatus
        
        success = await mission_brain.update_constraint(
            mission_id=mission_id,
            constraint_name=constraint_name,
            value=value,
            status=ConstraintStatus.FILLED
        )
        
        if success:
            status = await mission_brain.get_mission_status(mission_id)
            return {
                "success": True,
                "constraint": constraint_name,
                "value": value,
                "mission_status": status
            }
        else:
            return {"success": False, "error": "Failed to update constraint"}
            
    except Exception as e:
        logger.error(f"❌ Constraint update failed: {e}")
        return {"success": False, "error": str(e)}

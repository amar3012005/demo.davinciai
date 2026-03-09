import time
import uuid
from typing import Any, Optional

from visual_copilot.logging.config import emit_event, get_logger
from visual_copilot.models.contracts import PlanningContext
from visual_copilot.orchestration.bootstrap import build_context, require_runtime_modules
from visual_copilot.orchestration.completion import terminal_completion_response
from visual_copilot.orchestration.plan_next_step_flow import ultimate_plan_next_step_impl
from visual_copilot.mission.constraints import update_constraint

logger = get_logger("vc.orchestration.pipeline")


async def run_pipeline(
    *,
    app: Any,
    session_id: str,
    goal: str,
    current_url: str,
    step_number: int,
    action_history: list[str],
    previous_goal: Optional[str] = None,
    mission_id: Optional[str] = None,
    screenshot_b64: str = "",
    pre_decision: Optional[dict] = None,
    route_hint: str = "",
):
    trace_id = str(uuid.uuid4())
    ctx: PlanningContext = build_context(
        app=app,
        session_id=session_id,
        goal=goal,
        current_url=current_url,
        step_number=step_number,
        action_history=action_history or [],
        previous_goal=previous_goal,
        mission_id=mission_id,
        trace_id=trace_id,
    )
    t0 = time.time()
    emit_event(
        logger,
        "VC_PIPELINE_START",
        trace_id=trace_id,
        session_id=session_id,
        mission_id=mission_id or "",
        module="vc.orchestration.pipeline",
        step_number=step_number,
        subgoal_index=0,
        decision="start",
        reason="request_received",
        duration_ms=0,
    )

    modules = require_runtime_modules(ctx.app)
    mission_brain = modules["mission_brain"]

    try:
        existing = await mission_brain._load_session_mission(session_id)
        terminal = terminal_completion_response(existing)
        if terminal:
            terminal["trace_id"] = trace_id
            emit_event(
                logger,
                "VC_MISSION_COMPLETED",
                trace_id=trace_id,
                session_id=session_id,
                mission_id=getattr(existing, "mission_id", mission_id or ""),
                module="vc.orchestration.pipeline",
                step_number=step_number,
                subgoal_index=int(getattr(existing, "current_subgoal_index", 0) or 0),
                decision="terminal_shortcut",
                reason="already_completed",
                duration_ms=int((time.time() - t0) * 1000),
            )
            return terminal

        result = await ultimate_plan_next_step_impl(
            app=ctx.app,
            session_id=ctx.session_id,
            goal=ctx.goal,
            current_url=ctx.current_url,
            step_number=ctx.step_number,
            action_history=ctx.action_history,
            previous_goal=ctx.previous_goal,
            mission_id=ctx.mission_id,
            screenshot_b64=screenshot_b64,
            pre_decision=pre_decision,
            route_hint=route_hint,
        )
        res_action = (result or {}).get("action", {})
        target_id = ""
        if isinstance(res_action, list):
            for step in res_action:
                if isinstance(step, dict) and step.get("type", "").lower() in {"click", "type_text", "select"}:
                    target_id = step.get("target_id", "")
                    break
        elif isinstance(res_action, dict):
            if res_action.get("type") == "bundle":
                for step in res_action.get("sequence", []):
                    if isinstance(step, dict) and step.get("action", "").lower() in {"click", "type_text", "select"}:
                        target_id = step.get("target_id", "")
                        break
            else:
                target_id = res_action.get("target_id", "")
            
        emit_event(
            logger,
            "VC_ROUTER_DECISION",
            trace_id=trace_id,
            session_id=session_id,
            mission_id=(result or {}).get("mission_id", mission_id or ""),
            module="vc.orchestration.pipeline",
            step_number=step_number,
            subgoal_index=(result or {}).get("subgoal_index", 0),
            decision="success" if (result or {}).get("success") else "non_success",
            reason=(result or {}).get("pipeline", "vc.orchestration.plan_next_step"),
            target_id=target_id,
            duration_ms=int((time.time() - t0) * 1000),
        )
        return result
    except Exception as e:
        emit_event(
            logger,
            "VC_ERROR",
            trace_id=trace_id,
            session_id=session_id,
            mission_id=mission_id or "",
            module="vc.orchestration.pipeline",
            step_number=step_number,
            subgoal_index=0,
            decision="exception",
            reason=str(e),
            duration_ms=int((time.time() - t0) * 1000),
        )
        raise


async def run_update_constraint(*, app: Any, mission_id: str, constraint_name: str, value: str):
    modules = require_runtime_modules(app)
    mission_brain = modules["mission_brain"]
    return await update_constraint(
        mission_brain=mission_brain,
        mission_id=mission_id,
        constraint_name=constraint_name,
        value=value,
    )

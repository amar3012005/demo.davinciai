from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PlanningContext:
    app: Any
    session_id: str
    goal: str
    current_url: str
    step_number: int
    action_history: list[str] = field(default_factory=list)
    previous_goal: Optional[str] = None
    mission_id: Optional[str] = None
    trace_id: str = ""


@dataclass
class RoutingDecision:
    decision: str
    reason: str
    module: str
    confidence: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    module: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    success: bool
    response: Dict[str, Any]
    decision: Optional[RoutingDecision] = None

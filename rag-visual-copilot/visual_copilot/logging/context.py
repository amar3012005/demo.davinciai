from dataclasses import dataclass


@dataclass
class TraceContext:
    trace_id: str
    session_id: str
    mission_id: str = ""
    step_number: int = 0
    subgoal_index: int = 0

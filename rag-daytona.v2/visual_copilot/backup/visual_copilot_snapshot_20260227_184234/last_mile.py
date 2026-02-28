"""Last-mile reasoning helpers for Visual CoPilot.

Compatibility module: implementation is sourced from visual_copilot.orchestrator.
"""

from .orchestrator import (
    _should_enter_last_mile,
    _goal_completion_guard,
    _validate_last_mile_step,
    _hash_last_mile_sequence,
)

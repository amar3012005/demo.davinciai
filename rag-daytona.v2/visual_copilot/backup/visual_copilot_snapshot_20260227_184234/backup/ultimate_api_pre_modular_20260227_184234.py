"""Backward-compatible module alias for Visual CoPilot orchestrator.

This preserves existing imports and mutable module-level state behavior by
aliasing this module object to visual_copilot.orchestrator.
"""

import sys
from visual_copilot import orchestrator as _orchestrator

sys.modules[__name__] = _orchestrator

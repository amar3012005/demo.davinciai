"""DOM/node classification and validation helpers for Visual CoPilot.

Compatibility module: implementation is sourced from visual_copilot.orchestrator.
"""

from .orchestrator import (
    MatchResult,
    _is_clickable_node,
    _is_type_node,
    _is_text_heavy,
    _action_tag_compatible,
    _validate_action_target,
    _node_text_blob,
    _collect_node_text_for_match,
    _node_matches_expected_labels,
    _resolve_clickable_target_id,
    _build_dom_signature,
    _snapshot_node_state,
    _verify_pending_action_effect,
)

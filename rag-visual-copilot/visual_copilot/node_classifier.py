"""DOM/node classification and validation helpers for Visual CoPilot."""

from .routing.lexical_router import MatchResult
from .routing.action_guard import (
    _is_clickable_node,
    _is_type_node,
    _is_text_heavy,
    _action_tag_compatible,
    _validate_action_target,
    _node_text_blob,
    _node_matches_expected_labels,
    _resolve_clickable_target_id,
)
from .text.label_extraction import _collect_node_text_for_match
from .mission.verified_advance import (
    build_dom_signature as _build_dom_signature,
    snapshot_node_state as _snapshot_node_state,
    verify_pending_action_effect as _verify_pending_action_effect,
)

"""Deterministic keyword and lexical matching for Visual CoPilot."""

from .routing.lexical_router import (
    _find_hard_keyword_match,
    _build_label_policy,
    _find_best_type_target,
    _lexical_ground_candidate,
    _node_matches_strategy_focus,
)

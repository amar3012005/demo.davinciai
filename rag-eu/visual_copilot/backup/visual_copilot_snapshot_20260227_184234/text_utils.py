"""Text parsing and normalization utilities for Visual CoPilot.

Compatibility module: implementation is sourced from visual_copilot.orchestrator.
"""

from .orchestrator import (
    _tokenize,
    _canonicalize_label,
    _expand_label_synonyms,
    _extract_zone_targets,
    _zone_compatible_for_direct,
    _explicit_query_terms,
    _strategy_focus_terms,
    _extract_type_text,
    _candidate_signature,
    _extract_quoted_labels,
    _extract_unquoted_label_phrase,
    _extract_label_candidates,
    _override_mode_for_labels,
    _classify_subgoal_mode,
)

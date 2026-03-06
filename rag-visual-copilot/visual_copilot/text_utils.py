"""Text parsing and normalization utilities for Visual CoPilot."""

from .text.tokenization import (
    _tokenize,
    _canonicalize_label,
    _extract_zone_targets,
    _zone_compatible_for_direct,
    _explicit_query_terms,
    _strategy_focus_terms,
    _extract_type_text,
    _candidate_signature,
    _extract_quoted_labels,
    _extract_unquoted_label_phrase,
    _override_mode_for_labels,
    _classify_subgoal_mode,
)
from .text.normalization import _expand_label_synonyms
from .text.label_extraction import _extract_label_candidates

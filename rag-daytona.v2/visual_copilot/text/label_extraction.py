from typing import Any, List
from visual_copilot.text.tokenization import _canonicalize_label, _extract_quoted_labels, _extract_unquoted_label_phrase

def _extract_label_candidates(
    query: str,
    schema: Any,
    *,
    allow_first_subgoal_fallback: bool = True
) -> List[str]:
    labels: List[str] = []
    query_quoted = _extract_quoted_labels(query)
    labels.extend(query_quoted)
    unquoted_query = _extract_unquoted_label_phrase(query)
    if unquoted_query:
        labels.append(unquoted_query)

    # Only use first_subgoal as fallback context if current query has no explicit label.
    if allow_first_subgoal_fallback and not labels:
        first_sub = getattr(schema, "first_subgoal", "") or ""
        if first_sub and first_sub != query:
            labels.extend(_extract_quoted_labels(first_sub))
            unquoted_first = _extract_unquoted_label_phrase(first_sub)
            if unquoted_first:
                labels.append(unquoted_first)

    uniq: List[str] = []
    seen = set()
    for label in labels:
        can = _canonicalize_label(label)
        if can and can not in seen:
            seen.add(can)
            uniq.append(label.strip())
    return uniq

def _collect_node_text_for_match(node: Any) -> str:
    return " ".join([
        getattr(node, "text", "") or "",
        getattr(node, "placeholder", "") or "",
        getattr(node, "name", "") or "",
        getattr(node, "role", "") or "",
        getattr(node, "state", "") or "",
        str(getattr(node, "aria_selected", "") or ""),
        str(getattr(node, "aria_expanded", "") or ""),
    ])


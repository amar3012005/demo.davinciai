from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from visual_copilot.constants import _DOMAIN_ALIASES, _TYPE_TAGS, _TYPE_ROLES, _CLICK_TAGS, _CLICK_ROLES
from visual_copilot.text.tokenization import _tokenize, _explicit_query_terms, _extract_zone_targets, _zone_compatible_for_direct, _strategy_focus_terms, _canonicalize_label
from visual_copilot.text.label_extraction import _collect_node_text_for_match, _extract_label_candidates
from visual_copilot.text.normalization import _expand_label_synonyms
from visual_copilot.routing.action_guard import _is_type_node, _is_clickable_node, _is_text_heavy, _node_text_blob


@dataclass
class MatchResult:
    candidate_id: Optional[str]
    matched_label: str
    match_mode: str
    raw_node_id: Optional[str]
    reason: str


_LABEL_NOISE_TOKENS = {
    "the",
    "a",
    "an",
    "to",
    "in",
    "on",
    "at",
    "of",
    "for",
    "click",
    "open",
    "select",
    "choose",
    "navigate",
    "go",
    "link",
    "button",
    "tab",
    "menu",
    "navigation",
    "nav",
    "top",
    "left",
    "right",
    "sidebar",
    "header",
    "footer",
}


def _reduce_label_phrase(text: str) -> str:
    canonical = _canonicalize_label(text)
    if not canonical:
        return ""
    tokens = [t for t in canonical.split() if t and t not in _LABEL_NOISE_TOKENS]
    return " ".join(tokens).strip()


def _node_matches_strategy_focus(node: Any, query: str) -> bool:
    terms = _strategy_focus_terms(query)
    if not terms:
        return True
    blob = _canonicalize_label(_collect_node_text_for_match(node))
    if not blob:
        return False
    return any(t in blob for t in terms)


def _find_best_type_target(nodes: List[Any], query: str, excluded_ids: set[str]) -> Optional[Any]:
    hints = {"search", "such", "suchort", "location", "city", "ort", "where"}
    query_terms = _tokenize(query or "")
    best_node = None
    best_score = -1.0
    for n in nodes:
        if not getattr(n, "interactive", False):
            continue
        nid = getattr(n, "id", "")
        if nid in excluded_ids:
            continue
        if not _is_type_node(n):
            continue
        blob = _node_text_blob(n)
        score = 0.0
        if any(h in blob for h in hints):
            score += 0.6
        score += min(0.4, 0.1 * len(query_terms & _tokenize(blob)))
        if getattr(n, "zone", "") in {"main", "nav"}:
            score += 0.05
        if score > best_score:
            best_score = score
            best_node = n
    return best_node if best_score >= 0.35 else None


def _find_hard_keyword_match(nodes: List[Any], labels: List[str], domain: str, subgoal_mode: str, excluded_ids: set[str]) -> MatchResult:
    if not labels:
        return MatchResult(None, "", "none", None, "no_labels")

    # (raw_label, normalized_label, match_mode, reduced_variant)
    expanded_labels: List[Tuple[str, str, str, bool]] = []
    for raw in labels:
        raw_canonical = _canonicalize_label(raw)
        for variant in _expand_label_synonyms(raw, domain):
            mode = "exact" if variant == raw_canonical else "synonym"
            expanded_labels.append((raw, variant, mode, False))

            reduced = _reduce_label_phrase(variant)
            if reduced and reduced != variant:
                # Keep reduced variants as strong label intents (not token_overlap),
                # so phrases like "the docs link in navigation" map to "Docs".
                expanded_labels.append((raw, reduced, mode, True))

    best_raw_id: Optional[str] = None
    best_label = ""
    best_mode = "none"
    best_score = 0.0
    for n in nodes:
        if not getattr(n, "interactive", False):
            continue
        nid = getattr(n, "id", "")
        if nid in excluded_ids:
            continue
        if subgoal_mode == "literal_type" and not _is_type_node(n):
            continue
        if subgoal_mode == "literal_click" and getattr(n, "tag", "") not in _CLICK_TAGS and getattr(n, "role", "") not in _CLICK_ROLES:
            continue
            
        blob = _canonicalize_label(_collect_node_text_for_match(n))
        if not blob:
            continue
            
        for raw_label, norm_label, mode, is_reduced in expanded_labels:
            score = 0.0
            if norm_label == blob:
                score = 1.0
            elif norm_label in blob:
                score = 0.97 if mode == "exact" else 0.94
            else:
                overlap = len(set(norm_label.split()) & set(blob.split()))
                if overlap > 0:
                    score = min(0.92, overlap / max(1, len(norm_label.split())))
            
            if score <= 0.0:
                continue
                
            if mode == "synonym":
                score -= 0.01
            if is_reduced:
                score -= 0.015
                
            if getattr(n, "zone", "") in {"nav", "sidebar", "header"}:
                score += 0.02
                
            if score > best_score:
                best_score = score
                best_raw_id = nid
                best_label = raw_label
                best_mode = mode if score >= 0.94 else "token_overlap"

    if not best_raw_id:
        return MatchResult(None, "", "none", None, "no_label_match")
    return MatchResult(best_raw_id, best_label, best_mode, best_raw_id, "ok")


def _build_label_policy(query: str, schema: Any, subgoal_mode: str, label_candidates: Optional[List[str]] = None, allow_first_subgoal_fallback: bool = True) -> Dict[str, Any]:
    labels = list(label_candidates or _extract_label_candidates(query, schema, allow_first_subgoal_fallback=allow_first_subgoal_fallback))
    uniq = []
    seen = set()
    for l in labels:
        c = _canonicalize_label(l)
        if c and c not in seen:
            seen.add(c)
            uniq.append(l)
    return {
        "has_explicit_label": bool(uniq) and subgoal_mode in {"literal_click", "literal_type"},
        "label_candidates": uniq,
        "match_mode": "none",
        "resolved_target_id": None,
        "matched_label": "",
        "raw_node_id": None,
        "miss_reason": "",
    }


def _lexical_ground_candidate(query: str, schema: Any, nodes: List[Any], subgoal_mode: str, excluded_ids: set[str]) -> Optional[Dict[str, Any]]:
    explicit_terms = _explicit_query_terms(query)
    target_zones = _extract_zone_targets(query)
    target_tokens = _tokenize(getattr(schema, "target_entity", ""))
    all_tokens = _tokenize(query) | target_tokens | _DOMAIN_ALIASES
    if not all_tokens:
        return None

    best = None
    best_score = 0.0
    for n in nodes:
        if not getattr(n, "interactive", False):
            continue
        if getattr(n, "id", "") in excluded_ids:
            continue
        if subgoal_mode == "literal_type" and not _is_type_node(n):
            continue
        if subgoal_mode == "literal_click" and not _is_clickable_node(n):
            continue

        text = getattr(n, "text", "") or ""
        tokens = _tokenize(text) | _tokenize(getattr(n, "id", ""))
        if not (tokens & all_tokens):
            continue

        explicit_overlap = len(tokens & explicit_terms)
        target_overlap = len(tokens & target_tokens)
        explicit_score = min(0.85, explicit_overlap / max(1, len(explicit_terms) or 1))
        target_score = min(0.10, 0.05 * target_overlap)
        overlap_score = explicit_score + target_score

        zone = (getattr(n, "zone", "") or "").lower()
        tag = (getattr(n, "tag", "") or "").lower()
        role = (getattr(n, "role", "") or "").lower()

        zone_boost = 0.15 if (subgoal_mode == "literal_click" and zone in {"nav", "sidebar"}) else 0.08 if (subgoal_mode == "literal_type" and zone in {"main", "nav"}) else 0.0
        affordance_boost = 0.2 if (subgoal_mode == "literal_type" and (tag in _TYPE_TAGS or role in _TYPE_ROLES)) else 0.12 if (subgoal_mode == "literal_click" and (tag in _CLICK_TAGS or role in _CLICK_ROLES)) else 0.0
        text_penalty = 0.25 if (subgoal_mode == "literal_click" and _is_text_heavy(tag, text)) else 0.0
        zone_penalty = 0.35 if (target_zones and not _zone_compatible_for_direct(target_zones, zone)) else 0.0

        score = max(0.0, min(1.0, overlap_score + zone_boost + affordance_boost - text_penalty - zone_penalty))
        if score > best_score:
            best_score = score
            best = {
                "node": n,
                "score": score,
                "explicit_overlap": explicit_overlap,
                "label_exact": any(term and term in text.lower() for term in explicit_terms),
                "zone_match": _zone_compatible_for_direct(target_zones, zone),
                "explicit_terms": sorted(explicit_terms),
                "target_zones": sorted(target_zones),
            }

    return best

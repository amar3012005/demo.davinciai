"""
Tool Executor for the TARA Compound Last-Mile Agent.

Bridges the gap between the LLM's tool calls and TARA's live
DOM validation + Groq Vision multimodal pipeline.

Vision pipeline:
  LLM calls request_vision
    -> broker looks up WebSocket from app.state.active_websockets
    -> sends request_screenshot WS message to browser
    -> browser captures page via html2canvas
    -> browser sends screenshot_response WS message
    -> screenshot_broker resolves the Future
    -> executor calls Groq llama-4-scout-17b-16e-instruct with the base64 image
    -> vision result fed back into the LLM conversation as a tool result
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx
from visual_copilot.constants import _CLICK_TAGS, _CLICK_ROLES
from visual_copilot.text.tokenization import _tokenize
from visual_copilot.mission.escalation_checkpoint import EscalationPayload

logger = logging.getLogger(__name__)
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# ═══════════════════════════════════════════════════════════════════════
# Vision API Rate Limiting
# ═══════════════════════════════════════════════════════════════════════

_vision_rate_limiter: Optional[asyncio.Semaphore] = None


def _get_vision_rate_limiter() -> asyncio.Semaphore:
    """Get or create the vision API rate limiter semaphore (max 2 concurrent calls)."""
    global _vision_rate_limiter
    if _vision_rate_limiter is None:
        _vision_rate_limiter = asyncio.Semaphore(2)
    return _vision_rate_limiter


# ═══════════════════════════════════════════════════════════════════════
# Screenshot Dedup Cache — skip duplicate vision calls
# ═══════════════════════════════════════════════════════════════════════

import hashlib as _hashlib

# session_id -> (screenshot_hash, vision_result_str)
_vision_dedup_cache: Dict[str, Tuple[str, str]] = {}


def _hash_screenshot(b64_data: str) -> str:
    """Fast hash of screenshot data for dedup (first 2048 chars + length)."""
    sig = f"{len(b64_data)}:{b64_data[:2048]}"
    return _hashlib.md5(sig.encode("utf-8")).hexdigest()[:16]


def _get_cached_vision(session_id: str, screenshot_hash: str) -> Optional[str]:
    """Return cached vision result if screenshot is unchanged."""
    entry = _vision_dedup_cache.get(session_id)
    if entry and entry[0] == screenshot_hash:
        return entry[1]
    return None


def _set_vision_cache(session_id: str, screenshot_hash: str, result: str) -> None:
    """Cache vision result keyed by session + screenshot hash."""
    _vision_dedup_cache[session_id] = (screenshot_hash, result)


def clear_vision_cache(session_id: str) -> None:
    """Clear vision cache for a session (call on session end or new goal)."""
    _vision_dedup_cache.pop(session_id, None)


# ═══════════════════════════════════════════════════════════════════════
# Click Tracking for Semantic Guardrails
# ═══════════════════════════════════════════════════════════════════════

class ClickTracker:
    """Session-scoped tracker for click patterns to detect rabbit-hole behavior."""

    def __init__(self):
        self.click_history: List[Dict[str, str]] = []  # [{id, label, url_depth}]
        self.consecutive_no_progress: int = 0

    def record_click(self, target_id: str, label: str) -> None:
        self.click_history.append({"id": target_id, "label": label.lower().strip()})
        if len(self.click_history) > 10:
            self.click_history = self.click_history[-10:]

    def is_reclick(self, target_id: str) -> bool:
        return any(c["id"] == target_id for c in self.click_history)

    def get_recent_labels(self, n: int = 5) -> List[str]:
        return [c["label"] for c in self.click_history[-n:]]


# Module-level tracker registry (session_id -> ClickTracker)
_click_trackers: Dict[str, ClickTracker] = {}


def get_click_tracker(session_id: str) -> ClickTracker:
    if session_id not in _click_trackers:
        _click_trackers[session_id] = ClickTracker()
    return _click_trackers[session_id]


def clear_click_tracker(session_id: str) -> None:
    _click_trackers.pop(session_id, None)


def _is_valid_id(target_id: str, nodes: List[Any]) -> bool:
    """Check if the target_id exists in the current LiveGraph."""
    if not target_id:
        return False
    if target_id.lower() in {"?", "unknown", "none", "id", "missing"} or "t-?" in target_id:
        return False
    for n in nodes:
        if str(getattr(n, "id", "")) == str(target_id):
            return True
    return False


def _resolve_intent_to_target_id(
    intent: Dict[str, Any],
    nodes: List[Any],
    excluded_ids: set[str],
) -> Tuple[Optional[str], str]:
    """
    Map LLM intent description to actual DOM element ID.
    
    This is the core of the Intent-Based Architecture (Phase 2).
    The LLM describes what it sees (text, zone, element type), and this
    function finds the matching DOM element by scoring candidate nodes.
    
    Args:
        intent: Dictionary with keys:
            - text_label: The exact visible text on the element
            - zone: Where it appears (nav, sidebar, main, header, footer)
            - element_type: What type (button, link, input, dropdown, span)
            - context: What it's near or what it does (optional)
        nodes: Current LiveGraph nodes to search through
        excluded_ids: Already-clicked IDs to avoid (to prevent loops)
    
    Returns:
        Tuple of (target_id, resolve_reason) on success, or (None, error_reason) on failure.
        The resolve_reason includes scoring info for debugging.
    
    Scoring Strategy:
        - Text match: +0.5 (most important - LLM saw this text)
        - Zone match: +0.3 (location context)
        - Element type match: +0.2 (semantic type)
        - Requires score >= 0.5 to succeed (at least text match)
    """
    text_label = intent.get("text_label", "")
    zone = intent.get("zone", "")
    element_type = intent.get("element_type", "")
    context = intent.get("context", "")
    
    # Scoring function for intent matching
    best_id: Optional[str] = None
    best_score = 0.0
    best_match_details = ""
    
    for n in nodes:
        # Check if interactive
        is_interactive = bool(getattr(n, "interactive", False))
        
        nid = str(getattr(n, "id", ""))
        if not nid or nid in excluded_ids:
            continue
        
        # Check zone match
        node_zone = getattr(n, "zone", "").lower()
        zone_match = zone.lower() in node_zone if zone else True
        
        # Check text match (most important)
        node_text = (getattr(n, "text", "") or "").lower()
        text_match = text_label.lower() in node_text if text_label else False
        
        # Check element type
        node_tag = getattr(n, "tag", "").lower()
        node_role = getattr(n, "role", "").lower()
        type_match = (
            element_type.lower() in node_tag or 
            element_type.lower() in node_role
        ) if element_type else True
        
        # Check context (optional bonus)
        context_match = False
        if context:
            context_lower = context.lower()
            # Check in text, aria labels, or nearby content
            node_aria = (getattr(n, "aria_label", "") or "").lower()
            if context_lower in node_text or context_lower in node_aria:
                context_match = True
        
        # Score calculation
        score = 0.15 if is_interactive else 0.0
        match_details = []
        if is_interactive:
            match_details.append("is_interactive=+0.15")
        
        if text_match:
            score += 0.5  # Text match is most important
            match_details.append(f"text_match={text_label}")
        
        if zone_match:
            score += 0.3
            match_details.append(f"zone_match={zone}")
        
        if type_match:
            score += 0.2
            match_details.append(f"type_match={element_type}")
        
        if context_match:
            score += 0.1  # Bonus for context match
            match_details.append(f"context_match={context}")
        
        # Exact text match gets a bonus
        if text_label and node_text.strip() == text_label.lower().strip():
            score += 0.2
            match_details.append("exact_text_match")
        
        if score > best_score:
            best_score = score
            best_id = nid
            best_match_details = ", ".join(match_details)
    
    if best_id and best_score >= 0.5:  # Require at least text match
        return best_id, f"intent_match_score_{best_score:.2f}({best_match_details})"

    return None, f"no_match_for_intent_text={text_label}_zone={zone}_type={element_type}"


def _extract_url_evidence(current_url: str, goal: str) -> Dict[str, Any]:
    """
    Extract evidence from URL parameters.
    
    Many dashboards encode filter state in URL (date ranges, tabs, models).
    This helps recognize when the goal is already satisfied even if visible
    text doesn't match exactly.
    
    Args:
        current_url: The current page URL
        goal: The user's goal statement
    
    Returns:
        Dictionary with extracted evidence:
        - date_range: {"from": str, "to": str, "days": int} if found
        - tab: str if found
        - model_filter: str if found
        - has_time_range: bool
    """
    from urllib.parse import urlparse, parse_qs
    import json
    import re
    from datetime import datetime
    
    evidence = {
        "has_time_range": False,
        "date_range": None,
        "tab": None,
        "model_filter": None,
    }
    
    try:
        parsed = urlparse(current_url)
        query_params = parse_qs(parsed.query)
        
        # Check for dateRange parameter (common pattern)
        if "dateRange" in query_params:
            date_range_str = query_params["dateRange"][0]
            # Try to parse JSON-encoded date range
            try:
                date_range = json.loads(date_range_str)
                if "from" in date_range and "to" in date_range:
                    # Calculate days between
                    from_date = datetime.fromisoformat(date_range["from"].replace("Z", "+00:00"))
                    to_date = datetime.fromisoformat(date_range["to"].replace("Z", "+00:00"))
                    days = (to_date - from_date).days + 1
                    
                    evidence["date_range"] = {
                        "from": date_range["from"],
                        "to": date_range["to"],
                        "days": days,
                    }
                    evidence["has_time_range"] = True
                    
                    # Check if it matches "last 7 days" goal
                    if "7 days" in goal.lower() or "last week" in goal.lower():
                        if 6 <= days <= 8:  # Allow 1 day tolerance
                            evidence["matches_goal"] = True
                            evidence["goal_match_reason"] = f"URL shows {days}-day range (goal: 7 days)"
            except (json.JSONDecodeError, ValueError):
                pass
        
        # Check for tab parameter
        if "tab" in query_params:
            evidence["tab"] = query_params["tab"][0]
        
        # Check for model filter
        if "model" in query_params:
            evidence["model_filter"] = query_params["model"][0]
        
    except Exception as e:
        pass  # Silently ignore URL parsing errors
    
    return evidence


def _should_force_click(text_label: str, context: str, node: Any) -> bool:
    """
    Determine if force_click should be enabled for reliable click execution.
    
    force_click uses simulated mouse events (mousedown → mouseup → click)
    which work better for:
    - Radix UI components (date pickers, dropdowns)
    - Overlay menus
    - Tab triggers
    - Custom button components
    
    Args:
        text_label: The text label from intent
        context: The context description from intent
        node: The DOM node being clicked
    
    Returns:
        True if force_click should be used, False otherwise
    """
    # Check for Radix UI patterns (most common cause of click failures)
    node_id = str(getattr(node, "id", ""))
    if "radix" in node_id.lower():
        return True
    
    # Check for date/time picker patterns
    date_keywords = ["date", "time", "calendar", "picker", "select", "range", "month", "year"]
    if any(kw in text_label.lower() for kw in date_keywords):
        return True
    if any(kw in context.lower() for kw in date_keywords):
        return True
    
    # Check for dropdown/menu patterns
    dropdown_keywords = ["dropdown", "menu", "option", "select", "trigger", "combobox"]
    if any(kw in context.lower() for kw in dropdown_keywords):
        return True
    
    # Check for tab and common navigation patterns
    if any(kw in context.lower() for kw in ["tab", "trigger", "activity", "cost"]):
        return True
    if "trigger" in node_id.lower() or "tab" in node_id.lower():
        return True
    
    # Check element role
    node_role = getattr(node, "role", "").lower()
    if node_role in ["tab", "menuitem", "option", "combobox"]:
        return True
    
    return False


def _list_available_elements(nodes: List[Any], limit: int = 10) -> str:
    """
    List clickable elements for debugging intent resolution failures.
    
    This helps the LLM understand what elements are actually available
    when its intent description doesn't match anything.
    
    Args:
        nodes: Current LiveGraph nodes
        limit: Maximum number of elements to list (default 10)
    
    Returns:
        Formatted string listing interactive elements with their text, zone, and tag.
    """
    clickable = []
    for n in nodes:
        if getattr(n, "interactive", False):
            text = (getattr(n, "text", "") or "")[:40]
            zone = getattr(n, "zone", "")
            tag = getattr(n, "tag", "")
            nid = getattr(n, "id", "")
            clickable.append(f"- id={nid}: '{text}' (zone={zone}, tag={tag})")
    return "\n".join(clickable[:limit])


def _is_clickable_interactive_node(node: Any) -> bool:
    """Strict clickability gate: exists, interactive, not hidden, and clickable semantics."""
    if not node:
        return False
    if not bool(getattr(node, "interactive", False)):
        # RELAXATION: Allow common text/content tags if they have text and are not obviously disabled
        tag = (getattr(node, "tag", "") or "").lower()
        if tag not in {"span", "div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i"}:
            return False
        if not (getattr(node, "text", "") or "").strip():
            return False
        # If it's one of these, we'll allow it if it has text and isn't hidden
    state = (getattr(node, "state", "") or "").lower()
    if any(flag in state for flag in ("disabled", "aria-disabled", "readonly", "hidden")):
        return False
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    return (tag in _CLICK_TAGS) or (role in _CLICK_ROLES)


def _is_question_like(goal: str) -> bool:
    g = (goal or "").strip().lower()
    return (
        "?" in g
        or g.startswith(("what ", "how ", "why ", "which ", "who ", "when ", "where "))
    )


def _requires_interaction_before_completion(goal: str, schema_action: str) -> bool:
    """
    Generalized intent gate:
    - For action-oriented intents, require at least one concrete UI interaction
      before allowing completion.
    - For direct question intents, allow read-and-answer completion.
    """
    g = (goal or "").strip().lower()
    action = (schema_action or "").strip().lower()

    if not g:
        return False
    if _is_question_like(g):
        return False

    # EXCEPTION: If the goal contains extraction keywords, it's information-seeking, not purely action-oriented.
    # e.g. "show me my usage" vs "show me the dashboard"
    extraction_keywords = ("usage", "cost", "tokens", "spend", "balance", "limit", "status", "value", "price", "metric", "analytics", "stats", "history", "report", "logs", "details", "quota")
    if any(k in g for k in extraction_keywords):
        return False

    # Explicit action verbs from user intent
    action_phrases = (
        "show me",
        "open ",
        "click ",
        "select ",
        "choose ",
        "play ",
        "watch ",
        "go to ",
        "navigate ",
        "take me ",
        "find me ",
        "create ",
        "make ",
    )
    if any(p in g for p in action_phrases):
        return True

    # Schema-level action intents that are typically interaction-driven
    if action in {"navigation", "interaction", "purchase", "search"}:
        return True

    return False


_GOAL_STOPWORDS = {
    "show", "me", "find", "open", "click", "select", "choose", "go", "to", "navigate",
    "take", "watch", "play", "view", "please", "the", "a", "an", "in", "with", "on",
    "for", "from", "of", "is", "are", "at", "create", "make", "generate", "new",
}


def _goal_entity_and_qualifier_terms(goal: str) -> Tuple[List[str], List[str]]:
    g = (goal or "").strip().lower()
    if not g:
        return [], []
    g = re.sub(
        r"^(show me|find me|find|show|open|click|select|choose|go to|navigate to|take me to|create me|create|make me|make|generate me|generate|new)\s+",
        "",
        g,
    )
    parts = re.split(r"\b(?:in|with|wearing|on|from)\b", g, maxsplit=1)
    entity_part = parts[0] if parts else g
    qualifier_part = parts[1] if len(parts) > 1 else ""
    entity_terms = [t for t in re.findall(r"[a-z0-9]+", entity_part) if len(t) > 2 and t not in _GOAL_STOPWORDS]
    qualifier_terms = [t for t in re.findall(r"[a-z0-9]+", qualifier_part) if len(t) > 2 and t not in _GOAL_STOPWORDS]
    return entity_terms[:5], qualifier_terms[:5]


def _weak_presence_claim(text: str) -> bool:
    t = (text or "").lower()
    has_presence = bool(re.search(r"\b(is present|present|found|listed|available|exists)\b", t))
    has_concrete = bool(re.search(r"\b(clicked|opened|selected|image|photo|gallery|result|showing)\b", t))
    return has_presence and not has_concrete


def _text_has_terms(text: str, terms: List[str], min_hits: int = 1) -> bool:
    if not terms:
        return True
    t = (text or "").lower()
    hits = sum(1 for term in terms if term in t)
    return hits >= min_hits


def _dom_has_entity_qualifier_evidence(nodes: List[Any], entity_terms: List[str], qualifier_terms: List[str]) -> bool:
    if not entity_terms:
        return False
    for n in nodes:
        txt = (getattr(n, "text", "") or "").strip().lower()
        if not txt:
            continue
        if not _text_has_terms(txt, entity_terms, min_hits=1):
            continue
        if qualifier_terms and not _text_has_terms(txt, qualifier_terms, min_hits=1):
            continue
        return True
    return False


def _verify_entity_anchor(evidence_blob: str, entity_terms: List[str]) -> bool:
    """
    LogicCritic Anchor Verification:
    Ensures that the evidence actually contains the core entity requested
    by the user, preventing 'hallucinated success' where the agent claims
    an answer found on a page that lacks the entity.
    """
    if not entity_terms:
        return True
    
    # LogicCritic Rule: The evidence must contain at least one Hard Anchor from the goal.
    evidence_clean = re.sub(r"[^a-z0-9\s]", " ", (evidence_blob or "").lower())
    for term in entity_terms:
        if term in evidence_clean:
            return True
            
    return False


def _is_search_like_node(node: Any) -> bool:
    if node is None:
        return False
    text = (getattr(node, "text", "") or "").lower()
    node_id = (getattr(node, "id", "") or "").lower()
    tag = (getattr(node, "tag", "") or "").lower()
    return (
        "search" in text
        or "search" in node_id
        or tag == "input"
    )


def _best_entity_click_target(nodes: List[Any], entity_terms: List[str], excluded_ids: Optional[set] = None) -> Optional[Any]:
    if not entity_terms:
        return None
    best_node = None
    best_score = 0
    for n in nodes:
        if excluded_ids and getattr(n, "id", "") in excluded_ids:
            continue
        if not getattr(n, "interactive", False):
            continue
        txt = (getattr(n, "text", "") or "").lower()
        if not txt:
            continue
        score = sum(1 for term in entity_terms if term in txt)
        if score <= 0:
            continue
        if score > best_score:
            best_score = score
            best_node = n
    return best_node


def _resolve_target_label(nodes: List[Any], target_id: str) -> str:
    if not target_id:
        return ""
    node = next((n for n in nodes if str(getattr(n, "id", "")) == str(target_id)), None)
    if not node:
        return ""
    return (getattr(node, "text", "") or "").strip()[:120]


def _is_same_section_reclick_blocked(nodes: List[Any], target_node: Any) -> bool:
    """
    Prevent recursive nav clicks (e.g., clicking 'Usage' while Usage is already active).
    DOM is source-of-truth for state; vision hints are advisory.
    """
    if target_node is None:
        return False
    zone = (getattr(target_node, "zone", "") or "").lower()
    if zone not in {"nav", "sidebar"}:
        return False

    text = (getattr(target_node, "text", "") or "").lower().strip()
    if not text:
        return False

    section_keywords = {"usage", "dashboard", "billing", "activity", "models"}
    matched = [kw for kw in section_keywords if kw in text]
    if not matched:
        return False

    # If target itself is marked active/current/selected, reclick is redundant.
    state = (getattr(target_node, "state", "") or "").lower()
    if any(flag in state for flag in ("active", "current", "selected")):
        return True

    # If readable/main content already contains section cues, treat nav reclick as non-progress.
    for n in nodes:
        zone_n = (getattr(n, "zone", "") or "").lower()
        if zone_n not in {"main", "content"}:
            continue
        txt = (getattr(n, "text", "") or "").lower()
        if not txt:
            continue
        if any(kw in txt for kw in matched):
            return True
    return False


def _infer_active_section(nodes: List[Any], current_url: str = "") -> str:
    # Prefer explicit active/current nav/sidebar node
    for n in nodes:
        zone = (getattr(n, "zone", "") or "").lower()
        if zone not in {"nav", "sidebar"}:
            continue
        state = (getattr(n, "state", "") or "").lower()
        if any(flag in state for flag in ("active", "current", "selected")):
            txt = (getattr(n, "text", "") or "").strip()
            if txt:
                return txt[:80]
    # Fallback to URL path segment
    url = (current_url or "").lower()
    for key in ("usage", "dashboard", "billing", "models", "activity"):
        if f"/{key}" in url:
            return key
    return "unknown"


def _filter_vision_hints(hints: Dict[str, Any], nodes: List[Any], excluded_ids: set) -> Dict[str, Any]:
    """Remove stale/redundant targets before hints are injected back into LLM context."""
    excluded_ids = excluded_ids or set()
    filtered = dict(hints or {})
    candidates = []
    for t in (hints.get("candidate_targets") or []):
        tid = str(t.get("target_id") or "").strip()
        node = next((n for n in nodes if str(getattr(n, "id", "")) == tid), None)
        if not tid or tid in excluded_ids:
            continue
        if not _is_clickable_interactive_node(node):
            continue
        if _is_same_section_reclick_blocked(nodes, node):
            continue
        candidates.append(t)

    actions = []
    for a in (hints.get("recommended_actions") or []):
        tool = str(a.get("tool") or "").strip()
        tid = str(a.get("target_id") or "").strip()
        if tool == "click_element":
            node = next((n for n in nodes if str(getattr(n, "id", "")) == tid), None)
            if not tid or tid in excluded_ids:
                continue
            if not _is_clickable_interactive_node(node):
                continue
            if _is_same_section_reclick_blocked(nodes, node):
                continue
        actions.append(a)

    filtered["candidate_targets"] = candidates
    filtered["recommended_actions"] = actions
    if filtered.get("best_target_id"):
        best_id = str(filtered.get("best_target_id") or "").strip()
        best_node = next((n for n in nodes if str(getattr(n, "id", "")) == best_id), None)
        if best_id in excluded_ids or _is_same_section_reclick_blocked(nodes, best_node):
            if actions:
                first = actions[0]
                filtered["best_target_id"] = str(first.get("target_id") or "")
                filtered["recommended_tool"] = str(first.get("tool") or filtered.get("recommended_tool") or "")
            else:
                filtered["best_target_id"] = ""
    return filtered


def _render_vision_reasoning_brief(hints: Dict[str, Any], nodes: List[Any]) -> str:
    """Render vision analysis into observation-first plain text for the compound loop."""
    answer_visible = bool(hints.get("answer_visible"))
    best_target = str(hints.get("best_target_id") or "").strip()
    best_target_label = str(hints.get("best_target_label") or "").strip()
    if best_target and not best_target_label:
        best_target_label = _resolve_target_label(nodes, best_target)
    recommended_tool = str(hints.get("recommended_tool") or "").strip() or "read_page_content"
    evidence_summary = str(hints.get("evidence_summary") or "").strip()
    blocking_reason = str(hints.get("blocking_reason") or "").strip()
    page_mode = str(hints.get("page_mode") or "").strip()
    uncertainty = str(hints.get("uncertainty_summary") or "").strip()
    candidate_targets = hints.get("candidate_targets") or []
    actions = hints.get("recommended_actions") or []

    lines: List[str] = []
    lines.append("Vision Strategic Brief:")
    lines.append("- Intent: report what is visibly present and what is still missing.")
    lines.append(f"- Answer visible now: {'yes' if answer_visible else 'no'}")
    if page_mode:
        lines.append(f"- Visible page mode: {page_mode}")
    if evidence_summary:
        lines.append(f"- What is visible: {evidence_summary}")
    if blocking_reason:
        lines.append(f"- What is missing: {blocking_reason}")
    if uncertainty:
        lines.append(f"- Uncertainty: {uncertainty}")
    if best_target:
        if best_target_label:
            lines.append(f"- Strongest visible control: {best_target_label} ({best_target})")
        else:
            lines.append(f"- Strongest visible control: {best_target}")
    if candidate_targets:
        lines.append("- Ranked visible controls:")
        for t in sorted(candidate_targets, key=lambda x: int(x.get("priority", 99)))[:3]:
            tid = str(t.get("target_id") or "").strip()
            lbl = str(t.get("label") or "").strip()
            if not lbl:
                lbl = _resolve_target_label(nodes, tid)
            why = str(t.get("why") or "").strip()
            p = int(t.get("priority", 99))
            lines.append(f"  - P{p}: {lbl or 'unknown'} ({tid or 'n/a'}) — {why or 'viable route'}")
    lines.append(f"- Safest next probe: {recommended_tool}")
    if actions:
        lines.append("- Advisory probes:")
        for idx, a in enumerate(actions[:3], start=1):
            tool = a.get("tool") or "read_page_content"
            target = a.get("target_id") or "n/a"
            target_label = (a.get("target_label") or "").strip()
            if target and target != "n/a" and not target_label:
                target_label = _resolve_target_label(nodes, target)
            text = a.get("text") or ""
            seconds = a.get("seconds", 2)
            force_click = a.get("force_click", False)
            why = (a.get("why") or "").strip()
            if tool == "click_element":
                target_desc = f"{target_label} ({target})" if target_label else target
                lines.append(f"  {idx}. click -> target={target_desc} force_click={force_click} ({why or 'probe this control'})")
            elif tool == "type_text":
                target_desc = f"{target_label} ({target})" if target_label else target
                lines.append(f"  {idx}. type -> target={target_desc} text='{text}' press_enter={bool(a.get('press_enter', False))} ({why or 'refine results'})")
            elif tool == "wait_for_ui":
                lines.append(f"  {idx}. wait -> {seconds}s ({why or 'allow UI update'})")
            elif tool == "scroll_page":
                lines.append(f"  {idx}. scroll -> direction={a.get('direction', 'down')} ({why or 'reveal more content'})")
            elif tool == "read_page_content":
                lines.append(f"  {idx}. read_page_content ({why or 'verify evidence after UI change'})")
            else:
                lines.append(f"  {idx}. {tool} ({why})")
    return "\n".join(lines)


async def _call_groq_vision(
    image_b64: str,
    reason: str,
    nodes: List[Any],
    app: Any = None,
) -> str:
    """
    Call Groq llama-4-scout-17b-16e-instruct with a base64 screenshot.
    Returns a text description grounded against known DOM IDs.
    """
    llm = None
    if app and hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
        llm = app.state.mind_reader.llm

    # Build grounded prompt — DOM IDs are advisory anchors, not a strict execution contract.
    interactive_ids = []
    for n in nodes[:80]:
        if getattr(n, "interactive", False):
            text = (getattr(n, "text", "") or "")[:60]
            tag = getattr(n, "tag", "")
            nid = getattr(n, "id", "")
            interactive_ids.append(f"[{nid}] {tag}: '{text}'")
    id_context = "\n".join(interactive_ids) if interactive_ids else "(no interactive elements)"

    vision_prompt = (
        f"Goal: {reason}\n\n"
        "You are TARA's visual reasoning engine. Look at the screenshot and think through what you see.\n\n"
        "Provide your analysis in a natural, reasoning-based format. Think step by step:\n\n"
        "1. OBSERVE: What is currently visible on the screen? Describe the page layout, key elements, and content.\n"
        "2. ASSESS: Given the goal above, is the answer or information we need already visible? Why or why not?\n"
        "3. REASON: If the answer is not visible, what would be the most logical next step? Consider:\n"
        "   - Are there navigation elements, tabs, or menus that might lead to the answer?\n"
        "   - Is there a search field or filter that could help narrow down results?\n"
        "   - Are there expandable sections or clickable elements that might reveal more information?\n"
        "   - Is the page still loading or in a state where waiting would be appropriate?\n"
        "4. RECOMMEND: Based on your reasoning, what specific action should be taken next?\n\n"
        "Format your response naturally as a brief narrative (3-5 sentences), but include these precise key phrases:\n"
        "- \"Answer visible now: yes\" or \"Answer visible now: no\"\n"
        "- \"Visible page mode: [type]\" (e.g. dashboard, menu, documentation)\n"
        "- \"Strongest visible control: [Label] ([ID])\" (e.g. Usage (t-123))\n"
        "- \"Safest next probe: [tool]\" (e.g. click_element, read_page_content)\n"
        "- \"Action Plan: [Step 1], [Step 2]\" (e.g. Action Plan: click t-123 (dropdown), click t-456 (item))\n"
        "- \"Confidence: [high|medium|low]\"\n\n"
        "**BUNDLE RULE**: For popups, dropdowns, or search fields, you are ENCOURAGED to propose a multi-step Action Plan (e.g., click dropdown THEN click item) so they can be executed together.\n\n"
        "Be honest about uncertainty. If the screen is ambiguous or unclear, say so and explain why.\n"
        "Do not force a recommendation if the situation is unclear.\n\n"
        "Available DOM element IDs for reference (use only if clearly visible in screenshot):\n"
        f"{id_context}\n\n"
        "Remember: Think first, observe carefully, reason through the options, then recommend."
    )

    # Primary path: provider wrapper
    if llm and hasattr(llm, "generate_vision"):
        try:
            result = await llm.generate_vision(
                text_prompt=vision_prompt,
                image_b64=image_b64,
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                max_tokens=320,
                temperature=0.2,
            )
            return result
        except Exception as e:
            logger.warning(f"[VISION] provider failed, trying direct Groq HTTP fallback: {e}")

    # Fallback path: direct Groq HTTP (same reliability pattern as pre-router/analyse_page)
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        return "System: Groq Vision unavailable (missing API key). Rely on provided DOM text."

    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are TARA's visual observer. "
                    "Return concise observation-first plain text grounded to visible UI and provided DOM IDs."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": vision_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            },
        ],
        "max_tokens": 320,
        "temperature": 0.2,
    }
    try:
        limiter = _get_vision_rate_limiter()
        async with limiter:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(
                    GROQ_ENDPOINT,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
                result = resp.json()["choices"][0]["message"]["content"].strip()
        return result
    except Exception as e:
        logger.error(f"[VISION] direct fallback call failed: {e}")
        return f"System: Groq Vision call failed ({e}). Rely on the provided DOM text."


def escalate_to_human(
    blockage_type: str,
    speech: str,
    ask: str,
    diagnostics: Dict[str, Any],
    resume_context: Dict[str, Any],
    suggested_resolutions: List[str],
    **kwargs
) -> Dict[str, Any]:
    """Escalate to human with structured context.

    This replaces vague 'clarify' with specific escalation.
    """
    logger.warning(
        f"TOOL_ESCALATE blockage={blockage_type} "
        f"ask='{ask[:50]}...' "
        f"resolutions={len(suggested_resolutions)}"
    )

    return {
        "type": "escalate",
        "blockage_type": blockage_type,
        "speech": speech,
        "ask": ask,
        "diagnostics": diagnostics,
        "resume_context": resume_context,
        "suggested_resolutions": suggested_resolutions,
        "terminal": True,  # This is a terminal action
    }


async def execute_internal_tool(
    tool_name: str,
    args: Dict[str, Any],
    nodes: List[Any],
    screenshot_b64: Optional[str] = None,
    app: Any = None,
    session_id: str = "",
    excluded_ids: set = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Executes the LLM's chosen tool.

    Args:
        tool_name: Name of the tool the LLM called.
        args: Parsed tool arguments.
        nodes: Current LiveGraph nodes for ID validation.
        screenshot_b64: Pre-captured screenshot (optional, may be None/stale).
        app: FastAPI app — used for LLM access and WebSocket registry lookup.
        session_id: Current session ID for screenshot broker routing.

    Returns:
        is_terminal (bool): If True, break the internal loop and return to frontend.
        tool_result_str (str): Text to feed back to LLM if non-terminal.
        frontend_action (dict | None): Payload to return to frontend if terminal.
    """
    logger.info(f"[TOOL_EXEC] {tool_name}")

    # ── read_page_content: Non-terminal tool to re-examine visible content ──
    if tool_name == "read_page_content":
        focus = args.get("focus", "")
        readable = [
            n for n in nodes
            if getattr(n, "text", None)
            and len((getattr(n, "text", "") or "").strip()) >= 8
            and getattr(n, "zone", "") in {"main", "content"}
        ][:60]
        
        if focus:
            focus_terms = set(re.findall(r"[a-z0-9]+", focus.lower()))
            scored = []
            for n in readable:
                txt = (getattr(n, "text", "") or "").strip()
                tokens = set(re.findall(r"[a-z0-9]+", txt.lower()))
                overlap = len(tokens & focus_terms)
                scored.append((overlap, txt))
            scored.sort(key=lambda x: -x[0])
            result_lines = [txt[:250] for overlap, txt in scored[:15] if overlap > 0]
            if not result_lines:
                result_lines = [txt[:250] for _, txt in scored[:10]]
        else:
            result_lines = [(getattr(n, "text", "") or "").strip()[:250] for n in readable[:15]]

        content_text = "\n".join(result_lines) if result_lines else "(no readable content found on page)"
        
        # If we have good content, suggest the LLM extract an answer
        if result_lines and len(result_lines) >= 3:
            content_text += (
                "\n\n[SYSTEM: The content above contains information relevant to the goal. "
                "After reviewing this, call complete_mission with a comprehensive answer "
                "extracted from these excerpts.]"
            )

        logger.debug(f"[READ_CONTENT] focus='{focus}' results={len(result_lines)}")
        return False, f"Page Content (focus: {focus or 'general'}):\n{content_text}", None

    if tool_name == "click_element":
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4: Intent-Based Action Resolution
        # Check if this is an intent-based action (preferred) or direct ID-based
        # ═══════════════════════════════════════════════════════════════════
        
        # NEW: Check if intent-based action
        if "intent" in args and args.get("intent"):
            intent = args["intent"]
            target_id = args.get("target_id", "")
            
            # If no target_id provided, resolve intent to ID
            if not target_id or not _is_valid_id(target_id, nodes):
                logger.debug(f"[INTENT_RESOLVE] click: resolving intent text_label='{intent.get('text_label', '')}'")

                
                resolved_id, resolve_reason = _resolve_intent_to_target_id(
                    intent=intent,
                    nodes=nodes,
                    excluded_ids=set(excluded_ids) if excluded_ids else set(),
                )
                
                if not resolved_id:
                    logger.warning(f"[INTENT_RESOLVE] click failed: {resolve_reason}")
                    return (
                        False,
                        f"Could not find element matching intent: {intent}. "
                        f"Reason: {resolve_reason}. "
                        f"Available clickable elements:\n{_list_available_elements(nodes, limit=15)}",
                        None,
                    )
                
                logger.debug(f"[INTENT_RESOLVE] click: resolved -> {resolved_id} ({resolve_reason})")
                target_id = resolved_id
                args["target_id"] = target_id  # Update for downstream logic
            else:
                logger.debug(f"[INTENT_RESOLVE] click: using explicit target_id={target_id}")
        else:
            # Legacy path: direct target_id
            target_id = args.get("target_id", "")
        
        # Continue with existing validation using resolved/provided target_id
        if not _is_valid_id(target_id, nodes):
            logger.warning(f"[GUARDRAIL] hallucinated ID: {target_id}")
            return (
                False,
                f"Error: ID '{target_id}' does not exist in the current DOM. "
                f"Please review the provided elements and use a valid ID, "
                f"or call request_vision to see the page visually.",
                None,
            )
        if excluded_ids and target_id in excluded_ids:
            logger.warning(f"[GUARDRAIL] re-click blocked: {target_id}")
            return (
                False,
                f"Error: ID '{target_id}' was already clicked in a previous step. "
                f"Do NOT click it again. Choose a different element or use type_text to search instead.",
                None,
            )

        target_node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)

        # Strict clickability gate: reject non-clickable/hidden generic elements.
        # EXCEPTION: If force_click is True (vision override), bypass this gate.
        force_click = args.get("force_click", False)
        if not force_click and not _is_clickable_interactive_node(target_node):
            logger.warning(f"[CLICKABILITY_GATE] rejected {target_id} tag='{getattr(target_node, 'tag', '') if target_node else ''}'")

            return (
                False,
                f"REJECTED: Target '{target_id}' is not a clickable control on the current page. "
                "Pick a real button/link/tab/menu item (not plain text/metric value), or use read_page_content first.",
                None,
            )

        # Entity-priority guard: for action goals, prefer the visible entity target over generic nav drift.
        main_goal = str(args.get("_main_goal", "") or "")
        schema_action = str(args.get("_schema_action", "") or "")
        entity_terms, qualifier_terms = _goal_entity_and_qualifier_terms(main_goal)

        target_text = (getattr(target_node, "text", "") or "").lower() if target_node else ""
        target_has_entity = _text_has_terms(target_text, entity_terms, min_hits=1) if entity_terms else False
        target_is_search = _is_search_like_node(target_node) if target_node else False

        # Anti-recursion guard: block redundant sidebar/nav section reclicks.
        if not force_click and _is_same_section_reclick_blocked(nodes, target_node):
            logger.warning(f"[SECTION_RECLICK_GUARD] blocked {target_id}")
            return (
                False,
                "REJECTED: You are already in this section. Look for local controls.",
                None,
            )

        if (
            _requires_interaction_before_completion(main_goal, schema_action)
            and entity_terms
            and not target_has_entity
            and schema_action not in ("navigation", "extraction")   # ← Don't override Vision for nav goals
        ):
            best_entity = _best_entity_click_target(nodes, entity_terms, excluded_ids)
            if best_entity is not None:
                best_id = str(getattr(best_entity, "id", "") or "")
                if best_id and best_id != target_id:
                    logger.debug(f"[ENTITY_GUARD] override {target_id} -> {best_id}")
                    target_id = best_id
                    target_node = best_entity
                    target_text = (getattr(target_node, "text", "") or "").lower()
                    target_has_entity = True
            elif not target_is_search:
                qualifier_hint = " ".join(qualifier_terms[:2]) if qualifier_terms else "requested qualifier"
                logger.warning(f"[ENTITY_GUARD] blocked generic click {target_id}")
                return (
                    False,
                    "REJECTED: Entity target is not visible yet. Do not click generic navigation. "
                    f"Use search controls or a list/filter likely to reveal the entity and {qualifier_hint}.",
                    None,
                )


        # Track click for session-level pattern detection
        if session_id:
            tracker = get_click_tracker(session_id)
            node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
            label = (getattr(node, "text", "") or "") if node else ""
            tracker.record_click(target_id, label)

        # ═══════════════════════════════════════════════════════════════════
        # AUTO force_click DETECTION FOR RELIABLE CLICK EXECUTION
        # ═══════════════════════════════════════════════════════════════════
        # For Radix UI, date pickers, dropdowns, tabs, use simulated mouse events
        # This fixes the "ghost cursor clicks but nothing happens" issue
        force_click = args.get("force_click", False)
        
        # Auto-detect when force_click is needed (even if not explicitly set)
        if not force_click and target_node:
            text_label = args.get("intent", {}).get("text_label", "") if "intent" in args else ""
            context = args.get("intent", {}).get("context", "") if "intent" in args else ""
            
            if _should_force_click(text_label, context, target_node):
                force_click = True
                logger.debug(f"[CLICK] force_click auto-enabled for {target_id}")

        return False, f"Action 'click_element' on '{target_id}' queued. You may queue more actions, or call `wait_for_ui` to execute the queue.", {
            "type": "click",
            "target_id": target_id,
            "text": (getattr(target_node, "text", "") or "").strip()[:120] if target_node else "",
            "speech": args.get("why", "Clicking element."),
            "force_click": force_click,
        }

    elif tool_name == "type_text":
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4: Intent-Based Action Resolution for type_text
        # ═══════════════════════════════════════════════════════════════════
        
        # Check if intent-based action
        if "intent" in args and args.get("intent"):
            intent = args["intent"]
            target_id = args.get("target_id", "")
            
            # If no target_id provided, resolve intent to ID
            if not target_id or not _is_valid_id(target_id, nodes):
                logger.debug(f"[INTENT_RESOLVE] type: resolving intent text_label='{intent.get('text_label', '')}'")

                
                resolved_id, resolve_reason = _resolve_intent_to_target_id(
                    intent=intent,
                    nodes=nodes,
                    excluded_ids=set(excluded_ids) if excluded_ids else set(),
                )
                
                if not resolved_id:
                    logger.warning(f"[INTENT_RESOLVE] type failed: {resolve_reason}")
                    return (
                        False,
                        f"Could not find input element matching intent: {intent}. "
                        f"Reason: {resolve_reason}. "
                        f"Available clickable elements:\n{_list_available_elements(nodes, limit=15)}",
                        None,
                    )
                
                logger.debug(f"[INTENT_RESOLVE] type: resolved -> {resolved_id} ({resolve_reason})")
                target_id = resolved_id
                args["target_id"] = target_id
            else:
                logger.debug(f"[INTENT_RESOLVE] type: using explicit target_id={target_id}")
        else:
            # Legacy path: direct target_id
            target_id = args.get("target_id", "")
        
        text = args.get("text", "")
        press_enter = args.get("press_enter", True)
        if not _is_valid_id(target_id, nodes):
            logger.warning(f"[GUARDRAIL] hallucinated ID: {target_id}")
            return (
                False,
                f"Error: ID '{target_id}' does not exist in the current DOM. "
                f"Please review the provided elements.",
                None,
            )
        return False, f"Action 'type_text' on '{target_id}' queued. Call `wait_for_ui` to execute the queue.", {
            "type": "type_text",
            "target_id": target_id,
            "text": text,
            "press_enter": press_enter,
            "speech": f"Typing '{text}'.",
        }

    elif tool_name == "scroll_page":
        direction = args.get("direction", "down")
        return False, f"Action 'scroll_page' queued. Call `wait_for_ui` to execute.", {"type": "scroll", "direction": direction, "speech": "Scrolling to reveal more content."}

    elif tool_name == "wait_for_ui":
        seconds = float(args.get("seconds", 2) or 2)
        return True, "", {"type": "wait", "seconds": seconds, "wait_ms": int(seconds * 1000), "speech": "Waiting briefly for the page to update."}

    elif tool_name == "request_vision":
        # ═══════════════════════════════════════════════════════════════════
        # REAL VISION — Groq llama-4-scout-17b-16e-instruct
        #
        # Flow:
        #   1. Try to get a fresh screenshot via the broker (WS round-trip)
        #   2. Fall back to pre-captured screenshot_b64 if broker fails
        #   3. Call Groq Vision API with the image + mission context
        #   4. Parse structured hints from vision response
        #   5. Return result as a tool observation (non-terminal → LLM re-reasons)
        # ═══════════════════════════════════════════════════════════════════
        reason = args.get("reason", "general page analysis")
        current_url = str(args.get("_current_url", "") or "")
        goal_url = str(args.get("_goal_url", "") or "")
        user_goal = str(args.get("_user_goal", "") or "")
        mission_ctx = str(args.get("_mission_ctx", "") or "")  # compact mission state block
        already_clicked_ids = set(args.get("_already_clicked_ids") or [])
        active_section = _infer_active_section(nodes, current_url)
        recent_actions = []
        if session_id:
            recent_actions = get_click_tracker(session_id).click_history[-5:]
        action_trace = ", ".join(
            [f"{a.get('label','')}({a.get('id','')})" for a in recent_actions if isinstance(a, dict)]
        ) or "none"

        # Build enriched context for the vision model.
        # If a mission_ctx block was passed from the compound loop, prepend it so
        # vision knows exactly which subgoals are done and what NOT to suggest.
        state_block = ""
        if mission_ctx:
            state_block = f"\n{mission_ctx}\n"

        enriched_reason = (
            f"{reason}\n"
            f"{state_block}"
            f"Original user goal: {user_goal or 'unknown'}\n"
            f"Current URL: {current_url or 'unknown'}\n"
            f"Goal URL: {goal_url or 'unknown'}\n"
            f"Active section: {active_section}\n"
            f"Recent successful clicks: {action_trace}\n"
            f"Already-clicked IDs: {', '.join(sorted(already_clicked_ids)) if already_clicked_ids else 'none'}\n"
            "Do not suggest clicking a sidebar/nav section that is already active or listed as a completed subgoal."
        )
        logger.info(f"[VISION] requested: reason='{reason[:80]}' url={current_url[:60]}")


        image_b64 = None

        # 1) Prefer a fresh on-demand screenshot via WebSocket broker (current UI at tool time)
        if app and session_id:
            try:
                from visual_copilot.mission.screenshot_broker import request_screenshot
                image_b64 = await request_screenshot(
                    app=app,
                    session_id=session_id,
                        reason=enriched_reason,
                )
                if image_b64:
                    logger.debug("[VISION] using fresh broker screenshot")
            except Exception as e:
                logger.error(f"[VISION] screenshot broker error: {e}")

        # 2) Fallback to screenshot from current request
        if not image_b64 and screenshot_b64:
            logger.debug("[VISION] using pre-captured screenshot fallback")
            image_b64 = screenshot_b64

        # 3) Then try session-level cached screenshot saved by app.py
        if not image_b64 and app and session_id:
            try:
                cache = getattr(app.state, "latest_screenshots", None) or {}
                cached_b64 = cache.get(session_id)
                if cached_b64:
                    logger.debug("[VISION] using cached screenshot fallback")
                    image_b64 = cached_b64
            except Exception as e:
                logger.warning(f"[VISION] screenshot cache lookup failed: {e}")

        if image_b64:
            # ── Screenshot Dedup: skip vision API if screenshot unchanged ──
            ss_hash = _hash_screenshot(image_b64)
            cached_vision = _get_cached_vision(session_id or "", ss_hash) if session_id else None
            if cached_vision:
                logger.info(f"[VISION] dedup_hit: reusing cached result (hash={ss_hash})")
                vision_result = cached_vision
            else:
                vision_result = await _call_groq_vision(
                    image_b64=image_b64,
                    reason=enriched_reason,
                    nodes=nodes,
                    app=app,
                )
                if session_id:
                    _set_vision_cache(session_id, ss_hash, vision_result)
            logger.debug(f"[VISION] raw result length={len(vision_result)}")
            # Parse structured hints from vision response
            try:
                from visual_copilot.mission.screenshot_broker import parse_vision_response
                hints = parse_vision_response(vision_result)
                hints = _filter_vision_hints(hints, nodes, excluded_ids or already_clicked_ids)
                hint_suffix = (
                    f"\n\n**Vision Hints**: answer_visible={hints['answer_visible']}, "
                    f"best_target={hints['best_target_id'] or 'none'}, "
                    f"recommended_tool={hints['recommended_tool']}"
                )
                brief_parts = []
                if hints.get("evidence_summary"):
                    brief_parts.append(f"visible_now={hints.get('evidence_summary')}")
                if hints.get("blocking_reason"):
                    brief_parts.append(f"missing={hints.get('blocking_reason')}")
                if hints.get("uncertainty_summary"):
                    brief_parts.append(f"uncertainty={hints.get('uncertainty_summary')}")
                if brief_parts:
                    hint_suffix += "\n\n**Vision Brief:** " + " | ".join(brief_parts)
                actions = hints.get("recommended_actions") or []
                vision_result += hint_suffix
                brief_text = _render_vision_reasoning_brief(hints, nodes)
                logger.info(
                    f"[VISION] answer_visible={hints['answer_visible']} "
                    f"target={hints['best_target_id']} tool={hints['recommended_tool']} "
                    f"plan_steps={len(actions)}"
                )
                # Use plain-text reasoning brief in compound loop context (avoid raw JSON blobs).
                vision_result = brief_text
            except Exception as hint_err:
                logger.warning(f"[VISION] hint parsing failed: {hint_err}")

            return False, vision_result, None
        else:
            return (
                False,
                "System: No screenshot could be captured from the browser (WebSocket unavailable or timeout). "
                "Please rely ONLY on the LiveGraph DOM text provided in the mission brief. "
                "Call complete_mission with whatever evidence you have, or acknowledge stuck state.",
                None,
            )

    elif tool_name == "complete_mission":
        status = args.get("status", "success")
        response = args.get("response", "Task completed.")
        evidence_refs = args.get("evidence_refs", "")
        answer_confidence = args.get("answer_confidence", "medium")
        main_goal = str(args.get("_main_goal", "") or "")
        target_entity = str(args.get("_target_entity", "") or "").strip()
        schema_action = str(args.get("_schema_action", "") or "")
        current_url = str(args.get("_current_url", "") or "")
        session_clicks = 0
        if session_id:
            session_clicks = len(get_click_tracker(session_id).click_history)

        # ═══════════════════════════════════════════════════════════════════
        # URL EVIDENCE CHECK - Recognize success from URL parameters
        # ═══════════════════════════════════════════════════════════════════
        # Many dashboards encode filter state in URL (date ranges, tabs, models).
        # If URL shows the goal is satisfied, allow completion even without
        # explicit visible text evidence.
        url_evidence = _extract_url_evidence(current_url, main_goal)
        if url_evidence.get("matches_goal"):
            logger.info(f"[COMPLETION_GATE] URL evidence match: {url_evidence.get('goal_match_reason')}")
            # URL confirms goal is met - allow completion
            # Inject URL evidence into response if not already present
            if not evidence_refs or len(evidence_refs) < 20:
                evidence_refs = url_evidence.get("goal_match_reason", current_url)
            if not response or len(response) < 20:
                response = f"URL confirms: {url_evidence.get('goal_match_reason')}"

        # Extract mission context for better error messages
        mission_ctx = str(args.get("_mission_ctx", "") or "")
        expected_controls = []
        if "Expected Controls:" in mission_ctx:
            controls_section = mission_ctx.split("Expected Controls:")[1].split("\n")[0].strip()
            expected_controls = [c.strip() for c in controls_section.split(",") if c.strip()]

        # ── Intent-based completion gate: require UI interaction for action intents ──
        if (
            status == "success"
            and _requires_interaction_before_completion(main_goal, schema_action)
            and session_clicks == 0
            and not url_evidence.get("matches_goal")
        ):
            logger.warning(f"[COMPLETION_GATE] interaction required, no clicks yet | action='{schema_action}'")

            # ── DETERMINISTIC GROUNDING: find the best clickable target ──
            # Instead of just rejecting, find the actual button to click
            # Priority: 1) expected_controls matched to DOM, 2) vision-identified target
            best_target_id = None
            best_target_label = ""
            goal_lower = main_goal.lower()

            # Try to find the best control from expected_controls matched against live DOM
            if expected_controls:
                for control in expected_controls:
                    control_lower = control.lower()
                    # Match goal intent to control
                    goal_matches_control = (
                        ("create" in goal_lower and "create" in control_lower) or
                        ("new" in goal_lower and ("new" in control_lower or "create" in control_lower)) or
                        ("add" in goal_lower and ("add" in control_lower or "create" in control_lower)) or
                        ("delete" in goal_lower and "delete" in control_lower) or
                        ("remove" in goal_lower and "remove" in control_lower)
                    )
                    if goal_matches_control or (not best_target_label and expected_controls):
                        # Ground the control label to a live DOM element
                        for n in nodes:
                            if not getattr(n, "interactive", False):
                                continue
                            node_text = (getattr(n, "text", "") or "").lower().strip()
                            node_tag = getattr(n, "tag", "").lower()
                            node_role = getattr(n, "role", "").lower()
                            is_button = node_tag in ("button", "a") or node_role in ("button", "link")
                            if is_button and control_lower in node_text:
                                best_target_id = str(getattr(n, "id", ""))
                                best_target_label = control
                                break
                    if best_target_id:
                        break

            # Fallback: scan DOM for any button matching goal keywords
            if not best_target_id:
                goal_action_keywords = []
                for kw in ["create", "add", "new", "delete", "remove", "submit", "save", "confirm"]:
                    if kw in goal_lower:
                        goal_action_keywords.append(kw)
                if goal_action_keywords:
                    for n in nodes:
                        if not getattr(n, "interactive", False):
                            continue
                        node_text = (getattr(n, "text", "") or "").lower().strip()
                        node_tag = getattr(n, "tag", "").lower()
                        node_role = getattr(n, "role", "").lower()
                        is_button = node_tag in ("button", "a") or node_role in ("button", "link")
                        if is_button and any(kw in node_text for kw in goal_action_keywords):
                            best_target_id = str(getattr(n, "id", ""))
                            best_target_label = node_text[:50]
                            break

            # If we found a concrete target, return a DETERMINISTIC CLICK action
            if best_target_id:
                logger.info(f"[COMPLETION_GATE] deterministic click -> {best_target_id} label='{best_target_label}'")
                return True, "", {
                    "type": "click",
                    "target_id": best_target_id,
                    "speech": f"Let me click '{best_target_label}' to proceed.",
                    "force_click": False,
                }

            # No target found — return rejection with specific guidance
            guidance = ""
            if expected_controls:
                guidance = f" Look for a '{expected_controls[0]}' button and click it."

            return (
                False,
                f"REJECTED: Action-oriented goal requires a UI click before completion."
                f"{guidance} You MUST use click_element with a valid target_id from the DOM list. "
                f"Do NOT call complete_mission again until you have clicked.",
                None,
            )

        # ── Goal-achievement gate: don't complete on weak presence-only claims ──
        # For "show/find X in Y" goals, require evidence that BOTH entity and qualifier are achieved.
        if status == "success":
            entity_terms, qualifier_terms = _goal_entity_and_qualifier_terms(main_goal)
            evidence_blob = f"{response} || {evidence_refs}".lower()
            dom_joint_evidence = _dom_has_entity_qualifier_evidence(nodes, entity_terms, qualifier_terms)

            tracker = get_click_tracker(session_id) if session_id else None
            recent_labels = tracker.get_recent_labels(8) if tracker else []
            entity_click_seen = any(
                any(et in lbl for et in entity_terms)
                for lbl in recent_labels
            ) if entity_terms else True

            has_entity_in_answer = _text_has_terms(evidence_blob, entity_terms, min_hits=1)
            has_qualifier_in_answer = _text_has_terms(evidence_blob, qualifier_terms, min_hits=1) if qualifier_terms else True

            # LogicCritic: Entity Anchor Verification
            anchor_satisfied = _verify_entity_anchor(evidence_blob, entity_terms)

            if (
                _requires_interaction_before_completion(main_goal, schema_action)
                and (
                    _weak_presence_claim(response)
                    or (entity_terms and not entity_click_seen and not dom_joint_evidence and not has_entity_in_answer)
                    or (not anchor_satisfied)
                )
            ):
                logger.warning(
                    f"[COMPLETION_GATE] goal not achieved: entity_click={entity_click_seen} "
                    f"dom_joint={dom_joint_evidence} anchor={anchor_satisfied}"
                )
                return (
                    False,
                    "REJECTED: Goal not fully achieved yet. Do not complete from name/category presence only. "
                    "You must navigate to the target entity result and verify the requested qualifier "
                    "(for example color/outfit/category) in the visible result before complete_mission.",
                    None,
                )

        # ── Low-Confidence Interception: HELP CONSTRUCT BETTER ANSWER instead of just rejecting ──
        if status == "success" and answer_confidence == "low":
            # Instead of rejecting, help extract better evidence from the page
            logger.warning(f"[COMPLETION_GATE] low-confidence answer, attempting evidence rescue")
            
            # Extract comprehensive answer from readable nodes
            readable = [
                n for n in nodes
                if getattr(n, "text", None)
                and len((getattr(n, "text", "") or "").strip()) >= 15
                and getattr(n, "zone", "") in {"main", "content"}
            ][:30]
            
            if readable:
                # Build comprehensive answer from multiple sources
                goal_terms = set(_tokenize(main_goal))
                best_excerpts = []
                
                for n in readable:
                    txt = (getattr(n, "text", "") or "").strip()
                    txt_terms = set(_tokenize(txt))
                    overlap = len(goal_terms & txt_terms)
                    if overlap >= 2 or len(txt) >= 50:
                        best_excerpts.append(txt[:300])
                
                if best_excerpts:
                    comprehensive_answer = "\n\n".join(best_excerpts[:5])
                    logger.info(f"[COMPLETION_GATE] evidence rescue: {len(response)} -> {len(comprehensive_answer)} chars")
                    return True, "", {
                        "type": "answer",
                        "speech": comprehensive_answer,
                        "text": comprehensive_answer,
                        "status": "success",
                        "evidence_refs": comprehensive_answer[:500],
                        "answer_confidence": "medium",
                    }
            
            # Fallback: still reject but with helpful guidance
            return (
                False,
                "REJECTED: Your answer_confidence is 'low', which means you are NOT confident "
                "this is correct. Instead of guessing:\n"
                "1. Use read_page_content to re-examine the page for better evidence.\n"
                "2. Look for tabs, toggles, or filters that might show the correct data.\n"
                "3. Use request_vision if you cannot find the right content in text DOM.\n"
                "4. Only call complete_mission again when you have 'medium' or 'high' confidence.",
                None,
            )

        # ── Metric Mismatch Interception ──
        # Detect when answer contains dollar/cost values but goal asks for tokens/counts (or vice versa)
        if status == "success" and session_id:
            # Simple heuristic: if response has $ or "cost"/"spend"/"price" but no token/usage keywords
            response_lower = response.lower()
            has_money = bool(re.search(r'\$[\d,.]+|cost|spend|price|billing|usd|eur', response_lower))
            has_tokens = bool(re.search(r'token|usage|request|call|count|minute|hour|quota', response_lower))
            # We can't check the goal here directly, but evidence_refs may hint at mismatch
            # The main enforcement is in the system prompt; this is a safety net
            if has_money and not has_tokens and answer_confidence != "high":
                logger.warning(f"[COMPLETION_GATE] metric mismatch: money refs without token refs, confidence={answer_confidence}")
                return (
                    False,
                    "WARNING: Your answer contains dollar amounts/cost data but no token/usage metrics. "
                    "If the user's goal asks for tokens, usage, or counts — this is the WRONG metric. "
                    "Look for tabs or toggles that switch between 'Cost' and 'Usage/Tokens' views. "
                    "If the goal IS about cost/pricing, re-call complete_mission with confidence='high'.",
                    None,
                )

        # Strict validation ensuring the target entity exists in the evidence
        if (
            status == "success" 
            and target_entity 
            and _requires_interaction_before_completion(main_goal, schema_action)
        ):
            target_lower = target_entity.lower().strip()
            # Only skip for very abstract questions (e.g. "what is x" where x is a definition)
            # But if it's "what is the usage for X", we STILL want X as an anchor.
            
            entity_lower = target_entity.lower()
            evidence_lower = (f"{response} {evidence_refs}").lower()
            # Split into tokens if it's multiple words, checking for any match to allow fuzzy but strict
            entity_tokens = [t for t in re.findall(r"[a-z0-9]+", entity_lower) if len(t) > 2 and t not in _GOAL_STOPWORDS]

            # Require at least 50% of significant entity keywords to be present
            if entity_tokens:
                found_count = sum(1 for et in entity_tokens if et in evidence_lower)
                if found_count == 0 or (len(entity_tokens) > 1 and found_count < len(entity_tokens) / 2):
                    logger.warning(f"[COMPLETION_GATE] entity anchor blocked: '{target_entity}' not found in evidence")
                    return (
                        False,
                        f"REJECTED: Target entity '{target_entity}' not adequately found in your response or evidence. "
                        f"Your answer must explicitly mention the specific entity requested ('{target_entity}'). "
                        f"If you are on a different page or looking at generic data, navigate to the correct entity first.",
                        None,
                    )

        # Completion gate: warn if answer is suspiciously short/empty for success
        if status == "success" and len(response.strip()) < 15 and not evidence_refs:
            logger.warning(f"[COMPLETION_GATE] weak completion: response_len={len(response.strip())} confidence={answer_confidence}")
            # Try to extract better evidence from readable nodes
            readable = [
                n for n in nodes
                if getattr(n, "text", None)
                and len((getattr(n, "text", "") or "").strip()) >= 15
                and getattr(n, "zone", "") in {"main", "content"}
            ][:20]
            if readable:
                best_text = max(
                    [(getattr(n, "text", "") or "").strip() for n in readable],
                    key=len,
                    default="",
                )
                if best_text and len(best_text) > len(response):
                    response = best_text[:500]
                    logger.debug(f"[COMPLETION_GATE] evidence rescue len={len(response)}")

        return True, "", {
            "type": "answer",
            "speech": response,
            "text": response,
            "status": status,
            "evidence_refs": evidence_refs,
            "answer_confidence": answer_confidence,
        }

    elif tool_name == "escalate":
        # Handle escalate tool called by LLM
        blockage_type = args.get("blockage_type", "unknown")
        speech = args.get("speech", "I need help with this task.")
        ask = args.get("ask", "How should I proceed?")
        diagnostics = args.get("diagnostics", {})
        resume_context = args.get("resume_context", {})
        suggested_resolutions = args.get("suggested_resolutions", [])

        result = escalate_to_human(
            blockage_type=blockage_type,
            speech=speech,
            ask=ask,
            diagnostics=diagnostics,
            resume_context=resume_context,
            suggested_resolutions=suggested_resolutions,
        )
        return True, "", result

    else:
        return False, f"Error: Unknown tool '{tool_name}'.", None

# /Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/mission/page_state.py
"""
Semantic Stability Engine for Phase 1

Deterministic semantic fingerprints of page states to detect meaningful changes
vs. volatile DOM mutations (animations, timers, ads). Prevents false stagnancy
and premature termination during loading states.
"""

import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple


# =============================================================================
# Feature Flags
# =============================================================================

LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED = os.getenv(
    "LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}

LAST_MILE_LOADING_STATE_GUARD_ENABLED = os.getenv(
    "LAST_MILE_LOADING_STATE_GUARD_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class PageStateSnapshot:
    """Deterministic semantic fingerprints of a page state.
    
    Captures the essential semantic structure of a page without volatile
    attributes that change due to animations, timers, or ads.
    """
    heading_fingerprint: str  # hash of h1/h2/h3 text
    interactive_fingerprint: str  # hash of visible interactive controls
    content_fingerprint: str  # hash of visible content text
    loading_state: str  # "loading", "stable", "skeleton", "unknown"
    timestamp: float
    url: str
    dom_signature: str  # original for backward compat


@dataclass
class PageStateDelta:
    """Comparison result between two page states.
    
    Provides detailed information about what changed between two snapshots,
    enabling intelligent decisions about whether progress is being made.
    """
    loading_changed: bool
    headings_changed: bool
    interactives_changed: bool
    content_changed: bool
    classification: str  # "loading", "stable_no_change", "structure_changed", "interactives_changed", "content_changed", "mixed_changed"
    new_headings: List[str]
    added_interactive_labels: List[str]
    content_token_diff_summary: str
    raw_dom_changed: bool  # original DOM hash comparison


# =============================================================================
# Core Functions
# =============================================================================

def _normalize_text(text: Optional[str]) -> str:
    """Normalize text for fingerprinting: lowercase, strip whitespace, collapse spaces."""
    if not text:
        return ""
    return " ".join(text.lower().split())


def _compute_hash(data: str) -> str:
    """Compute MD5 hash of normalized data."""
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def _extract_heading_nodes(nodes: List[Any]) -> List[Dict[str, Any]]:
    """Extract heading-like nodes (h1/h2/h3 or role=heading)."""
    headings = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        
        tag = node.get("tag", "").lower()
        role = node.get("role", "").lower()
        
        is_heading = (
            tag in ("h1", "h2", "h3") or
            role == "heading"
        )
        
        if is_heading:
            text = _normalize_text(node.get("text", ""))
            if text:  # Only include non-empty headings
                headings.append({
                    "text": text,
                    "level": tag if tag in ("h1", "h2", "h3") else "h2",
                    "id": node.get("id", "")[:20]  # Truncate ID for stability
                })
    
    return headings


def _build_heading_fingerprint(nodes: List[Any]) -> str:
    """Build fingerprint from heading text (h1/h2/h3 or role=heading).
    
    Process:
    1. Collect nodes with tag h1/h2/h3 or role="heading"
    2. Extract text, normalize whitespace, lowercase
    3. Sort alphabetically, join with "|", hash with md5
    """
    headings = _extract_heading_nodes(nodes)
    
    # Extract and normalize heading texts
    heading_texts = [_normalize_text(h["text"]) for h in headings]
    
    # Remove empty and dedupe while preserving order for consistent hashing
    seen: Set[str] = set()
    unique_headings = []
    for text in heading_texts:
        if text and text not in seen:
            seen.add(text)
            unique_headings.append(text)
    
    # Sort alphabetically for deterministic ordering
    unique_headings.sort()
    
    # Join with delimiter and hash
    fingerprint_data = "|".join(unique_headings)
    return _compute_hash(fingerprint_data)


def _extract_interactive_nodes(nodes: List[Any]) -> List[Dict[str, Any]]:
    """Extract visible interactive nodes."""
    interactives = []
    
    for node in nodes:
        if not isinstance(node, dict):
            continue
        
        # Check if node is interactive
        is_interactive = node.get("interactive", False)
        role = node.get("role", "").lower()
        tag = node.get("tag", "").lower()
        
        # Additional interactive indicators
        interactive_roles = {
            "button", "link", "textbox", "checkbox", "radio", 
            "combobox", "menuitem", "tab", "searchbox", "spinbutton",
            "switch", "option", "menuitemcheckbox", "menuitemradio"
        }
        interactive_tags = {
            "button", "a", "input", "select", "textarea", "details",
            "summary", "label"
        }
        
        is_interactive = (
            is_interactive or
            role in interactive_roles or
            tag in interactive_tags or
            node.get("clickable", False)
        )
        
        if is_interactive:
            # Check visibility (exclude hidden elements)
            is_hidden = (
                node.get("hidden", False) or
                node.get("aria_hidden", False) or
                node.get("visible", True) is False
            )
            
            if not is_hidden:
                interactives.append({
                    "role": role or tag,
                    "tag": tag,
                    "text": _normalize_text(node.get("text", ""))[:30],  # Truncate for stability
                    "zone": node.get("zone", ""),
                    "id": node.get("id", "")[:8],  # Truncate ID for stability
                    "aria_label": _normalize_text(node.get("aria_label", ""))[:30],
                    "title": _normalize_text(node.get("title", ""))[:30]
                })
    
    return interactives


def _build_interactive_fingerprint(nodes: List[Any]) -> str:
    """Build fingerprint from visible interactive controls.
    
    Process:
    1. Filter nodes where interactive=True
    2. For each: extract (role, tag, text[:30], zone, id[:8])
    3. Exclude volatile classes/attributes
    4. Sort by id, join with "|", hash with md5
    """
    interactives = _extract_interactive_nodes(nodes)
    
    # Build stable representation for each interactive element
    interactive_signatures = []
    for elem in interactives:
        # Create a stable signature excluding volatile attributes
        signature_parts = [
            elem["role"],
            elem["tag"],
            elem["text"],
            elem["zone"],
            elem["id"]
        ]
        
        # Add aria-label or title if present (stable identifiers)
        if elem["aria_label"]:
            signature_parts.append(f"aria:{elem['aria_label']}")
        elif elem["title"]:
            signature_parts.append(f"title:{elem['title']}")
        
        signature = ":".join(signature_parts)
        interactive_signatures.append((elem["id"], signature))
    
    # Sort by ID for deterministic ordering
    interactive_signatures.sort(key=lambda x: x[0])
    
    # Extract just the signatures (without IDs used for sorting)
    sorted_signatures = [sig for _, sig in interactive_signatures]
    
    # Join and hash
    fingerprint_data = "|".join(sorted_signatures)
    return _compute_hash(fingerprint_data)


def _extract_content_nodes(nodes: List[Any]) -> List[Dict[str, Any]]:
    """Extract content nodes from main/content zones."""
    content_nodes = []
    content_zones = {"main", "content", "article", "section"}
    
    for node in nodes:
        if not isinstance(node, dict):
            continue
        
        zone = node.get("zone", "").lower()
        tag = node.get("tag", "").lower()
        role = node.get("role", "").lower()
        
        # Check if node is in a content zone
        is_content_zone = (
            zone in content_zones or
            tag in ("main", "article", "section") or
            role in ("main", "article", "region")
        )
        
        if is_content_zone:
            text = _normalize_text(node.get("text", ""))
            if text and len(text) > 3:  # Filter out very short text
                content_nodes.append({
                    "text": text,
                    "zone": zone,
                    "tag": tag
                })
    
    return content_nodes


def _build_content_fingerprint(nodes: List[Any]) -> str:
    """Build fingerprint from visible content text.
    
    Process:
    1. Filter nodes in zone="main" or zone="content"
    2. Extract text, tokenize (split on whitespace)
    3. Lowercase, dedupe, sort alphabetically
    4. Join with " ", hash with md5
    """
    content_nodes = _extract_content_nodes(nodes)
    
    # Extract all tokens from content
    all_tokens: Set[str] = set()
    for node in content_nodes:
        text = node["text"]
        # Tokenize by splitting on whitespace and punctuation
        tokens = text.split()
        # Filter out very short tokens (likely noise)
        tokens = [t for t in tokens if len(t) > 1]
        all_tokens.update(tokens)
    
    # Sort alphabetically for deterministic ordering
    sorted_tokens = sorted(all_tokens)
    
    # Join and hash
    fingerprint_data = " ".join(sorted_tokens)
    return _compute_hash(fingerprint_data)


def classify_loading_state(nodes: List[Any]) -> str:
    """Classify page as loading/stable/skeleton/unknown.
    
    Detection heuristics:
    - Check for text containing: "loading", "please wait", "spinner", "skeleton"
    - Check for roles: "progressbar", "status", "alert"
    - Check for aria-busy="true"
    - Check for skeleton/shimmer CSS classes
    
    Returns:
        "loading" - Page is actively loading
        "skeleton" - Skeleton/shimmer placeholders visible
        "stable" - Page appears stable
        "unknown" - Cannot determine loading state
    """
    loading_indicators = 0
    skeleton_indicators = 0
    has_content = False
    
    loading_keywords = {
        "loading", "please wait", "spinner", "progress", 
        "fetching", "downloading", "processing", "updating"
    }
    
    skeleton_keywords = {
        "skeleton", "shimmer", "placeholder", "suspense"
    }
    
    for node in nodes:
        if not isinstance(node, dict):
            continue
        
        text = _normalize_text(node.get("text", ""))
        role = node.get("role", "").lower()
        aria_busy = str(node.get("aria_busy", "")).lower()
        css_class = node.get("class", "").lower()
        
        # Check for loading keywords in text
        if any(keyword in text for keyword in loading_keywords):
            loading_indicators += 1
        
        # Check for skeleton keywords
        if any(keyword in text for keyword in skeleton_keywords):
            skeleton_indicators += 1
        if any(keyword in css_class for keyword in skeleton_keywords):
            skeleton_indicators += 1
        
        # Check for progress indicators
        if role in ("progressbar", "status"):
            loading_indicators += 1
        
        # Check for aria-busy
        if aria_busy == "true":
            loading_indicators += 1
        
        # Check for substantial content (indicates not loading)
        if len(text) > 50 and not any(k in text for k in loading_keywords):
            has_content = True
    
    # Determine loading state
    if skeleton_indicators > 0 and not has_content:
        return "skeleton"
    elif loading_indicators > 0:
        return "loading"
    elif has_content:
        return "stable"
    else:
        return "unknown"


def build_page_state_snapshot(
    nodes: List[Any],
    url: str,
    dom_signature: str
) -> PageStateSnapshot:
    """Build semantic fingerprints from DOM nodes.
    
    Creates a deterministic snapshot of page state that is resilient to
    volatile DOM mutations like animations, timers, and ads.
    
    Args:
        nodes: List of DOM node dictionaries
        url: Current page URL
        dom_signature: Original DOM signature for backward compatibility
    
    Returns:
        PageStateSnapshot with semantic fingerprints
    """
    # Build individual fingerprints
    heading_fingerprint = _build_heading_fingerprint(nodes)
    interactive_fingerprint = _build_interactive_fingerprint(nodes)
    content_fingerprint = _build_content_fingerprint(nodes)
    loading_state = classify_loading_state(nodes)
    
    return PageStateSnapshot(
        heading_fingerprint=heading_fingerprint,
        interactive_fingerprint=interactive_fingerprint,
        content_fingerprint=content_fingerprint,
        loading_state=loading_state,
        timestamp=time.time(),
        url=url,
        dom_signature=dom_signature
    )


def _extract_new_headings(
    prev_nodes: List[Dict[str, Any]],
    curr_nodes: List[Dict[str, Any]]
) -> List[str]:
    """Extract headings that are new in current state."""
    prev_texts = {_normalize_text(n["text"]) for n in prev_nodes}
    curr_texts = {_normalize_text(n["text"]) for n in curr_nodes}
    
    new_texts = curr_texts - prev_texts
    return sorted(list(new_texts))


def _extract_added_interactives(
    prev_nodes: List[Dict[str, Any]],
    curr_nodes: List[Dict[str, Any]]
) -> List[str]:
    """Extract interactive labels that are new in current state."""
    def get_label(node: Dict[str, Any]) -> str:
        text = node.get("text", "")
        aria_label = node.get("aria_label", "")
        title = node.get("title", "")
        return text or aria_label or title or node.get("id", "")[:8]
    
    prev_labels = {_normalize_text(get_label(n)) for n in prev_nodes}
    curr_labels = {_normalize_text(get_label(n)) for n in curr_nodes}
    
    new_labels = curr_labels - prev_labels
    return sorted(list(new_labels))


def _compute_content_diff_summary(
    prev_nodes: List[Dict[str, Any]],
    curr_nodes: List[Dict[str, Any]]
) -> str:
    """Compute a summary of content token differences."""
    def extract_tokens(nodes: List[Dict[str, Any]]) -> Set[str]:
        tokens: Set[str] = set()
        for node in nodes:
            text = node.get("text", "")
            tokens.update(text.split())
        return tokens
    
    prev_tokens = extract_tokens(prev_nodes)
    curr_tokens = extract_tokens(curr_nodes)
    
    added = len(curr_tokens - prev_tokens)
    removed = len(prev_tokens - curr_tokens)
    
    if added == 0 and removed == 0:
        return "no_change"
    elif added > 0 and removed == 0:
        return f"+{added} tokens"
    elif added == 0 and removed > 0:
        return f"-{removed} tokens"
    else:
        return f"+{added}/-{removed} tokens"


def compare_page_state(
    previous: PageStateSnapshot,
    current: PageStateSnapshot
) -> PageStateDelta:
    """Compare two page states and return delta.
    
    Analyzes the differences between two snapshots to determine what changed
    and classify the type of change.
    
    Args:
        previous: The previous page state snapshot
        current: The current page state snapshot
    
    Returns:
        PageStateDelta describing the differences
    """
    # Compare fingerprints
    loading_changed = previous.loading_state != current.loading_state
    headings_changed = previous.heading_fingerprint != current.heading_fingerprint
    interactives_changed = previous.interactive_fingerprint != current.interactive_fingerprint
    content_changed = previous.content_fingerprint != current.content_fingerprint
    raw_dom_changed = previous.dom_signature != current.dom_signature
    
    # Determine classification
    if loading_changed:
        classification = "loading"
    elif not any([headings_changed, interactives_changed, content_changed]):
        classification = "stable_no_change"
    elif headings_changed and not any([interactives_changed, content_changed]):
        classification = "structure_changed"
    elif interactives_changed and not any([headings_changed, content_changed]):
        classification = "interactives_changed"
    elif content_changed and not any([headings_changed, interactives_changed]):
        classification = "content_changed"
    else:
        classification = "mixed_changed"
    
    # Note: To extract detailed diffs, we'd need the original nodes.
    # For now, return empty lists - these can be populated by the caller
    # if they have access to the original node lists.
    new_headings: List[str] = []
    added_interactive_labels: List[str] = []
    content_token_diff_summary = "unknown"
    
    if content_changed:
        content_token_diff_summary = "changed"
    else:
        content_token_diff_summary = "no_change"
    
    return PageStateDelta(
        loading_changed=loading_changed,
        headings_changed=headings_changed,
        interactives_changed=interactives_changed,
        content_changed=content_changed,
        classification=classification,
        new_headings=new_headings,
        added_interactive_labels=added_interactive_labels,
        content_token_diff_summary=content_token_diff_summary,
        raw_dom_changed=raw_dom_changed
    )


def should_count_as_stagnant(delta: PageStateDelta) -> bool:
    """Determine if this delta should increment stagnancy counter.
    
    Stagnancy should only be counted when:
    - The page is not in a loading state
    - No semantic fingerprints have changed (true stagnancy)
    
    Args:
        delta: The page state delta to evaluate
    
    Returns:
        True if this represents true stagnancy, False otherwise
    """
    # Don't count loading state changes as stagnancy
    if delta.loading_changed:
        return False
    
    # Don't count if any semantic fingerprint changed (real progress)
    if delta.headings_changed or delta.interactives_changed or delta.content_changed:
        return False
    
    # True stagnancy: no semantic changes detected
    return True


def format_page_state_for_prompt(delta: PageStateDelta) -> str:
    """Format delta as compact system note for LLM prompt.
    
    Creates a concise summary of page state changes suitable for
    inclusion in an LLM system prompt.
    
    Args:
        delta: The page state delta to format
    
    Returns:
        Compact system note string
    """
    parts = ["SYSTEM:"]
    
    # Add classification
    parts.append(delta.classification.replace("_", " "))
    
    # Add detail about what changed
    change_details = []
    if delta.headings_changed:
        change_details.append("headings_changed")
    else:
        change_details.append("headings_unchanged")
    
    if delta.interactives_changed:
        change_details.append("interactives_changed")
    else:
        change_details.append("interactives_unchanged")
    
    if delta.content_changed:
        change_details.append("content_changed")
    else:
        change_details.append("content_unchanged")
    
    parts.append(", ".join(change_details) + ".")
    
    # Add new elements if present
    if delta.new_headings:
        parts.append(f"New headings: {', '.join(delta.new_headings[:3])}.")
    
    if delta.added_interactive_labels:
        labels = delta.added_interactive_labels[:3]
        parts.append(f"New controls: {', '.join(labels)}.")
    
    # Add stagnancy note
    if should_count_as_stagnant(delta):
        parts.append("Page appears stagnant.")
    elif delta.loading_changed:
        parts.append("Page is still loading.")
    else:
        parts.append("Page state has changed.")
    
    return " ".join(parts)


# =============================================================================
# Testing
# =============================================================================

def test_page_state():
    """Test suite for page state detection.
    
    Tests:
    1. Identical DOM with changed volatile attrs -> stable
    2. New heading only -> structure_changed
    3. Dropdown open -> interactives_changed
    4. New table data -> content_changed
    5. Skeleton state -> loading
    """
    print("=" * 60)
    print("Page State Semantic Stability Engine Tests")
    print("=" * 60)
    
    # Test 1: Identical DOM with changed volatile attrs -> stable
    print("\n[Test 1] Identical DOM with changed volatile attributes")
    nodes_base = [
        {"tag": "h1", "text": "Dashboard", "id": "heading-1", "zone": "main"},
        {"tag": "button", "text": "Submit", "id": "btn-1", "interactive": True, "zone": "main"},
        {"tag": "p", "text": "Welcome to the dashboard", "id": "p-1", "zone": "content"},
    ]
    
    # Same semantic content, different volatile attrs
    nodes_volatile = [
        {"tag": "h1", "text": "Dashboard", "id": "heading-1", "zone": "main"},
        {"tag": "button", "text": "Submit", "id": "btn-1", "interactive": True, "zone": "main", "class": "animate-pulse"},
        {"tag": "p", "text": "Welcome to the dashboard", "id": "p-1", "zone": "content", "style": "color: red"},
    ]
    
    snap1 = build_page_state_snapshot(nodes_base, "http://test.com", "sig1")
    snap2 = build_page_state_snapshot(nodes_volatile, "http://test.com", "sig2")
    delta = compare_page_state(snap1, snap2)
    
    print(f"  DOM signatures different: {snap1.dom_signature != snap2.dom_signature}")
    print(f"  Headings changed: {delta.headings_changed}")
    print(f"  Interactives changed: {delta.interactives_changed}")
    print(f"  Content changed: {delta.content_changed}")
    print(f"  Classification: {delta.classification}")
    print(f"  Should count as stagnant: {should_count_as_stagnant(delta)}")
    
    assert delta.raw_dom_changed == True, "DOM signatures should differ"
    assert delta.headings_changed == False, "Headings should be identical"
    assert delta.interactives_changed == False, "Interactives should be identical"
    assert delta.content_changed == False, "Content should be identical"
    assert delta.classification == "stable_no_change", "Should be stable"
    assert should_count_as_stagnant(delta) == True, "Should count as stagnant"
    print("  ✓ PASSED")
    
    # Test 2: New heading -> mixed_changed (headings + content both change)
    print("\n[Test 2] New heading added")
    nodes_with_heading = [
        {"tag": "h1", "text": "Dashboard", "id": "heading-1", "zone": "main"},
        {"tag": "h2", "text": "Overview", "id": "heading-2", "zone": "main"},
        {"tag": "button", "text": "Submit", "id": "btn-1", "interactive": True, "zone": "main"},
    ]
    
    snap3 = build_page_state_snapshot(nodes_base, "http://test.com", "sig1")
    snap4 = build_page_state_snapshot(nodes_with_heading, "http://test.com", "sig3")
    delta2 = compare_page_state(snap3, snap4)
    
    print(f"  Headings changed: {delta2.headings_changed}")
    print(f"  Interactives changed: {delta2.interactives_changed}")
    print(f"  Content changed: {delta2.content_changed}")
    print(f"  Classification: {delta2.classification}")
    
    assert delta2.headings_changed == True, "Headings should have changed"
    assert delta2.interactives_changed == False, "Interactives should be unchanged"
    # Note: Content also changes because heading text is included in content tokens
    assert delta2.content_changed == True, "Content should have changed (new heading text)"
    assert delta2.classification == "mixed_changed", "Should be mixed_changed"
    assert should_count_as_stagnant(delta2) == False, "Should NOT count as stagnant"
    print("  ✓ PASSED")
    
    # Test 3: Dropdown open -> mixed_changed (interactives + content both change)
    print("\n[Test 3] Dropdown menu opened")
    nodes_dropdown_open = [
        {"tag": "h1", "text": "Dashboard", "id": "heading-1", "zone": "main"},
        {"tag": "button", "text": "Submit", "id": "btn-1", "interactive": True, "zone": "main"},
        {"tag": "button", "text": "Option 1", "id": "opt-1", "interactive": True, "zone": "main"},
        {"tag": "button", "text": "Option 2", "id": "opt-2", "interactive": True, "zone": "main"},
    ]
    
    snap5 = build_page_state_snapshot(nodes_base, "http://test.com", "sig1")
    snap6 = build_page_state_snapshot(nodes_dropdown_open, "http://test.com", "sig4")
    delta3 = compare_page_state(snap5, snap6)
    
    print(f"  Headings changed: {delta3.headings_changed}")
    print(f"  Interactives changed: {delta3.interactives_changed}")
    print(f"  Content changed: {delta3.content_changed}")
    print(f"  Classification: {delta3.classification}")
    
    assert delta3.headings_changed == False, "Headings should be unchanged"
    assert delta3.interactives_changed == True, "Interactives should have changed"
    # Note: Content also changes because button text is included in content tokens
    assert delta3.content_changed == True, "Content should have changed (new button text)"
    assert delta3.classification == "mixed_changed", "Should be mixed_changed"
    assert should_count_as_stagnant(delta3) == False, "Should NOT count as stagnant"
    print("  ✓ PASSED")
    
    # Test 4: New table data -> content_changed
    print("\n[Test 4] New table data loaded")
    nodes_with_data = [
        {"tag": "h1", "text": "Dashboard", "id": "heading-1", "zone": "main"},
        {"tag": "button", "text": "Submit", "id": "btn-1", "interactive": True, "zone": "main"},
        {"tag": "p", "text": "Welcome to the dashboard with new data", "id": "p-1", "zone": "content"},
        {"tag": "td", "text": "Row 1 Data", "id": "td-1", "zone": "content"},
        {"tag": "td", "text": "Row 2 Data", "id": "td-2", "zone": "content"},
    ]
    
    snap7 = build_page_state_snapshot(nodes_base, "http://test.com", "sig1")
    snap8 = build_page_state_snapshot(nodes_with_data, "http://test.com", "sig5")
    delta4 = compare_page_state(snap7, snap8)
    
    print(f"  Headings changed: {delta4.headings_changed}")
    print(f"  Interactives changed: {delta4.interactives_changed}")
    print(f"  Content changed: {delta4.content_changed}")
    print(f"  Classification: {delta4.classification}")
    
    assert delta4.headings_changed == False, "Headings should be unchanged"
    assert delta4.interactives_changed == False, "Interactives should be unchanged"
    assert delta4.content_changed == True, "Content should have changed"
    assert delta4.classification == "content_changed", "Should be content_changed"
    assert should_count_as_stagnant(delta4) == False, "Should NOT count as stagnant"
    print("  ✓ PASSED")
    
    # Test 5: Skeleton state -> loading
    print("\n[Test 5] Skeleton loading state")
    nodes_skeleton = [
        {"tag": "div", "text": "Loading...", "id": "loading-1", "zone": "main"},
        {"tag": "div", "class": "skeleton-card", "id": "skel-1", "zone": "main"},
        {"tag": "div", "class": "shimmer", "id": "shim-1", "zone": "main"},
    ]
    
    loading_state = classify_loading_state(nodes_skeleton)
    print(f"  Loading state: {loading_state}")
    
    assert loading_state == "skeleton", "Should detect skeleton state"
    print("  ✓ PASSED")
    
    # Test 6: Loading state transition
    print("\n[Test 6] Loading state transition")
    nodes_loading = [
        {"tag": "div", "text": "Please wait, loading data", "id": "load-1", "zone": "main"},
        {"tag": "div", "role": "progressbar", "id": "prog-1", "zone": "main"},
    ]
    
    snap_loading = build_page_state_snapshot(nodes_loading, "http://test.com", "sig-loading")
    snap_stable = build_page_state_snapshot(nodes_base, "http://test.com", "sig-stable")
    delta5 = compare_page_state(snap_loading, snap_stable)
    
    print(f"  Previous loading state: {snap_loading.loading_state}")
    print(f"  Current loading state: {snap_stable.loading_state}")
    print(f"  Loading changed: {delta5.loading_changed}")
    print(f"  Classification: {delta5.classification}")
    print(f"  Should count as stagnant: {should_count_as_stagnant(delta5)}")
    
    assert delta5.loading_changed == True, "Loading state should have changed"
    assert delta5.classification == "loading", "Should be loading classification"
    assert should_count_as_stagnant(delta5) == False, "Should NOT count as stagnant during loading"
    print("  ✓ PASSED")
    
    # Test 7: Prompt formatting
    print("\n[Test 7] Prompt formatting")
    prompt = format_page_state_for_prompt(delta3)
    print(f"  Formatted prompt: {prompt}")
    
    assert "SYSTEM:" in prompt, "Prompt should contain SYSTEM prefix"
    assert "interactives_changed" in prompt, "Prompt should mention interactives_changed"
    print("  ✓ PASSED")
    
    print("\n" + "=" * 60)
    print("All tests passed! ✓")
    print("=" * 60)


if __name__ == "__main__":
    test_page_state()

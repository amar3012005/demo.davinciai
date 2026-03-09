"""
page_index.py  —  PageIndex (Vectorless) Traversal Engine

PURPOSE:
  Replaces Qdrant-based vector search in the pre-decision gate.
  Provides top-down logical traversal of a site_map.json tree
  to determine navigation paths based on goal + current URL.

ALGORITHM:
  1. LOCATE: Match current URL against path_regex → find "Current Node"
  2. TARGET: LLM scans summary fields to find "Target Node" for the goal
  3. PATH: Calculate transition path between Current → Target
  4. OUTPUT: recommended_strategy_order (e.g., ["Click Docs", "Click Prompt Caching", "LAST_MILE: ..."])

NO VECTOR SEARCH — pure reasoning over a JSON tree.
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("vc.stage.page_index")

# ═══════════════════════════════════════════════════════════════════════
# Index Loading
# ═══════════════════════════════════════════════════════════════════════

_cached_index: Optional[Dict[str, Any]] = None
_cached_index_path: Optional[str] = None


def load_index(path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load and cache the site_map.json index.
    Searches in order:
      1. Explicit path argument
      2. SITE_MAP_PATH env var
      3. ./site_map.json (relative to rag-visual-copilot)
      4. /app/site_map.json (Docker container path)
    """
    global _cached_index, _cached_index_path

    search_paths = [
        path,
        os.getenv("SITE_MAP_PATH"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "site_map.json"),
        "/app/site_map.json",
    ]

    for p in search_paths:
        if p and os.path.isfile(p):
            if _cached_index is not None and _cached_index_path == p:
                return _cached_index
            with open(p, "r", encoding="utf-8") as f:
                _cached_index = json.load(f)
                _cached_index_path = p
            logger.info(f"PageIndex loaded from {p} (domain={_cached_index.get('site_metadata', {}).get('domain', '?')})")
            return _cached_index

    logger.warning("PageIndex site_map.json not found in any search path")
    return {}


def is_index_available() -> bool:
    """Check if a site_map.json is available."""
    idx = load_index()
    return bool(idx and idx.get("root"))


# ═══════════════════════════════════════════════════════════════════════
# Node Lookup — URL → Node matching via path_regex
# ═══════════════════════════════════════════════════════════════════════

def _flatten_nodes(node: Dict[str, Any], parent_path: str = "") -> List[Dict[str, Any]]:
    """Flatten the tree into a list of (node, depth, ancestors) tuples."""
    results = []
    logical = node.get("logical_path", "")
    results.append(node)
    for child in node.get("children", []):
        results.extend(_flatten_nodes(child, logical))
    return results


def locate_current_node(
    current_url: str,
    index: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find the node in the PageIndex that matches the current URL.
    Uses path_regex fields for matching.
    Returns the most specific (deepest) matching node.
    """
    if not index:
        index = load_index()
    if not index or not index.get("root"):
        return None

    parsed = urlparse(current_url if "://" in current_url else f"https://{current_url}")
    path = parsed.path.rstrip("/") or "/"

    root = index["root"]
    all_nodes = _flatten_nodes(root)

    # Score matches by specificity (longer path_regex = more specific)
    best_match = None
    best_specificity = -1

    for node in all_nodes:
        regex = node.get("path_regex", "")
        if not regex:
            continue
        try:
            if re.match(regex, path):
                specificity = len(regex)
                if specificity > best_specificity:
                    best_specificity = specificity
                    best_match = node
        except re.error:
            continue

    return best_match


def find_node_by_id(node_id: str, index: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Find a node by its node_id."""
    if not index:
        index = load_index()
    if not index or not index.get("root"):
        return None

    all_nodes = _flatten_nodes(index["root"])
    for node in all_nodes:
        if node.get("node_id") == node_id:
            return node
    return None


def find_parent_node(
    child_node_id: str,
    index: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Find the parent of a node by traversing the tree."""
    if not index:
        index = load_index()
    if not index or not index.get("root"):
        return None

    def _search(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for child in node.get("children", []):
            if child.get("node_id") == child_node_id:
                return node
            found = _search(child)
            if found:
                return found
        return None

    return _search(index["root"])


# ═══════════════════════════════════════════════════════════════════════
# Target Node Discovery — Goal → Best matching leaf
# ═══════════════════════════════════════════════════════════════════════

_STOP_WORDS = frozenset({
    "the", "is", "a", "an", "to", "for", "of", "in", "on", "at", "by",
    "how", "what", "where", "when", "which", "who", "show", "me", "my",
    "find", "get", "use", "see", "read", "about", "check", "look",
    "can", "do", "does", "and", "with", "from",
})


# Module-level IDF cache
_idf_weights: Dict[str, float] = {}
_idf_built = False


def _build_idf(index: Dict[str, Any]) -> None:
    """Build inverse document frequency weights for all terms across all nodes."""
    global _idf_weights, _idf_built
    if _idf_built:
        return

    all_nodes = _flatten_nodes(index.get("root", {}))
    n_docs = len(all_nodes)
    if n_docs == 0:
        _idf_built = True
        return

    # Count how many nodes each term appears in
    doc_freq: Dict[str, int] = {}
    for node in all_nodes:
        searchable = " ".join([
            node.get("title", ""),
            node.get("summary_of_contents", ""),
            " ".join(node.get("terminal_capabilities", [])),
            " ".join(node.get("expected_controls", [])),
            node.get("url", ""),
        ]).lower()
        terms = set(re.findall(r"[a-z0-9]+", searchable))
        for t in terms:
            doc_freq[t] = doc_freq.get(t, 0) + 1

    import math
    for term, df in doc_freq.items():
        # IDF = log(N / df), clamped to [0.1, 5.0]
        _idf_weights[term] = max(0.1, min(5.0, math.log(n_docs / df)))

    _idf_built = True
    logger.info(f"PageIndex IDF built: {len(_idf_weights)} terms, {n_docs} nodes")


def _goal_relevance_score(goal: str, node: Dict[str, Any]) -> float:
    """
    Score how relevant a node is to a given goal.
    Uses IDF-weighted term matching: rare/specific terms like 'vision'
    score higher than ubiquitous terms like 'api' or 'docs'.
    """
    raw_terms = re.findall(r"[a-z0-9]+", goal.lower())
    goal_terms = set(t for t in raw_terms if t not in _STOP_WORDS and len(t) > 1)
    if not goal_terms:
        goal_terms = set(t for t in raw_terms if len(t) > 1)
    if not goal_terms:
        return 0.0

    # Combine all searchable text from the node
    title_text = node.get("title", "").lower()
    summary_text = node.get("summary_of_contents", "").lower()
    cap_text = " ".join(node.get("terminal_capabilities", [])).lower()
    url_text = node.get("url", "").lower()

    searchable = f"{title_text} {summary_text} {cap_text} {url_text}"
    node_terms = set(re.findall(r"[a-z0-9]+", searchable))

    overlap = goal_terms & node_terms
    if not overlap:
        return 0.0

    # Per-field term sets for weighting
    title_terms = set(re.findall(r"[a-z0-9]+", title_text))
    summary_terms = set(re.findall(r"[a-z0-9]+", summary_text))
    cap_terms_set = set(re.findall(r"[a-z0-9]+", cap_text))
    url_path = urlparse(url_text).path if url_text else ""
    path_segments = set(re.findall(r"[a-z0-9]+", url_path))

    score = 0.0
    for term in overlap:
        # IDF weight — rare terms (like 'vision') get high weight,
        # common terms (like 'api', 'docs') get low weight
        idf = _idf_weights.get(term, 1.0)

        # Field-specific bonus
        field_weight = 1.0
        if term in title_terms:
            field_weight += 3.0
        if term in summary_terms:
            field_weight += 1.5
        if term in cap_terms_set:
            field_weight += 2.0
        if term in path_segments:
            field_weight += 2.5

        score += field_weight * idf

    # Leaf node bonus (actionable pages)
    if not node.get("children"):
        score += 1.0
    if node.get("terminal_capabilities"):
        score += 0.5

    return score


def find_target_node(
    goal: str,
    index: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find the best target node for a given goal using IDF-weighted scoring.
    Returns the most relevant leaf node.
    """
    if not index:
        index = load_index()
    if not index or not index.get("root"):
        return None

    # Build IDF weights on first call
    _build_idf(index)

    all_nodes = _flatten_nodes(index["root"])
    scored = [(node, _goal_relevance_score(goal, node)) for node in all_nodes]
    scored.sort(key=lambda x: x[1], reverse=True)

    if scored and scored[0][1] > 0:
        return scored[0][0]
    return None


# ═══════════════════════════════════════════════════════════════════════
# Path Calculation — Current Node → Target Node transition
# ═══════════════════════════════════════════════════════════════════════

def _build_path_map(node: Dict[str, Any], path: List[str] = None) -> Dict[str, List[str]]:
    """Build a map of node_id → list of ancestor node_ids (path from root)."""
    if path is None:
        path = []
    current_path = path + [node.get("node_id", "")]
    result = {node.get("node_id", ""): current_path}
    for child in node.get("children", []):
        result.update(_build_path_map(child, current_path))
    return result


def calculate_transition_path(
    current_node: Dict[str, Any],
    target_node: Dict[str, Any],
    index: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Calculate the list of transition nodes from current to target.
    Returns a list of nodes representing the navigation path.
    """
    if not index:
        index = load_index()
    if not index or not index.get("root"):
        return []

    path_map = _build_path_map(index["root"])
    current_id = current_node.get("node_id", "")
    target_id = target_node.get("node_id", "")

    current_path = path_map.get(current_id, [])
    target_path = path_map.get(target_id, [])

    if not current_path or not target_path:
        return [target_node]

    # Find common ancestor
    common_depth = 0
    for i in range(min(len(current_path), len(target_path))):
        if current_path[i] == target_path[i]:
            common_depth = i + 1
        else:
            break

    # Transition = nodes in target_path after the common ancestor
    # (skip the ones we're already in)
    transition_ids = target_path[common_depth:]

    # Resolve to actual nodes
    all_nodes = {n.get("node_id"): n for n in _flatten_nodes(index["root"])}
    transition_nodes = [all_nodes[nid] for nid in transition_ids if nid in all_nodes]

    return transition_nodes


# ═══════════════════════════════════════════════════════════════════════
# Strategy Generation — Build recommended_strategy_order
# ═══════════════════════════════════════════════════════════════════════

def _nav_label_from_node(node: Dict[str, Any]) -> str:
    """
    Derive a short, DOM-friendly navigation label from a node.
    The nav sidebar typically shows short labels like 'Usage', 'Docs',
    'Dashboard' — not full titles like 'Usage and Spend' or 'Documentation'.
    """
    title = node.get("title", "Unknown")
    url = node.get("url", "")

    _GENERIC_SEGMENTS = frozenset({
        "home", "index", "overview", "main", "default", "root",
    })

    # Known title → nav label overrides (common abbreviations in nav bars)
    _TITLE_TO_NAV = {
        "documentation": "Docs",
    }

    # Check title override first
    title_lower = title.lower()
    if title_lower in _TITLE_TO_NAV:
        return _TITLE_TO_NAV[title_lower]

    # Try URL path segment — most reliable for nav matching
    if url:
        path = urlparse(url).path.rstrip("/")
        if path:
            segment = path.split("/")[-1]
            label = segment.replace("-", " ").replace("_", " ").strip()

            if label and len(label) >= 3 and label.lower() not in _GENERIC_SEGMENTS:
                # If the segment is a SUFFIX of the title, use the title instead
                # e.g., segment="keys" but title="API Keys" → use "API Keys"
                # But if segment is a PREFIX, use the segment
                # e.g., segment="dashboard" and title="Dashboard Overview" → use "Dashboard"
                if (label.lower() != title_lower
                        and label.lower() in title_lower
                        and not title_lower.startswith(label.lower())):
                    # segment is a suffix — title has an important prefix
                    pass
                else:
                    return label.title()

    # Fallback: shorten the title by taking just the first phrase
    # "Usage and Spend" → "Usage"
    for sep in [" and ", " & ", " - ", " | ", " — "]:
        if sep in title:
            return title.split(sep)[0].strip()

    return title


def generate_strategy_from_path(
    transition_nodes: List[Dict[str, Any]],
    goal: str,
) -> List[str]:
    """
    Convert a list of transition nodes into a recommended_strategy_order.
    Each intermediate node becomes "Click [NavLabel]", and the final node
    gets a LAST_MILE entry.

    Uses short nav-friendly labels (e.g., 'Usage' not 'Usage and Spend')
    that match what users see in sidebar/nav elements.
    
    DETECTS EXTERNAL LINKS: If target is on GitHub or other external domain,
    adds a warning note to the LAST_MILE entry.
    """
    if not transition_nodes:
        return [f"LAST_MILE: {goal}"]

    strategy = []
    has_external_target = False
    
    for i, node in enumerate(transition_nodes):
        label = _nav_label_from_node(node)
        is_last = (i == len(transition_nodes) - 1)
        has_url = bool(node.get("url"))
        
        # Check if this is an external URL (GitHub, etc.)
        node_url = node.get("url", "")
        if node_url and "github.com" in node_url:
            has_external_target = True
        
        if is_last:
            # The target leaf — add both a click and LAST_MILE
            strategy.append(f"Click {label}")
            if has_external_target:
                # Warn that this leads to external site (GitHub)
                strategy.append(f"LAST_MILE: {goal} [NOTE: Opens GitHub cookbooks for code examples]")
            else:
                strategy.append(f"LAST_MILE: {goal}")
        else:
            # Intermediate navigation node
            # Skip logical grouping folders (no URL) as they often aren't standalone clickable pages
            if has_url:
                strategy.append(f"Click {label}")

    return strategy


# ═══════════════════════════════════════════════════════════════════════
# Top-Level Index Traversal — The main entry point
# ═══════════════════════════════════════════════════════════════════════

def traverse_index(
    current_url: str,
    goal: str,
    index: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Main traversal function. Replaces quick_hive_probe for domains
    that have a PageIndex.

    Returns a dict compatible with the pre-decision gate format:
    {
        "has_index": bool,
        "current_node": {...} or None,
        "target_node": {...} or None,
        "transition_path": [...],
        "recommended_strategy_order": [...],
        "confidence": float,
        "reasoning": str,
    }
    """
    t_start = time.time()

    if not index:
        index = load_index()

    if not index or not index.get("root"):
        return {
            "has_index": False,
            "current_node": None,
            "target_node": None,
            "transition_path": [],
            "recommended_strategy_order": [f"LAST_MILE: {goal}"],
            "confidence": 0.0,
            "reasoning": "No PageIndex available",
        }

    # Step A: Locate current node
    current_node = locate_current_node(current_url, index)

    # Step B: Find target node
    target_node = find_target_node(goal, index)

    # Step C: Calculate transition path
    if current_node and target_node:
        if current_node.get("node_id") == target_node.get("node_id"):
            # Already at the target node
            strategy = [f"LAST_MILE: {goal}"]
            reasoning = (
                f"Already at target node '{current_node.get('title', '?')}'. "
                f"Goal can be achieved directly on this page."
            )
            confidence = 0.95
            transition = []
        else:
            transition = calculate_transition_path(current_node, target_node, index)
            strategy = generate_strategy_from_path(transition, goal)
            reasoning = (
                f"Current: '{current_node.get('title', '?')}' (node={current_node.get('node_id', '?')}). "
                f"Target: '{target_node.get('title', '?')}' (node={target_node.get('node_id', '?')}). "
                f"Path: {' → '.join(n.get('title', '?') for n in transition)}."
            )
            confidence = 0.90
    elif target_node:
        # Can't locate current position but know the target
        transition = [target_node]
        strategy = generate_strategy_from_path(transition, goal)
        reasoning = (
            f"Current node unknown (URL not in index). "
            f"Target: '{target_node.get('title', '?')}'. "
            f"Navigating directly."
        )
        confidence = 0.75
    else:
        # Neither current nor target found
        transition = []
        strategy = [f"LAST_MILE: {goal}"]
        reasoning = "No matching node found in PageIndex for this goal."
        confidence = 0.50

    elapsed_ms = int((time.time() - t_start) * 1000)
    logger.info(
        f"PageIndex traverse | url={current_url} goal='{goal[:60]}' "
        f"current={current_node.get('node_id', 'none') if current_node else 'none'} "
        f"target={target_node.get('node_id', 'none') if target_node else 'none'} "
        f"path_len={len(transition)} conf={confidence:.2f} ms={elapsed_ms}"
    )

    return {
        "has_index": True,
        "current_node": _slim_node(current_node) if current_node else None,
        "target_node": _slim_node(target_node) if target_node else None,
        "transition_path": [_slim_node(n) for n in transition],
        "recommended_strategy_order": strategy,
        "confidence": confidence,
        "reasoning": reasoning,
        "traverse_ms": elapsed_ms,
    }


def _slim_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """Return a lightweight copy of a node for logging/transmission."""
    if not node:
        return {}
    return {
        "node_id": node.get("node_id", ""),
        "title": node.get("title", ""),
        "logical_path": node.get("logical_path", ""),
        "summary_of_contents": node.get("summary_of_contents", ""),
        "terminal_capabilities": node.get("terminal_capabilities", []),
        "expected_controls": node.get("expected_controls", []),
    }


# ═══════════════════════════════════════════════════════════════════════
# LLM-Enhanced Traversal (for complex goals)
# ═══════════════════════════════════════════════════════════════════════

def traverse_index_with_llm(
    current_url: str,
    goal: str,
    nodes: List[Any] = None,
    index: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Enhanced traversal that uses the LLM (gpt-oss-20b) to reason over
    the PageIndex tree when deterministic matching isn't conclusive enough.

    Falls back to deterministic traverse_index if LLM is unavailable.
    """
    # First, try deterministic traversal
    result = traverse_index(current_url, goal, index)

    # If confidence is already high, use deterministic result
    if result.get("confidence", 0.0) >= 0.85:
        return result

    # Try LLM-enhanced reasoning
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return result

    if not index:
        index = load_index()
    if not index or not index.get("root"):
        return result

    try:
        # Build a compact representation of top-level node summaries
        root = index["root"]
        top_summaries = _build_top_level_summaries(root)

        prompt = f"""You are a web navigation pathfinder. Given a site map tree and a user goal,
determine the exact navigation path through the tree to reach the target.

SITE MAP (top-level summaries):
{top_summaries}

CURRENT URL: {current_url}
CURRENT NODE: {result.get('current_node', {}).get('title', 'Unknown')} ({result.get('current_node', {}).get('node_id', 'unknown')})
USER GOAL: {goal}

Based on the goal and current URL, which node in the tree contains the answer?
Provide:
1. target_node_id: The node_id of the target node
2. transition_clicks: Array of click labels to get there (e.g., ["Click Docs", "Click Prompt Caching"])
3. reasoning: One sentence explaining your path choice

Respond in JSON only:
{{"target_node_id": "...", "transition_clicks": [...], "reasoning": "..."}}"""

        payload = {
            "model": "openai/gpt-oss-20b",
            "messages": [
                {"role": "system", "content": "You are a precise navigation pathfinder. Respond with JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_completion_tokens": 200,
        }

        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()

        # Parse LLM response
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(content[start:end])
            target_id = data.get("target_node_id", "")
            clicks = data.get("transition_clicks", [])
            reasoning = data.get("reasoning", "")

            if target_id:
                target_node = find_node_by_id(target_id, index)
                if target_node:
                    strategy = list(clicks) + [f"LAST_MILE: {goal}"]
                    result.update({
                        "target_node": _slim_node(target_node),
                        "recommended_strategy_order": strategy,
                        "confidence": 0.92,
                        "reasoning": f"LLM pathfinding: {reasoning}",
                    })

    except Exception as e:
        logger.warning(f"PageIndex LLM traversal failed: {e}")
        # Fall back to deterministic result

    return result


def _build_top_level_summaries(root: Dict[str, Any], depth: int = 0, max_depth: int = 3) -> str:
    """Build a compact text representation of the tree for LLM context."""
    lines = []
    indent = "  " * depth
    node_id = root.get("node_id", "?")
    title = root.get("title", "?")
    summary = root.get("summary_of_contents", "")[:120]
    caps = root.get("terminal_capabilities", [])
    cap_str = f" [caps: {', '.join(caps[:3])}]" if caps else ""

    lines.append(f"{indent}• {node_id}: {title} — {summary}{cap_str}")

    if depth < max_depth:
        for child in root.get("children", []):
            lines.append(_build_top_level_summaries(child, depth + 1, max_depth))

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# Domain Check — Is this domain indexed?
# ═══════════════════════════════════════════════════════════════════════

def is_domain_indexed(domain: str) -> bool:
    """Check if a domain has a PageIndex available."""
    index = load_index()
    if not index:
        return False
    indexed_domain = index.get("site_metadata", {}).get("domain", "")
    domain_clean = domain.replace("www.", "").lower()
    indexed_clean = indexed_domain.replace("www.", "").lower()
    # Match both console.groq.com and groq.com
    return domain_clean == indexed_clean or domain_clean in indexed_clean or indexed_clean in domain_clean


def get_index_domain() -> str:
    """Get the domain of the loaded index."""
    index = load_index()
    return index.get("site_metadata", {}).get("domain", "") if index else ""

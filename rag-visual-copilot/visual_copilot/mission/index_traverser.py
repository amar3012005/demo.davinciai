import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import httpx

logger = logging.getLogger("vc.mission.index_traverser")

def _normalize_path(path: str) -> str:
    return path.lower().rstrip('/') or '/'

def _find_current_node(node: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    # Depth-first search, keeping the deepest match
    best_match = None
    
    node_regex = node.get("path_regex")
    if node_regex:
        try:
            if re.search(node_regex, path):
                best_match = node
        except Exception as e:
            logger.warning(f"Invalid regex in node {node.get('node_id')}: {e}")
            
    for child in node.get("children", []):
        child_match = _find_current_node(child, path)
        if child_match:
            best_match = child_match
            
    return best_match

def _get_api_key() -> str:
    return os.getenv("GROQ_API_KEY", "")

def _build_children_summaries(node: Dict[str, Any]) -> str:
    lines = []
    for child in node.get("children", []):
        node_id = child.get("node_id", "?")
        title = child.get("title", "?")
        summary = child.get("summary_of_contents", "")
        # Include capabilities to give the LLM better "scent" for deep features
        caps = ", ".join(child.get("terminal_capabilities", []))
        line = f"- ID: {node_id} | Title: {title} | Summary: {summary}"
        if caps:
            line += f" | Capabilities: {caps}"
        lines.append(line)
    return "\n".join(lines)

def _llm_choose_branch(current_node: Dict[str, Any], goal: str) -> Optional[Dict[str, Any]]:
    children = current_node.get("children", [])
    if not children:
        return None
        
    summaries = _build_children_summaries(current_node)
    
    prompt = f"""You are the TARA Master Pathfinder. Your mission is to decode human intent and map it to the optimal branch of a website's logical hierarchy.

Humans use "Free Language" (slang, emotional states, implied goals). You must use "Information Scent" reasoning to find the target.

### USER CONTEXT:
- **INTENT**: "{goal}"
- **CURRENT LOCATION**: {current_node.get('title', 'Unknown')} ({current_node.get('node_id', 'unknown')})

### SUB-PAGES AVAILABLE:
{summaries}

### NAVIGATION MASTER RULES:
1. **The "Soul" over the "Syntax"**: If a human says "it's broken," they want 'Status' or 'Support'. If they say "how much," they want 'Pricing' or 'Usage'. Do not look for literal word matches; look for the *result* they want.
2. **Information Scent**: Which branch *smells* like it leads to the goal? 
   - "Usage/Billing" smells like: money, cost, tokens, math, spending, history, limits.
   - "Documentation/Guides" smells like: how to, what is, setup, integration, examples, broken, error.
   - "Dashboard/Console" smells like: show me, where is, status, control, toggle, active.
3. **Handle Implied Nav**: If the intent is "find a video" but no branch says "video," look for "Media," "Gallery," "Library," or "Resources."
4. **Zero-Hallucination**: You MUST only return a `target_node_id` that exists in the list above.

Respond with a JSON object. Ensure your reasoning explicitly connects the human's "Free Language" to the technical capabilities of the chosen node.
{{"target_node_id": "...", "reasoning": "..."}}"""

    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [
            {"role": "system", "content": "You are a precise navigation pathfinder. Respond with JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_completion_tokens": 150,
    }

    api_key = _get_api_key()
    if not api_key:
        logger.warning("No GROQ_API_KEY found, returning None")
        return None

    try:
        with httpx.Client(timeout=10.0) as client:
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

        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(content[start:end])
            target_id = data.get("target_node_id", "")
            for child in children:
                if child.get("node_id") == target_id:
                    return child
    except Exception as e:
        logger.warning(f"IndexTraverser LLM traversal failed: {e}")

    return None

def _get_path_from_node_to_root(node: Dict[str, Any], root: Dict[str, Any]) -> List[Dict[str, Any]]:
    path = []
    current = node
    
    # We need to build parent pointers conceptually or search from root
    def find_path(current_search: Dict[str, Any], target_id: str, current_path: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        current_path.append(current_search)
        if current_search.get("node_id") == target_id:
            return list(current_path)
            
        for child in current_search.get("children", []):
            res = find_path(child, target_id, current_path)
            if res:
                return res
                
        current_path.pop()
        return None
        
    return find_path(root, node.get("node_id", ""), []) or []

def get_path_to_goal(current_url: str, goal: str, site_map: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main IndexTraverser logic requested by the user.
    Uses path_regex to find current node, then reasoning LLM to find the goal leaf node.
    Returns:
    {
        "recommended_strategy_order": ["Click X", "Click Y", "LAST_MILE: goal"],
        "target_node": {...}
    }
    """
    try:
        parsed = urlparse(current_url if "://" in current_url else f"http://{current_url}")
        path = _normalize_path(parsed.path)
    except Exception:
        path = "/"

    root = site_map.get("root")
    if not root:
        return {"recommended_strategy_order": [f"LAST_MILE: {goal}"], "target_node": None}

    # State Alignment
    current_node = _find_current_node(root, path)
    if not current_node:
        current_node = root
        
    logger.info(f"IndexTraverser | START | URL: {current_url} | Node: {current_node.get('node_id')}")

    # Reasoning-Based Search (Global Top-Down)
    # Start from root to ensure we can reach any node in the tree, 
    # even if it requires jumping between distant branches (e.g. from Docs to Dashboard).
    active_node = root
    reasoning_path = [root.get("node_id", "root")]
    while True:
        if not active_node.get("children"):
            # It's a leaf node
            break
            
        next_node = _llm_choose_branch(active_node, goal)
        if not next_node:
            # We don't know where to go next, or LLM failed
            break
        
        active_node = next_node
        reasoning_path.append(active_node.get("node_id", "unknown"))

    logger.info(f"IndexTraverser | GOAL | Node: {active_node.get('node_id')} | Reasoning Path: {' -> '.join(reasoning_path)}")

    # Generate path
    path_nodes = _get_path_from_node_to_root(active_node, root)
    if not path_nodes:
        return {"recommended_strategy_order": [f"LAST_MILE: {goal}"], "target_node": active_node}
        
    # We want transitions from current_node to active_node. 
    # To keep it simple, we just generate clicks from root to target, or from current to target
    # The prompt asks for root -> branch transitions as clicks
    strategy = []
    
    # We only care about steps taken after current node
    start_index = 0
    for i, node in enumerate(path_nodes):
        if node.get("node_id") == current_node.get("node_id"):
            start_index = i
            break
            
    path_remainder = path_nodes[start_index+1:]
    for i, node in enumerate(path_remainder):
        title = node.get("title", "")
        has_url = bool(node.get("url"))
        is_last = (i == len(path_remainder) - 1)
        
        if title:
            # Skip logical grouping folders (no URL) as they often aren't standalone clickable pages
            if is_last or has_url:
                strategy.append(f"Click {title}")
            
    strategy.append(f"LAST_MILE: {goal}")
    
    return {
        "recommended_strategy_order": strategy,
        "target_node": active_node
    }

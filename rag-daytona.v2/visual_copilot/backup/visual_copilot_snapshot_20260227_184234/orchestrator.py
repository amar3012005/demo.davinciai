"""
Ultimate TARA API Endpoints

New pipeline using:
- Mind Reader (intent parsing)
- Hive Interface (Qdrant retrieval)
- Mission Brain (constraint enforcement)
- Semantic Detective (hybrid scoring)
- Live Graph (Redis DOM mirror)
"""

import logging
import os
import re
import time
import unicodedata
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass
from tara_models import ActionIntent, HiveResponse

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TARA_ROUTER_V2_ENABLED = _env_bool("TARA_ROUTER_V2_ENABLED", False)
TARA_ROUTER_V2_SHADOW = _env_bool("TARA_ROUTER_V2_SHADOW", True)
DETECTIVE_MIN_SCORE = float(os.getenv("DETECTIVE_MIN_SCORE", "0.45"))
DETECTIVE_AMBIGUOUS_BAND = float(os.getenv("DETECTIVE_AMBIGUOUS_BAND", "0.12"))
LEXICAL_DIRECT_ACCEPT = float(os.getenv("LEXICAL_DIRECT_ACCEPT", "0.70"))
LEXICAL_DIRECT_ACCEPT_CLICK = float(os.getenv("LEXICAL_DIRECT_ACCEPT_CLICK", "0.75"))
LEXICAL_DIRECT_ACCEPT_TYPE = float(os.getenv("LEXICAL_DIRECT_ACCEPT_TYPE", "0.75"))
_canary_domains_raw = os.getenv("ROUTER_V2_CANARY_DOMAINS")
if not _canary_domains_raw:
    _canary_domains_raw = "console.groq.com,groq.com"
ROUTER_V2_CANARY_DOMAINS = {
    d.strip().lower().replace("www.", "")
    for d in _canary_domains_raw.split(",")
    if d.strip()
}
MAX_DETECTIVE_RETRIES_PER_SUBGOAL = int(os.getenv("MAX_DETECTIVE_RETRIES_PER_SUBGOAL", "1"))
ENABLE_LAST_MILE_REASONING = _env_bool("ENABLE_LAST_MILE_REASONING", False)
LAST_MILE_MAX_ATTEMPTS = int(os.getenv("LAST_MILE_MAX_ATTEMPTS", "4"))
_last_mile_canary_raw = os.getenv("LAST_MILE_CANARY_DOMAINS")
if not _last_mile_canary_raw:
    _last_mile_canary_raw = "console.groq.com,groq.com,engelvoelkers.com,pornpics.de"
LAST_MILE_CANARY_DOMAINS = {
    d.strip().lower().replace("www.", "")
    for d in _last_mile_canary_raw.split(",")
    if d.strip()
}
ENABLE_KEYWORD_DIRECT_V3 = _env_bool("ENABLE_KEYWORD_DIRECT_V3", False)
ENABLE_SUBGOAL_HINT_QUERY = _env_bool("ENABLE_SUBGOAL_HINT_QUERY", False)
ENABLE_VERIFIED_ADVANCE = _env_bool("ENABLE_VERIFIED_ADVANCE", False)
DEBUG_TRACE_OUTPUTS = _env_bool("DEBUG_TRACE_OUTPUTS", False)
_v3_canary_raw = os.getenv("V3_CANARY_DOMAINS")
if not _v3_canary_raw:
    _v3_canary_raw = "engelvoelkers.com,pornpics.de,groq.com,console.groq.com"
V3_CANARY_DOMAINS = {
    d.strip().lower().replace("www.", "")
    for d in _v3_canary_raw.split(",")
    if d.strip()
}
V3_ALWAYS_ON_ROOTS = {"groq.com"}
V3_AUTO_ROLLBACK_ENABLED = _env_bool("V3_AUTO_ROLLBACK_ENABLED", True)
V3_AUTO_ROLLBACK_MAX_PENDING_DROPS = int(os.getenv("V3_AUTO_ROLLBACK_MAX_PENDING_DROPS", "3"))
_V3_PENDING_DROP_COUNTS: Dict[str, int] = {}
_V3_DISABLED_DOMAINS: set[str] = set()

_NOISE_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your",
    "read", "check", "show", "find", "open", "click", "navigate", "please",
    "stats", "data", "tab", "page", "section"
}
_DOMAIN_ALIASES = {"usage", "billing", "activity", "analytics", "models", "token", "tokens"}
_CLICK_TAGS = {"a", "button", "summary"}
_CLICK_ROLES = {"button", "link", "tab", "menuitem"}
_TYPE_TAGS = {"input", "textarea", "select"}
_TYPE_ROLES = {"searchbox", "combobox", "textbox"}
_TEXT_HEAVY_TAGS = {"div", "summary", "p", "section", "article"}
_ZONE_HINTS = {
    "sidebar": {"sidebar", "left", "menu"},
    "nav": {"nav", "navigation", "header", "top"},
    "footer": {"footer", "bottom"},
    "main": {"main", "content"},
}

_DOMAIN_LABEL_SYNONYMS = {
    "engelvoelkers.com": {
        "kaufen and mieten": ["buy and rent", "buy rent"],
        "kaufen": ["buy"],
        "mieten": ["rent"],
    }
}

_GALLERY_WORDS = {"gallery", "image", "images", "photo", "photos", "pic", "pics", "thumbnail", "thumbnails", "card", "cards"}


@dataclass
class MatchResult:
    candidate_id: Optional[str]
    matched_label: str
    match_mode: str
    raw_node_id: Optional[str]
    reason: str


def _is_reclick_safe_node(node: Any) -> bool:
    """
    Some controls are intentionally clicked multiple times (tabs, menus, dropdown triggers).
    Keep them eligible even if present in click history.
    """
    if not node:
        return False
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    zone = (getattr(node, "zone", "") or "").lower()
    text = _canonicalize_label(getattr(node, "text", "") or "")
    node_id = (getattr(node, "id", "") or "").lower()
    state = (getattr(node, "state", "") or "").lower()
    aria_expanded = str(getattr(node, "aria_expanded", "") or "").lower()
    aria_selected = str(getattr(node, "aria_selected", "") or "").lower()

    if tag not in _CLICK_TAGS and role not in _CLICK_ROLES:
        return False

    trigger_words = {"menu", "dropdown", "tab", "tabs", "trigger", "developers", "docs", "documentation", "usage", "activity"}
    has_trigger_signal = any(w in text for w in trigger_words) or any(w in node_id for w in trigger_words)
    has_state_signal = any(v in {"true", "false"} for v in {aria_expanded, aria_selected}) or state in {
        "expanded", "collapsed", "active", "inactive", "open", "closed", "selected"
    }
    in_nav_cluster = zone in {"nav", "sidebar", "header", "menu", "modal"}

    return bool(
        role in {"tab", "menuitem"}
        or (in_nav_cluster and (has_trigger_signal or has_state_signal))
        or ("trigger" in node_id)
    )


def _drop_reclick_safe_exclusions(excluded_ids: set[str], nodes: List[Any]) -> set[str]:
    if not excluded_ids:
        return excluded_ids
    by_id = {getattr(n, "id", ""): n for n in nodes}
    removed: List[str] = []
    for nid in list(excluded_ids):
        node = by_id.get(nid)
        if node and _is_reclick_safe_node(node):
            excluded_ids.discard(nid)
            removed.append(nid)
    if removed:
        logger.info(
            f"EXCLUSION_RELAX removed={len(removed)} reclick_safe_ids={removed[:4]}"
        )
    return excluded_ids


def _mission_progress_label(mission: Any) -> str:
    subgoals = list(getattr(mission, "subgoals", []) or [])
    total = len(subgoals)
    raw_idx = int(getattr(mission, "current_subgoal_index", 0) or 0)
    if total <= 0:
        return f"{raw_idx}/0 (step 0 of 0)"
    shown_idx = min(max(0, raw_idx), total - 1)
    suffix = " terminal_handoff" if raw_idx >= total else ""
    return f"{shown_idx}/{total} (step {shown_idx + 1} of {total}{suffix})"


def _extract_visible_goal_evidence(main_goal: str, nodes: List[Any]) -> str:
    """
    Deterministically extract a short evidence line from visible content.
    Used as a last-mile fallback before giving up.
    """
    goal_terms = _tokenize(main_goal)
    if not goal_terms:
        return ""
    candidates: List[Tuple[int, int, str]] = []
    required_overlap = max(2, min(4, len(goal_terms)))
    for n in nodes:
        zone = (getattr(n, "zone", "") or "").lower()
        if zone not in {"main", "content"}:
            continue
        text = (getattr(n, "text", "") or "").strip()
        if len(text) < 18:
            continue
        line_terms = _tokenize(text)
        overlap = len(goal_terms & line_terms)
        has_numeric_signal = bool(re.search(r"\d", text))
        if overlap >= required_overlap and has_numeric_signal:
            candidates.append((overlap, len(text), text))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][2][:320]


def _extract_model_usage_evidence(main_goal: str, nodes: List[Any]) -> str:
    """
    Specialized extractor for usage pages where model name and value are split
    across adjacent nodes (e.g., h3 model line + p dollar amount).
    """
    goal_tokens = _tokenize(main_goal)
    if not goal_tokens:
        return ""

    # Prefer model-like tokens from goal to anchor extraction.
    model_tokens = [
        t for t in sorted(goal_tokens)
        if t not in {"show", "read", "usage", "token", "tokens", "model", "models", "cost", "spend", "dashboard", "console", "groq"}
    ]
    if not model_tokens:
        return ""

    ordered = [n for n in nodes if (getattr(n, "zone", "") or "").lower() in {"main", "content"}]
    for idx, n in enumerate(ordered):
        txt = (getattr(n, "text", "") or "").strip()
        if not txt:
            continue
        canon = _canonicalize_label(txt)
        if not all(tok in canon for tok in model_tokens[: min(2, len(model_tokens))]):
            continue

        # Search nearby nodes for usage/cost value.
        for j in range(idx + 1, min(idx + 5, len(ordered))):
            vtxt = (getattr(ordered[j], "text", "") or "").strip()
            if not vtxt:
                continue
            if re.search(r"[$€£]\s?\d", vtxt) or re.search(r"\d[\d,\.]*\s?(?:tokens?|usd)", vtxt.lower()):
                return f"{txt}: {vtxt}"[:320]
    return ""


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 2 and t not in _NOISE_WORDS
    }


def _canonicalize_label(text: str) -> str:
    if not text:
        return ""
    txt = unicodedata.normalize("NFKD", text)
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    txt = txt.lower()
    txt = txt.replace("&", " and ")
    txt = txt.replace("+", " and ")
    txt = txt.replace(" und ", " and ")
    txt = re.sub(r"[^a-z0-9]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _expand_label_synonyms(label: str, domain: str) -> List[str]:
    canonical = _canonicalize_label(label)
    if not canonical:
        return []
    variants = {canonical}
    domain_key = (domain or "").lower().replace("www.", "")
    for base_domain, mapping in _DOMAIN_LABEL_SYNONYMS.items():
        if domain_key.endswith(base_domain):
            for src, targets in mapping.items():
                src_can = _canonicalize_label(src)
                if src_can == canonical:
                    variants.update(_canonicalize_label(t) for t in targets)
                for t in targets:
                    if _canonicalize_label(t) == canonical:
                        variants.add(src_can)
    return sorted(v for v in variants if v)

def _extract_zone_targets(query: str) -> set[str]:
    q = (query or "").lower()
    zones = set()
    if any(k in q for k in _ZONE_HINTS["sidebar"]):
        zones.add("sidebar")
    if any(k in q for k in _ZONE_HINTS["nav"]):
        zones.add("nav")
    if any(k in q for k in _ZONE_HINTS["footer"]):
        zones.add("footer")
    if any(k in q for k in _ZONE_HINTS["main"]):
        zones.add("main")
    return zones


def _zone_compatible_for_direct(requested_zones: set[str], actual_zone: str) -> bool:
    """
    For direct lexical actions, treat nav/sidebar/header as a compatible cluster.
    This avoids rejecting exact label hits when site IA varies wording.
    """
    if not requested_zones:
        return True
    actual = (actual_zone or "").lower()
    if actual in requested_zones:
        return True
    nav_cluster = {"nav", "sidebar", "header", "menu"}
    if requested_zones & nav_cluster and actual in nav_cluster:
        return True
    return False


def _explicit_query_terms(query: str) -> set[str]:
    tokens = _tokenize(query)
    # Remove zone/location control words so lexical direct needs label matches
    zone_words = {"sidebar", "left", "menu", "nav", "navigation", "header", "top", "footer", "bottom", "main", "content", "tab", "link"}
    return {t for t in tokens if t not in zone_words}


def _strategy_focus_terms(query: str) -> set[str]:
    terms = _explicit_query_terms(query)
    generic_ops = {
        "use", "open", "click", "select", "navigate", "locate", "find", "read",
        "view", "check", "verify", "goto", "go", "section", "page", "step",
    }
    return {t for t in terms if t not in generic_ops}


def _node_matches_strategy_focus(node: Any, query: str) -> bool:
    terms = _strategy_focus_terms(query)
    if not terms:
        return True
    blob = _canonicalize_label(_collect_node_text_for_match(node))
    if not blob:
        return False
    return any(t in blob for t in terms)


def _subgoal_focus_visible(query: str, nodes: List[Any]) -> bool:
    """
    Verify that the current page still reflects the intended strategy subgoal focus.
    Used to avoid advancing on unrelated DOM changes.
    """
    terms = _strategy_focus_terms(query)
    if not terms:
        return True
    for n in nodes:
        txt = _canonicalize_label(getattr(n, "text", "") or "")
        if not txt:
            continue
        if all(t in txt for t in terms):
            return True
    return False


def _root_domain(host: str) -> str:
    parts = (host or "").replace("www.", "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").replace("www.", "")


def _domain_in_list(host: str, allowed: set[str]) -> bool:
    host = (host or "").lower().replace("www.", "")
    if not host:
        return False
    if host in allowed:
        return True
    host_root = _root_domain(host)
    return host_root in {_root_domain(d) for d in allowed}


def _is_v3_feature_enabled(host: str, global_enabled: bool) -> bool:
    host = (host or "").lower().replace("www.", "")
    host_root = _root_domain(host)
    if host_root in V3_ALWAYS_ON_ROOTS:
        return True
    if host_root in _V3_DISABLED_DOMAINS:
        return False
    if global_enabled:
        return True
    return _domain_in_list(host, V3_CANARY_DOMAINS)


def _register_v3_pending_drop(domain: str) -> None:
    if not V3_AUTO_ROLLBACK_ENABLED:
        return
    key = _root_domain((domain or "").lower().replace("www.", ""))
    if not key:
        return
    count = _V3_PENDING_DROP_COUNTS.get(key, 0) + 1
    _V3_PENDING_DROP_COUNTS[key] = count
    if count >= max(1, V3_AUTO_ROLLBACK_MAX_PENDING_DROPS):
        if key not in _V3_DISABLED_DOMAINS:
            _V3_DISABLED_DOMAINS.add(key)
            logger.warning(
                f"V3_AUTO_ROLLBACK domain={key} reason=pending_drops "
                f"count={count} threshold={V3_AUTO_ROLLBACK_MAX_PENDING_DROPS}"
            )


def _register_v3_success(domain: str) -> None:
    key = _root_domain((domain or "").lower().replace("www.", ""))
    if not key:
        return
    if key in _V3_PENDING_DROP_COUNTS:
        _V3_PENDING_DROP_COUNTS[key] = 0


def _is_canary_domain(host: str) -> bool:
    if not ROUTER_V2_CANARY_DOMAINS:
        return False
    host = (host or "").lower().replace("www.", "")
    if host in ROUTER_V2_CANARY_DOMAINS:
        return True
    host_root = _root_domain(host)
    return host_root in {_root_domain(d) for d in ROUTER_V2_CANARY_DOMAINS}


def _is_last_mile_enabled_for_domain(host: str) -> bool:
    if ENABLE_LAST_MILE_REASONING:
        return True
    host = (host or "").lower().replace("www.", "")
    if not host:
        return False
    if host in LAST_MILE_CANARY_DOMAINS:
        return True
    host_root = _root_domain(host)
    return host_root in {_root_domain(d) for d in LAST_MILE_CANARY_DOMAINS}


def _classify_subgoal_mode(query: str) -> str:
    q = (query or "").lower()
    if any(k in q for k in ["read", "check", "verify", "review", "analyze", "summarize", "look at"]):
        return "cognitive_read"
    if any(k in q for k in ["type", "enter", "search for", "fill", "input"]):
        return "literal_type"
    if any(k in q for k in ["click", "open tab", "select", "press", "toggle", " tab", "button", "link", "menu", "locate"]):
        return "literal_click"
    if q.startswith("open ") and any(w in q for w in _GALLERY_WORDS):
        return "literal_click"
    if any(k in q for k in ["go to", "navigate", "open", "take me", "switch to"]):
        return "cognitive_navigate"
    return "ambiguous"


def _is_clickable_node(node: Any) -> bool:
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    return tag in _CLICK_TAGS or role in _CLICK_ROLES


def _is_type_node(node: Any) -> bool:
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    return tag in _TYPE_TAGS or role in _TYPE_ROLES


def _is_text_heavy(tag: str, text: str) -> bool:
    words = len((text or "").split())
    return tag.lower() in _TEXT_HEAVY_TAGS and words >= 8


def _extract_type_text(query: str, default_text: str) -> str:
    m = re.search(r"type\s+'([^']+)'", query, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"search for\s+(.+)$", query, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return (default_text or "").strip()[:80]


def _extract_explicit_target_id(query: str) -> str:
    if not query:
        return ""
    m = re.search(r"\[(?:ID:\s*)?([^\]\s]+)\]", query, re.IGNORECASE)
    if not m:
        return ""
    return (m.group(1) or "").strip().rstrip("]")


def _candidate_signature(text: str, tag: str, zone: str) -> str:
    compact = re.sub(r"\s+", " ", (text or "").lower()).strip()[:96]
    return f"{tag.lower()}|{zone.lower()}|{compact}"


def _action_tag_compatible(subgoal_mode: str, candidate: Any) -> bool:
    if not candidate:
        return False
    tag = (getattr(candidate, "tag", "") or "").lower()
    text = getattr(candidate, "text", "") or ""
    role = (getattr(candidate, "role", "") or "").lower()
    if subgoal_mode == "literal_type":
        return tag in _TYPE_TAGS or role in _TYPE_ROLES
    if subgoal_mode == "literal_click":
        if tag in _CLICK_TAGS or role in _CLICK_ROLES:
            return not _is_text_heavy(tag, text)
        return False
    return True


def _should_enter_last_mile(mission: Any, query: str, schema: Any) -> Tuple[bool, str]:
    """
    Last-mile should start only when strategy is exhausted or completion was explicit.
    """
    if not mission:
        return False, "no_mission"
    phase = getattr(mission, "phase", "strategy")
    if phase == "last_mile":
        return True, "phase_last_mile"
    subgoals = getattr(mission, "subgoals", []) or []
    idx = int(getattr(mission, "current_subgoal_index", 0) or 0)
    if idx >= len(subgoals):
        return True, "strategy_exhausted"
    q = (query or "").lower().strip()
    if "extract and present" in q:
        return True, "explicit_extract_subgoal"
    if getattr(schema, "zero_shot_mode", False) and getattr(mission, "status", "") == "completed":
        return True, "zero_shot_completed"
    return False, "strategy_in_progress"


def _goal_completion_guard(main_goal: str, nodes: List[Any]) -> bool:
    """
    Deterministic completion check to avoid "arrived but not done".
    """
    goal_tokens = _tokenize(main_goal)
    if not goal_tokens:
        return False
    content_nodes = [
        n for n in nodes
        if getattr(n, "text", None) and getattr(n, "zone", "") == "main"
    ]
    hits = 0
    for n in content_nodes:
        text_tokens = _tokenize(getattr(n, "text", ""))
        if len(goal_tokens & text_tokens) >= min(2, max(1, len(goal_tokens) // 3)):
            hits += 1
    return hits >= 2


def _validate_last_mile_step(step: Dict[str, Any], nodes: List[Any], excluded_ids: set[str]) -> Tuple[bool, str]:
    action = (step.get("action") or "").lower()
    target_id = (step.get("target_id") or "").strip()
    original_target_id = target_id
    if action not in {"click", "type_text", "select", "scroll", "answer"}:
        return False, "unsupported_action"
    if action in {"scroll", "answer"}:
        return True, "ok"
    if not target_id:
        return False, "missing_target_id"
    node = next((n for n in nodes if getattr(n, "id", "") == target_id and getattr(n, "interactive", False)), None)
    if action in {"click", "select"}:
        # Last-mile may legitimately re-use previously clicked controls (tabs/dropdowns).
        resolved_id, resolve_reason = _resolve_clickable_target_id(target_id, nodes, set())
        if not resolved_id:
            raw_node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
            fallback_labels: List[str] = []
            if raw_node and getattr(raw_node, "text", ""):
                fallback_labels.append(getattr(raw_node, "text", ""))
            if step.get("text"):
                fallback_labels.append(step.get("text", ""))
            if fallback_labels:
                fallback_id, fallback_reason = _resolve_clickable_by_label_context(
                    target_id,
                    fallback_labels,
                    "",
                    nodes,
                    excluded_ids=set(),
                )
                if fallback_id:
                    resolved_id = fallback_id
                    resolve_reason = f"label_context:{fallback_reason}"
        if not resolved_id:
            return False, resolve_reason
        if resolved_id != target_id:
            step["target_id"] = resolved_id
            target_id = resolved_id
            logger.info(
                f"LAST_MILE_STEP_RESOLVE original={original_target_id} resolved={resolved_id}"
            )
        node = next((n for n in nodes if getattr(n, "id", "") == target_id and getattr(n, "interactive", False)), None)
    if not node:
        return False, "target_not_in_live_graph"
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    if action == "type_text":
        if not (tag in _TYPE_TAGS or role in _TYPE_ROLES):
            return False, "type_target_not_input"
    if action in {"click", "select"}:
        if not (tag in _CLICK_TAGS or role in _CLICK_ROLES):
            return False, "click_target_not_clickable"
    return True, "ok"


def _validate_action_target(action: str, target_id: str, nodes: List[Any], *, excluded_ids: Optional[set[str]] = None) -> Tuple[bool, str]:
    """
    Shared grounding gate used by all tiers before returning click/type actions.
    """
    if action in {"answer", "wait", "clarify", "scroll"}:
        return True, "ok"
    if not target_id:
        return False, "missing_target_id"
    node = next((n for n in nodes if getattr(n, "id", "") == target_id and getattr(n, "interactive", False)), None)
    if not node:
        return False, "target_not_in_live_graph"
    if excluded_ids and action == "click" and target_id in excluded_ids:
        return False, "target_already_excluded"
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    if action == "type_text" and not (tag in _TYPE_TAGS or role in _TYPE_ROLES):
        return False, "type_target_not_input"
    if action == "click" and not (tag in _CLICK_TAGS or role in _CLICK_ROLES):
        return False, "click_target_not_clickable"
    return True, "ok"


def _node_text_blob(node: Any) -> str:
    return " ".join([
        getattr(node, "text", "") or "",
        getattr(node, "id", "") or "",
        getattr(node, "role", "") or "",
        getattr(node, "zone", "") or "",
        getattr(node, "state", "") or "",
        str(getattr(node, "placeholder", "") or ""),
        str(getattr(node, "name", "") or ""),
        str(getattr(node, "value", "") or ""),
    ]).lower()


def _find_best_type_target(nodes: List[Any], query: str, excluded_ids: set[str]) -> Optional[Any]:
    """
    Deterministic fallback for typing/search tasks when semantic ranking is noisy.
    """
    q = (query or "").lower()
    hints = {"search", "such", "suchort", "location", "city", "ort", "where"}
    query_terms = _tokenize(q)
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
        overlap = len(query_terms & _tokenize(blob))
        score += min(0.4, 0.1 * overlap)
        if getattr(n, "zone", "") in {"main", "nav"}:
            score += 0.05
        if score > best_score:
            best_score = score
            best_node = n
    if best_score >= 0.35:
        return best_node
    return None


def _retarget_click_to_nav_duplicate_if_needed(
    *,
    target_node: Any,
    nodes: List[Any],
    query: str,
    goal: str,
) -> Tuple[Any, str]:
    """
    For docs/dev intents, if selected target is in main content but same label exists
    in nav/sidebar/header, prefer that navigation target.
    """
    if not target_node:
        return target_node, "no_target"

    target_text = _canonicalize_label(getattr(target_node, "text", "") or "")
    if not target_text:
        return target_node, "no_target_text"

    ctx = _canonicalize_label(f"{query} {goal}")
    nav_intent_terms = {"docs", "documentation", "developer", "developers", "stt", "speech", "text", "model"}
    if not any(t in ctx for t in nav_intent_terms):
        return target_node, "no_nav_intent"

    zone = (getattr(target_node, "zone", "") or "").lower()
    if zone in {"nav", "sidebar", "header", "menu"}:
        return target_node, "already_nav_zone"

    dup = [
        n for n in nodes
        if getattr(n, "interactive", False)
        and _is_clickable_node(n)
        and (getattr(n, "id", "") or "") != (getattr(target_node, "id", "") or "")
        and _canonicalize_label(getattr(n, "text", "") or "") == target_text
        and (getattr(n, "zone", "") or "").lower() in {"nav", "sidebar", "header", "menu"}
    ]
    if not dup:
        return target_node, "no_nav_duplicate"

    zone_priority = {"nav": 0, "sidebar": 1, "header": 2, "menu": 3}
    dup.sort(key=lambda n: zone_priority.get((getattr(n, "zone", "") or "").lower(), 9))
    return dup[0], "nav_duplicate_preferred"


def _extract_quoted_labels(text: str) -> List[str]:
    if not text:
        return []
    labels: List[str] = []
    for m in re.finditer(r"'([^']{2,120})'|\"([^\"]{2,120})\"", text):
        label = (m.group(1) or m.group(2) or "").strip()
        if label:
            labels.append(label)
    return labels


def _extract_unquoted_label_phrase(text: str) -> str:
    """
    Extract deterministic label phrase from unquoted nav/click subgoals.
    Example: "Open Kaufen & Mieten in navigation" -> "Kaufen & Mieten"
    """
    if not text:
        return ""
    raw = re.sub(r"\[.*?\]", " ", text).strip()
    match = re.match(
        r"^\s*(?:click(?: on)?|open|select|choose|navigate to|go to|locate|find)\s+(.+?)\s*$",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    phrase = match.group(1)
    phrase = re.split(
        r"\b(?:in|on|at)\b\s+(?:the\s+)?(?:left|right|top|bottom|sidebar|nav|navigation|header|footer|main|content|menu|tab)\b",
        phrase,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    phrase = re.sub(r"\b(?:navigation|menu|tab|link|button|section|page)\b$", "", phrase, flags=re.IGNORECASE).strip(" -:,")
    # Ignore placeholder nouns that are not actual labels.
    if _canonicalize_label(phrase) in {"element", "item", "node", "target"}:
        return ""
    return phrase.strip()


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


def _override_mode_for_labels(subgoal_mode: str, label_candidates: List[str]) -> str:
    if subgoal_mode in {"cognitive_navigate", "ambiguous"} and label_candidates:
        return "literal_click"
    return subgoal_mode


def _is_gallery_subgoal(query: str) -> bool:
    q = (query or "").lower()
    return any(w in q for w in _GALLERY_WORDS) and any(v in q for v in ("open", "click", "show", "view"))


def _is_image_like_node(node: Any) -> bool:
    tag = (getattr(node, "tag", "") or "").lower()
    role = (getattr(node, "role", "") or "").lower()
    text = ((getattr(node, "text", "") or "") + " " + (getattr(node, "id", "") or "")).lower()
    if tag in {"img", "image", "picture", "figure"} or role in {"img", "image"}:
        return True
    return any(k in text for k in ("thumb", "thumbnail", "image", "photo", "pic", "gallery", "card"))


def _find_gallery_click_target(nodes: List[Any], excluded_ids: set[str]) -> Optional[Tuple[str, str]]:
    best = None
    best_score = -1.0
    for n in nodes:
        if not getattr(n, "interactive", False):
            continue
        nid = getattr(n, "id", "")
        if not nid or nid in excluded_ids:
            continue
        zone = (getattr(n, "zone", "") or "").lower()
        text = (getattr(n, "text", "") or "").lower()
        tag = (getattr(n, "tag", "") or "").lower()
        score = 0.0
        if _is_image_like_node(n):
            score += 0.8
        if zone == "main":
            score += 0.35
        if tag in _CLICK_TAGS:
            score += 0.15
        if tag in {"img", "picture", "figure"}:
            score += 0.25
        if zone in {"nav", "header", "sidebar", "footer"}:
            score -= 0.30
        if any(x in text for x in ("random gallery", "categories", "tags", "search")):
            score -= 0.25
        if score > best_score:
            best_score = score
            best = n
    if not best or best_score < 0.55:
        return None
    target_id = getattr(best, "id", "")
    resolved_id = target_id
    if (getattr(best, "tag", "") or "").lower() not in _CLICK_TAGS:
        rid, reason = _resolve_clickable_target_id(target_id, nodes, excluded_ids=excluded_ids)
        if rid:
            resolved_id = rid
    return resolved_id, target_id


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


def _find_hard_keyword_match(
    nodes: List[Any],
    labels: List[str],
    domain: str,
    subgoal_mode: str,
    excluded_ids: set[str]
) -> MatchResult:
    if not labels:
        return MatchResult(None, "", "none", None, "no_labels")
    expanded_labels: List[Tuple[str, str, str]] = []
    for raw in labels:
        for variant in _expand_label_synonyms(raw, domain):
            mode = "exact" if variant == _canonicalize_label(raw) else "synonym"
            expanded_labels.append((raw, variant, mode))

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
        # For click, allow non-clickable label nodes and resolve to clickable ancestor later.
        if subgoal_mode == "literal_type" and not _is_type_node(n):
            continue
        blob = _canonicalize_label(_collect_node_text_for_match(n))
        if not blob:
            continue
        for raw_label, norm_label, mode in expanded_labels:
            score = 0.0
            if norm_label == blob:
                score = 1.0
            elif norm_label in blob:
                score = 0.97 if mode == "exact" else 0.94
            else:
                label_tokens = set(norm_label.split())
                blob_tokens = set(blob.split())
                overlap = len(label_tokens & blob_tokens)
                if overlap > 0:
                    score = min(0.92, overlap / max(1, len(label_tokens)))
            blob_tokens = set(blob.split())
            if score <= 0.0:
                continue
            if mode == "synonym":
                score -= 0.01
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


def _build_label_policy(
    query: str,
    schema: Any,
    subgoal_mode: str,
    label_candidates: Optional[List[str]] = None,
    allow_first_subgoal_fallback: bool = True
) -> Dict[str, Any]:
    labels = list(
        label_candidates
        or _extract_label_candidates(
            query,
            schema,
            allow_first_subgoal_fallback=allow_first_subgoal_fallback
        )
    )
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


def _node_matches_expected_labels(node: Any, labels: List[str], domain: str) -> bool:
    if not node or not labels:
        return False
    blob = _canonicalize_label(_collect_node_text_for_match(node))
    if not blob:
        return False
    expanded = []
    for l in labels:
        expanded.extend(_expand_label_synonyms(l, domain))
    for e in expanded:
        if e and (e == blob or e in blob):
            return True
    return False


def _resolve_clickable_target_id(
    target_id: str,
    nodes: List[Any],
    excluded_ids: Optional[set[str]] = None
) -> Tuple[Optional[str], str]:
    """
    If label text is on a non-clickable child, climb parent chain to clickable ancestor.
    """
    if not target_id:
        return None, "missing_target_id"
    node_map = {getattr(n, "id", ""): n for n in nodes}
    seen = set()
    current_id = target_id
    chain: List[str] = []
    for _ in range(8):
        if not current_id or current_id in seen:
            break
        seen.add(current_id)
        chain.append(current_id)
        node = node_map.get(current_id)
        if not node:
            return None, "target_not_in_live_graph"
        if excluded_ids and current_id in excluded_ids:
            return None, "target_already_excluded"
        if getattr(node, "interactive", False) and _is_clickable_node(node):
            if len(chain) > 1:
                logger.info(f"ROUTER_SHADOW ancestor_resolve chain={' -> '.join(chain)}")
            return current_id, "ok"
        parent_id = getattr(node, "parent_id", None) or ""
        current_id = parent_id
    return None, "click_target_not_clickable"


def _resolve_clickable_by_label_context(
    raw_node_id: str,
    labels: List[str],
    domain: str,
    nodes: List[Any],
    excluded_ids: Optional[set[str]] = None,
) -> Tuple[Optional[str], str]:
    """
    Secondary resolver when parent-chain resolution fails:
    find a nearby clickable node whose text matches the explicit label.
    """
    if not raw_node_id or not labels:
        return None, "missing_context"
    node_map = {getattr(n, "id", ""): n for n in nodes}
    raw_node = node_map.get(raw_node_id)
    if not raw_node:
        return None, "raw_node_not_found"

    expected_variants: List[str] = []
    for label in labels:
        expected_variants.extend(_expand_label_synonyms(label, domain))
    expected_variants = [v for v in expected_variants if v]
    if not expected_variants:
        return None, "no_expected_variants"

    raw_parent = getattr(raw_node, "parent_id", "") or ""
    raw_zone = (getattr(raw_node, "zone", "") or "").lower()
    best_id: Optional[str] = None
    best_score = 0.0

    for n in nodes:
        nid = getattr(n, "id", "")
        if not nid or nid == raw_node_id:
            continue
        if excluded_ids and nid in excluded_ids:
            continue
        if not (getattr(n, "interactive", False) and _is_clickable_node(n)):
            continue
        blob = _canonicalize_label(_collect_node_text_for_match(n))
        if not blob:
            continue

        score = 0.0
        for v in expected_variants:
            if v == blob:
                score = max(score, 1.0)
            elif v in blob:
                score = max(score, 0.96)
            else:
                vt = set(v.split())
                bt = set(blob.split())
                overlap = len(vt & bt)
                if overlap > 0:
                    score = max(score, min(0.90, overlap / max(1, len(vt))))

        if score <= 0.0:
            continue

        if raw_parent and (getattr(n, "parent_id", "") or "") == raw_parent:
            score += 0.05
        if raw_zone and (getattr(n, "zone", "") or "").lower() == raw_zone:
            score += 0.02

        if score > best_score:
            best_score = score
            best_id = nid

    # Only allow strong label peers (exact/synonym/contains), not loose overlaps.
    if best_id and best_score >= 0.93:
        return best_id, "ok"
    return None, "no_clickable_label_peer"


def _hash_last_mile_sequence(seq: List[Dict[str, Any]]) -> str:
    import json
    import hashlib
    raw = json.dumps(seq, sort_keys=True, ensure_ascii=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _snapshot_node_state(node: Any) -> Dict[str, Any]:
    if not node:
        return {}
    return {
        "state": getattr(node, "state", "") or "",
        "value": str(getattr(node, "value", "") or ""),
        "aria_selected": str(getattr(node, "aria_selected", "") or ""),
        "aria_expanded": str(getattr(node, "aria_expanded", "") or ""),
        "text": (getattr(node, "text", "") or "")[:200],
    }


def _build_dom_signature(nodes: List[Any]) -> str:
    import hashlib
    rows: List[str] = []
    for n in nodes:
        if not getattr(n, "interactive", False):
            continue
        rows.append(
            "|".join(
                [
                    getattr(n, "id", "") or "",
                    getattr(n, "zone", "") or "",
                    getattr(n, "tag", "") or "",
                    (getattr(n, "text", "") or "")[:120],
                    getattr(n, "state", "") or "",
                    str(getattr(n, "aria_selected", "") or ""),
                    str(getattr(n, "aria_expanded", "") or ""),
                    str(getattr(n, "value", "") or ""),
                ]
            )
        )
    rows.sort()
    raw = "\n".join(rows)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _verify_pending_action_effect(
    mission: Any,
    nodes: List[Any],
    current_url: str,
    current_dom_signature: str,
) -> Tuple[bool, str]:
    pending = getattr(mission, "pending_action", None) or {}
    if not pending:
        return False, "no_pending_action"

    action_type = (pending.get("type") or "").lower()
    target_id = pending.get("target_id", "")
    prev_url = (getattr(mission, "last_url", "") or "").strip()
    prev_sig = (getattr(mission, "last_dom_signature", "") or "").strip()
    prev_snapshot = pending.get("target_snapshot") or {}

    if prev_url and current_url and prev_url != current_url:
        return True, "url_changed"
    if prev_sig and current_dom_signature and prev_sig != current_dom_signature:
        return True, "dom_signature_changed"

    node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
    if not node:
        return True, "target_disappeared"

    cur_snapshot = _snapshot_node_state(node)
    if prev_snapshot and cur_snapshot != prev_snapshot:
        return True, "target_state_changed"

    if action_type == "type_text":
        typed = (pending.get("text") or "").strip().lower()
        if typed:
            node_value = str(getattr(node, "value", "") or "").lower()
            if typed in node_value:
                return True, "input_contains_typed_text"

    return False, "no_observable_effect"


async def _record_and_stage_pending_action(
    *,
    mission_brain: Any,
    mission: Any,
    action_type: str,
    target_id: str,
    nodes: List[Any],
    current_url: str,
    dom_signature: str,
    text: str = "",
) -> None:
    target_node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
    if action_type == "type_text":
        await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text)
    else:
        await mission_brain.record_action(mission.mission_id, "click", target_id, True)

    pending_action = {
        "type": action_type,
        "target_id": target_id,
        "text": text,
        "target_snapshot": _snapshot_node_state(target_node),
        "created_at": time.time(),
    }
    await mission_brain.set_pending_action(
        mission.mission_id,
        pending_action,
        dom_signature,
        current_url,
    )
    logger.info(
        f"PENDING_ACTION_SET mission={mission.mission_id} action={action_type} "
        f"id={target_id}"
    )


async def _record_and_maybe_advance(
    *,
    mission_brain: Any,
    mission: Any,
    action_type: str,
    target_id: str,
    nodes: List[Any],
    current_url: str,
    dom_signature: str,
    text: str = "",
    verified_advance_active: bool = False,
    is_zero_shot: bool = False,
) -> int:
    """
    Unified action recorder:
    - Verified mode: stage pending action and keep current subgoal index.
    - Legacy mode: record+advance immediately and return next index.
    """
    if verified_advance_active:
        await _record_and_stage_pending_action(
            mission_brain=mission_brain,
            mission=mission,
            action_type=action_type,
            target_id=target_id,
            nodes=nodes,
            current_url=current_url,
            dom_signature=dom_signature,
            text=text,
        )
        return mission.current_subgoal_index

    if action_type == "type_text":
        await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text)
    else:
        await mission_brain.record_action(mission.mission_id, "click", target_id, True)
    await mission_brain.advance_subgoal(
        mission.mission_id,
        is_zero_shot=is_zero_shot,
    )
    _updated = await mission_brain._load_mission(mission.mission_id)
    return _updated.current_subgoal_index if _updated else mission.current_subgoal_index + 1


def _lexical_ground_candidate(
    query: str,
    schema: Any,
    nodes: List[Any],
    subgoal_mode: str,
    excluded_ids: set[str]
) -> Optional[Dict[str, Any]]:
    query_tokens = _tokenize(query)
    explicit_terms = _explicit_query_terms(query)
    target_zones = _extract_zone_targets(query)
    target_tokens = _tokenize(getattr(schema, "target_entity", ""))
    all_tokens = query_tokens | target_tokens | _DOMAIN_ALIASES
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

        text = (getattr(n, "text", "") or "")
        tokens = _tokenize(text) | _tokenize(getattr(n, "id", ""))
        overlap = len(tokens & all_tokens)
        if overlap == 0:
            continue

        explicit_overlap = len(tokens & explicit_terms)
        target_overlap = len(tokens & target_tokens)
        label_exact = any(
            (term and term in (text or "").lower())
            for term in explicit_terms
        )

        explicit_score = min(0.85, explicit_overlap / max(1, len(explicit_terms) or 1))
        target_score = min(0.10, 0.05 * target_overlap)
        overlap_score = explicit_score + target_score
        zone = (getattr(n, "zone", "") or "").lower()
        tag = (getattr(n, "tag", "") or "").lower()
        role = (getattr(n, "role", "") or "").lower()

        zone_boost = 0.0
        if subgoal_mode == "literal_click" and zone in {"nav", "sidebar"}:
            zone_boost = 0.15
        elif subgoal_mode == "literal_type" and zone in {"main", "nav"}:
            zone_boost = 0.08

        affordance_boost = 0.0
        if subgoal_mode == "literal_type" and (tag in _TYPE_TAGS or role in _TYPE_ROLES):
            affordance_boost = 0.2
        if subgoal_mode == "literal_click" and (tag in _CLICK_TAGS or role in _CLICK_ROLES):
            affordance_boost = 0.12

        text_penalty = 0.0
        if subgoal_mode == "literal_click" and _is_text_heavy(tag, text):
            text_penalty = 0.25

        zone_match = _zone_compatible_for_direct(target_zones, zone)
        zone_penalty = 0.0
        if target_zones and zone not in target_zones:
            zone_penalty = 0.35

        score = max(0.0, min(1.0, overlap_score + zone_boost + affordance_boost - text_penalty - zone_penalty))
        if score > best_score:
            best_score = score
            best = {
                "node": n,
                "score": score,
                "explicit_overlap": explicit_overlap,
                "label_exact": label_exact,
                "zone_match": zone_match,
                "explicit_terms": sorted(explicit_terms),
                "target_zones": sorted(target_zones),
            }

    if not best:
        return None
    return best


async def _run_tier3_fallback(
    *,
    app: Any,
    goal: str,
    query: str,
    nodes: List[Any],
    mission: Any,
    mission_brain: Any,
    schema: Any,
    is_zero_shot: bool,
    start_time: float,
    forced_reason: str = "",
    excluded_ids: Optional[set[str]] = None,
    expected_labels: Optional[List[str]] = None,
    expected_domain: str = "",
    current_url: str = "",
    dom_signature: str = "",
    verified_advance_active: bool = False,
) -> Optional[Dict[str, Any]]:
    logger.warning(
        f"   ⚠️ Tier 1+2 FAILED/BYPASSED ({forced_reason or 'low_confidence'}). "
        "Activating Tier 3: V1 Full-DOM Fallback..."
    )

    try:
        tara_tools = [
            {
                "type": "function",
                "function": {
                    "name": "click_element",
                    "description": "Click a specific product, link, dropdown, or button.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "draft_thought": {"type": "string", "description": "A 10-word concise logic draft analyzing the DOM before acting"},
                            "target_id": {"type": "string", "description": "The exact ID of the element from the DOM list"}
                        },
                        "required": ["draft_thought", "target_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "type_text",
                    "description": "Type keywords into a search bar or input field.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "draft_thought": {"type": "string", "description": "A 10-word concise logic draft analyzing the DOM before acting"},
                            "target_id": {"type": "string"},
                            "text_to_type": {"type": "string", "description": "Concise search keywords (e.g., 'nike shoe' not 'find me the costliest nike shoe')"}
                        },
                        "required": ["draft_thought", "target_id", "text_to_type"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "answer_user",
                    "description": "Stop navigation and talk to the user if the goal is visible on screen.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "draft_thought": {"type": "string", "description": "A 10-word explanation of why the goal is achieved"},
                            "final_answer": {"type": "string", "description": "The information to speak to the user"}
                        },
                        "required": ["draft_thought", "final_answer"]
                    }
                }
            }
        ]

        interactive_nodes = [
            n for n in nodes
            if n.interactive and (n.text or n.tag == "input" or n.role == "searchbox")
        ]
        if interactive_nodes:
            priority_nodes = [n for n in interactive_nodes if n.tag in ["input", "textarea"] or n.role == "searchbox"]
            other_nodes = [n for n in interactive_nodes if n not in priority_nodes]
            final_nodes = (priority_nodes + other_nodes)[:100]
            compressed_dom = "\n".join([
                f"[ID: {n.id}] tag={n.tag} zone={n.zone} text='{n.text[:60] if n.text else ''}'"
                for n in final_nodes
            ])
        else:
            compressed_dom = "No interactive elements found."

        system_prompt = f"""You are a high-velocity web agent.
GOAL: '{goal}'
SUBGOAL: '{query}'

AVAILABLE NODES:
{compressed_dom}

INSTRUCTIONS:
1. You MUST call a tool.
2. Fill out the 'draft_thought' parameter FIRST to logically deduce your action.
3. If the goal is visible on the screen, use the 'answer_user' tool. Do not click unnecessarily.
4. If you must act, verify the 'target_id' perfectly matches an ID in the AVAILABLE NODES list.
"""
        import httpx
        import json

        api_key = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_KEY")
        if not api_key and hasattr(app.state, "mind_reader") and hasattr(app.state.mind_reader, "llm"):
            api_key = getattr(app.state.mind_reader.llm._client, "api_key", getattr(app.state.mind_reader.llm, "api_key", None))

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "system", "content": system_prompt}],
                    "tools": tara_tools,
                    "tool_choice": "required",
                    "temperature": 0.0
                },
                timeout=10
            )

            if resp.status_code != 200:
                raise Exception(f"Groq API Error: {resp.status_code}")

            message = resp.json()["choices"][0]["message"]
            if DEBUG_TRACE_OUTPUTS:
                logger.info(f"TRACE_OUTPUT tier3_raw\n{json.dumps(message)}")
            if not message.get("tool_calls"):
                return None

            tool_call = message["tool_calls"][0]
            action_name = tool_call["function"]["name"]
            args = json.loads(tool_call["function"]["arguments"])

            logger.info(f"   🧠 Tier 3 Chain of Draft: {args.get('draft_thought')}")
            logger.info(f"   🚀 Tier 3 Tool Call: {action_name}")

            if action_name == "click_element":
                resolved_id, resolve_reason = _resolve_clickable_target_id(
                    args.get("target_id", ""), nodes, excluded_ids=excluded_ids
                )
                if resolved_id and resolved_id != args.get("target_id", ""):
                    logger.info(
                        f"   🧭 Tier 3 click target remap: {args.get('target_id')} -> {resolved_id}"
                    )
                    args["target_id"] = resolved_id
                ok, reason = _validate_action_target("click", args.get("target_id", ""), nodes, excluded_ids=excluded_ids)
                if not ok:
                    logger.warning(
                        f"   🚫 Tier 3 rejected ungrounded click target: {reason} "
                        f"(resolve={resolve_reason})"
                    )
                    return None
                if expected_labels:
                    _candidate = next((n for n in nodes if getattr(n, "id", "") == args.get("target_id", "")), None)
                    if not _node_matches_expected_labels(_candidate, expected_labels, expected_domain):
                        logger.warning("TIER3_REJECT reason=tier3_label_mismatch action=click")
                        return None
                _t3_node = next((n for n in nodes if n.id == args["target_id"]), None)
                _t3_label = (_t3_node.text[:40].strip() if _t3_node and _t3_node.text else "the element")
                _next_idx = await _record_and_maybe_advance(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="click",
                    target_id=args["target_id"],
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=dom_signature,
                    text="",
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                )
                return {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "click", "target_id": args["target_id"], "text": _t3_label},
                    "speech": f"Clicking on {_t3_label}...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_tier3",
                    "pending_verification": verified_advance_active,
                }

            if action_name == "type_text":
                _t3_text = args.get("text_to_type", "")
                ok, reason = _validate_action_target("type_text", args.get("target_id", ""), nodes, excluded_ids=excluded_ids)
                if not ok:
                    logger.warning(f"   🚫 Tier 3 rejected ungrounded type target: {reason}")
                    fallback_type_node = _find_best_type_target(nodes, query, excluded_ids or set())
                    if fallback_type_node:
                        logger.info(
                            f"   🧭 Tier 3 type fallback: using grounded input {fallback_type_node.id} "
                            f"after rejection={reason}"
                        )
                        _next_idx = await _record_and_maybe_advance(
                            mission_brain=mission_brain,
                            mission=mission,
                            action_type="type_text",
                            target_id=fallback_type_node.id,
                            nodes=nodes,
                            current_url=current_url,
                            dom_signature=dom_signature,
                            text=_t3_text or _extract_type_text(query, schema.target_entity),
                            verified_advance_active=verified_advance_active,
                            is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                        )
                        return {
                            "success": True,
                            "blocked": False,
                            "action": {
                                "type": "type_text",
                                "target_id": fallback_type_node.id,
                                "text": _t3_text or _extract_type_text(query, schema.target_entity),
                                "press_enter": True
                            },
                            "speech": f"Typing '{_t3_text or _extract_type_text(query, schema.target_entity)}' and searching...",
                            "mission_id": mission.mission_id,
                            "subgoal_index": _next_idx,
                            "confidence": "medium",
                            "timing_ms": int((time.time() - start_time) * 1000),
                            "pipeline": "ultimate_tara_tier3",
                            "pending_verification": verified_advance_active,
                        }
                    return None
                if expected_labels:
                    _candidate = next((n for n in nodes if getattr(n, "id", "") == args.get("target_id", "")), None)
                    if not _node_matches_expected_labels(_candidate, expected_labels, expected_domain):
                        logger.warning("TIER3_REJECT reason=tier3_label_mismatch action=type_text")
                        return None
                logger.info(f"   🚀 V1 Fallback Success: TYPE '{_t3_text}' into {args['target_id']}")
                _next_idx = await _record_and_maybe_advance(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="type_text",
                    target_id=args["target_id"],
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=dom_signature,
                    text=_t3_text,
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                )
                return {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "type_text",
                        "target_id": args["target_id"],
                        "text": _t3_text,
                        "press_enter": True
                    },
                    "speech": f"Typing '{_t3_text}' and searching...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_tier3",
                    "pending_verification": verified_advance_active,
                }

            if action_name == "answer_user":
                logger.info("   🧠 Tier 3: LLM says goal is already visible — no click needed.")
                await mission_brain.advance_subgoal(mission.mission_id, is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot)
                return {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "answer", "speech": args.get("final_answer", "")},
                    "complete": True,
                    "pipeline": "ultimate_tara_tier3",
                    "confidence": 0.80
                }

    except Exception as e:
        logger.error(f"   ❌ Tier 3 V1 Fallback failed: {e}")

    return None


async def _check_if_arrived(app, session_id: str, goal: str, current_url: str, nodes: List[Any], schema: Any) -> tuple[bool, str]:
    """
    Robust LLM-powered Location Guard.
    Filters the DOM strictly for images or substantial text in the main zone
    to prevent false positives from search dropdowns or navigation menus.
    """
    if not app or not hasattr(app.state, 'mind_reader') or not hasattr(app.state.mind_reader, 'llm'):
        return False, "No LLM available for arrival check."

    # 1. Filter DOM for Content Nodes Only
    content_nodes = [
        n for n in nodes
        if n.tag == 'img' or (n.text and len(n.text.strip()) > 5 and n.zone == 'main')
    ]
    
    if not content_nodes:
        return False, "No substantive content nodes found."

    compressed_dom = "\n".join([
        f"[tag={n.tag}] text='{n.text[:100]}'" if n.text else f"[tag={n.tag}] image"
        for n in content_nodes[:50]  # Cap at 50 to save tokens, usually plenty for results
    ])

    # 2. LLM Prompt
    prompt = f"""You are determining if a web agent has achieved its ultimate goal.
USER GOAL: '{goal}'
TARGET ENTITY: '{schema.target_entity}'
CURRENT URL: '{current_url}'

VISIBLE CONTENT ELEMENTS (Images & Main Text only):
{compressed_dom}

INSTRUCTIONS:
1. Analyze the URL and Content Elements.
2. Has the user's goal been achieved? For a search/find goal, this means there are MULTIPLE distinct content results (e.g., product cards, video thumbnails, or a detailed profile) matching the goal.
3. CRITICAL: A single search dropdown suggestion matching the name is NOT arrival. You must see actual content items on the page.
4. Reply in strict JSON: {{"arrived": true/false, "reason": "brief 1-sentence explanation"}}"""

    try:
        import json, re
        response = await app.state.mind_reader.llm.generate(
            prompt,
            model="llama-3.1-8b-instant",
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        match = re.search(r'\{.*\}', response, re.DOTALL)
        decision = json.loads(match.group()) if match else json.loads(response)
        
        arrived = decision.get("arrived", False)
        reason = decision.get("reason", "No reason provided")
        
        if arrived:
            logger.info(f"🎯 LLM DOM CHECK (ARRIVED): {reason}")
        else:
            logger.debug(f"🔍 LLM DOM CHECK (NOT ARRIVED): {reason}")
            
        return arrived, reason
    except Exception as e:
        logger.error(f"LLM DOM check failed: {e}")
        return False, f"Error: {e}"


async def _validate_and_end_mission(
    schema, nodes, mission, mission_brain, app, start_time
) -> Optional[Dict[str, Any]]:
    """
    Validate that the mission is truly complete, then generate a closing dialogue.
    Returns a complete response dict if mission is done, or None if not actually complete.
    """
    from prompts.mission_end import build_validate_prompt, build_mission_end_prompt
    import json, re

    try:
        # Step 1: Validate — is the goal actually achieved?
        validate_prompt = build_validate_prompt(schema, nodes, mission)
        if app and hasattr(app.state, 'mind_reader') and hasattr(app.state.mind_reader, 'llm'):
            llm = app.state.mind_reader.llm
            raw = await llm.generate(
                validate_prompt,
                model="llama-3.1-8b-instant",
                temperature=0.1,
                response_format={"type": "json_object"}
            )
        else:
            # Can't validate without LLM — assume complete
            raw = '{"is_complete": true, "confidence": 0.7, "evidence": "no LLM available", "what_was_done": "completed task"}'

        match = re.search(r'\{.*\}', raw, re.DOTALL)
        validation = json.loads(match.group()) if match else json.loads(raw)

        logger.info(
            f"🏁 Mission Validator: complete={validation.get('is_complete')}, "
            f"confidence={validation.get('confidence')}, "
            f"evidence={validation.get('evidence', '')[:100]}"
        )

        if not validation.get("is_complete"):
            return None  # Not done yet — caller will continue the mission

        what_was_done = validation.get("what_was_done", "completed the task")

        # Step 2: Generate closing dialogue
        end_prompt = build_mission_end_prompt(schema, nodes, what_was_done, mission)
        if app and hasattr(app.state, 'mind_reader') and hasattr(app.state.mind_reader, 'llm'):
            end_raw = await llm.generate(
                end_prompt,
                model="llama-3.1-8b-instant",
                temperature=0.5,
                response_format={"type": "json_object"}
            )
        else:
            end_raw = f'{{"speech": "Done! I found {schema.target_entity} for you. What would you like to do next?"}}'

        end_match = re.search(r'\{.*\}', end_raw, re.DOTALL)
        end_data = json.loads(end_match.group()) if end_match else json.loads(end_raw)

        speech = end_data.get("speech", f"All done with {schema.target_entity}! What's next?")
        logger.info(f"🎤 Mission End Speech: {speech}")

        # Step 3: Mark mission as completed
        if mission:
            mission.status = "completed"
            mission.phase = "done"
            await mission_brain._save_mission(mission)

        return {
            "success": True,
            "blocked": False,
            "action": {
                "type": "answer",
                "speech": speech
            },
            "complete": True,
            "pipeline": "ultimate_tara",
            "confidence": validation.get("confidence", 0.9),
            "timing_ms": int((time.time() - start_time) * 1000)
        }

    except Exception as e:
        logger.error(f"❌ Mission validation/end failed: {e}")
        # Fallback: mark complete with generic speech
        if mission:
            mission.status = "paused"
            await mission_brain._save_mission(mission)
        return {
            "success": True,
            "blocked": False,
            "action": {
                "type": "answer",
                "speech": f"I've found {schema.target_entity} for you! What would you like to do next?"
            },
            "complete": True,
            "pipeline": "ultimate_tara",
            "confidence": 0.7,
            "timing_ms": int((time.time() - start_time) * 1000)
        }


async def _run_read_only_terminal(
    *,
    session_id: str,
    app: Any,
    goal: str,
    query: str,
    schema: Any,
    nodes: List[Any],
    mission: Any,
    mission_brain: Any,
    semantic_detective: Any,
    hive_hints: Optional[List[Any]],
    excluded_ids: set[str],
    start_time: float,
    is_zero_shot: bool,
    current_url: str = "",
    dom_signature: str = "",
    verified_advance_active: bool = False,
) -> Dict[str, Any]:
    """
    Read-only terminal handler.
    For cognitive_read subgoals, never click/type fallback actions.
    """
    # Prefer substantive main-content text. Keep nav/sidebar as secondary evidence.
    main_text_nodes = [
        n for n in nodes
        if getattr(n, "text", None) and len((n.text or "").strip()) >= 4 and getattr(n, "zone", "") == "main"
    ]
    aux_text_nodes = [
        n for n in nodes
        if getattr(n, "text", None) and len((n.text or "").strip()) >= 4 and getattr(n, "zone", "") in {"nav", "sidebar"}
    ]
    source_nodes = (main_text_nodes[:80] + aux_text_nodes[:20])[:100]
    visible_text = "\n".join((n.text or "")[:120] for n in source_nodes if n.text)

    if not visible_text.strip():
        return {
            "success": False,
            "blocked": True,
            "reason": "I could not find readable usage content on screen yet. Please wait for the page to render.",
            "action": {"type": "wait"},
            "pipeline": "ultimate_tara_read_only"
        }

    # Ask LLM to extract, but force explicit uncertainty when evidence is missing.
    speech = ""
    try:
        if hasattr(app.state, 'mind_reader') and hasattr(app.state.mind_reader, 'llm'):
            import json
            import re
            prompt = (
                f"You are extracting an answer from visible web content.\n"
                f"User goal: {goal}\n"
                f"Target: {schema.target_entity}\n\n"
                f"Visible content:\n{visible_text[:5000]}\n\n"
                "Return strict JSON with keys:\n"
                "{\"found\": true/false, \"answer\": \"short answer\", \"evidence\": \"short evidence\"}\n"
                "Rules:\n"
                "1) If usage numbers/timeframe are not visible, set found=false.\n"
                "2) Do not invent values."
            )
            raw = await app.state.mind_reader.llm.generate(
                prompt,
                model="llama-3.1-8b-instant",
                temperature=0.0,
                response_format={"type": "json_object"}
            )
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(match.group()) if match else json.loads(raw)
            if data.get("found"):
                speech = data.get("answer", "").strip()
                if not speech:
                    logger.info("🧠 READ MODE: model returned found=true but empty answer, treating as not-found.")
            else:
                logger.info("🧠 READ MODE: no clear stats yet, attempting constrained semantic assist.")
    except Exception as e:
        logger.warning(f"Read-only extraction failed, using safe fallback: {e}")

    # If we couldn't answer, try ONE controlled detective action focused on nav/sidebar targets.
    if not speech:
        usage_terms = {"usage", "token", "tokens", "activity", "analytics", "billing", "dashboard", "model", "models"}
        goal_terms = _tokenize(f"{goal} {schema.target_entity} {query}")
        if "usage" in goal_terms or "token" in goal_terms:
            assist_query = "Click Usage in sidebar"
        elif "activity" in goal_terms:
            assist_query = "Click Activity in sidebar"
        elif "dashboard" in goal_terms:
            assist_query = "Click Dashboard in sidebar"
        else:
            assist_query = "Click Usage or Dashboard in sidebar"

        try:
            detective_report = await semantic_detective.investigate(
                session_id=session_id,
                query=assist_query,
                hive_hints=hive_hints or [],
                action_intent=ActionIntent.NAVIGATION,
                excluded_ids=list(excluded_ids),
                subgoal_mode="literal_click"
            )
            cand = detective_report.best_match
            if cand:
                text = (cand.text or "").lower()
                zone = (cand.zone or "").lower()
                tag = (cand.tag or "").lower()
                score = getattr(cand, "final_score", cand.hybrid_score)
                lexical = getattr(cand, "lexical_score", 0.0)
                overlap = any(t in text for t in usage_terms) or bool(goal_terms & _tokenize(text))
                zone_ok = zone in {"sidebar", "nav", "header"} or (zone == "main" and overlap)
                tag_ok = tag in {"a", "button", "summary"}
                strong_match = ("focus_exact_match" in (cand.reasons or [])) or lexical >= 0.55
                if tag_ok and zone_ok and overlap and strong_match and score >= 0.62 and cand.node_id not in excluded_ids:
                    logger.info(
                        f"🧠 READ MODE semantic assist: click '{cand.text[:40]}' "
                        f"(zone={cand.zone}, score={score:.2f}, lex={lexical:.2f})"
                    )
                    _next_idx = await _record_and_maybe_advance(
                        mission_brain=mission_brain,
                        mission=mission,
                        action_type="click",
                        target_id=cand.node_id,
                        nodes=nodes,
                        current_url=current_url,
                        dom_signature=dom_signature,
                        verified_advance_active=verified_advance_active,
                        is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                    )
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "click", "target_id": cand.node_id, "text": cand.text[:60] if cand.text else ""},
                        "speech": f"Opening {(cand.text[:40].strip() if cand.text else 'the relevant section')}...",
                        "mission_id": mission.mission_id if mission else None,
                        "subgoal_index": _next_idx if mission else 0,
                        "pipeline": "ultimate_tara_read_assist",
                        "confidence": "medium",
                        "pending_verification": verified_advance_active,
                        "timing_ms": int((time.time() - start_time) * 1000)
                    }
                logger.info(
                    f"🧠 READ MODE semantic assist rejected: "
                    f"text='{cand.text[:40] if cand and cand.text else ''}' zone={zone} score={score:.2f}"
                )
        except Exception as e:
            logger.warning(f"READ MODE semantic assist failed: {e}")

        return {
            "success": False,
            "blocked": True,
            "reason": "Usage stats are not clearly visible yet, and no safe closest navigation target was found.",
            "action": {"type": "wait"},
            "pipeline": "ultimate_tara_read_only"
        }

    if not speech:
        # Safe fallback if extraction model unavailable.
        speech = "I am on the usage area, but I cannot confirm the exact numbers yet from visible content."

    if mission:
        await mission_brain.advance_subgoal(
            mission.mission_id,
            is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot
        )
        _updated = await mission_brain._load_mission(mission.mission_id)
        _complete = bool(_updated and _updated.status == "completed")
    else:
        _complete = True

    return {
        "success": True,
        "blocked": False,
        "action": {"type": "answer", "speech": speech, "text": speech},
        "speech": speech,
        "complete": _complete,
        "pipeline": "ultimate_tara_read_only",
        "confidence": 0.85,
        "timing_ms": int((time.time() - start_time) * 1000)
    }


async def ultimate_plan_next_step(
    app,
    session_id: str,
    goal: str,
    current_url: str,
    step_number: int = 0,
    action_history: Optional[list] = None,
    previous_goal: Optional[str] = None,  # 🔗 Previous mission goal for follow-up context resolution
    mission_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ultimate TARA pipeline for planning next step.
    
    Flow:
    1. Mind Reader → TacticalSchema
    2. Get Map Hints from existing endpoint (GPS navigation)
    3. Hive Interface → Strategy + Visual Hints
    4. Mission Brain → Create Mission + Sub-goals
    5. Live Graph → Get DOM nodes
    6. Semantic Detective → Score candidates
    7. Mission Brain → Audit action (constraint check)
    8. Return approved action
    """
    start_time = time.time()
    
    # Get Ultimate TARA modules from app state
    mind_reader = app.state.mind_reader
    hive_interface = app.state.hive_interface
    mission_brain = app.state.mission_brain
    semantic_detective = app.state.semantic_detective
    live_graph = app.state.live_graph
    
    if not all([mind_reader, hive_interface, mission_brain, semantic_detective, live_graph]):
        logger.warning("⚠️ Ultimate TARA modules not fully initialized, falling back to legacy")
        return None
    
    try:
        # ── Build excluded IDs from action_history (anti-loop) ──
        # Only exclude CLICK targets — type targets should stay available
        # (you might need to type different text into the same search input)
        excluded_ids = set()
        if action_history:
            for entry in action_history:
                if isinstance(entry, dict):
                    if entry.get("action") == "click" and entry.get("target_id"):
                        excluded_ids.add(entry["target_id"].strip())
                elif isinstance(entry, str) and ":" in entry:
                    action_type_str, target = entry.split(":", 1)
                    # Only exclude clicks, not types
                    if action_type_str.strip().lower() == "click":
                        excluded_ids.add(target.strip())
        logger.info(f"📋 action_history: {len(action_history or [])} entries, {len(excluded_ids)} excluded click IDs")

        # ═══════════════════════════════════════════════════════════
        # 🛡️ FRONTEND AMNESIA GUARD — Backend is single source of truth
        # The frontend's step_number resets to 0 on SPA reload.
        # Check Redis mission state FIRST to detect stale step counts.
        # ═══════════════════════════════════════════════════════════
        existing_mission = await mission_brain._load_session_mission(session_id)
        if not existing_mission and mission_id:
            by_id = await mission_brain._load_mission(mission_id)
            if by_id and by_id.status in ("in_progress", "paused", "completed"):
                existing_mission = by_id
                if by_id.session_id != session_id:
                    old_session_id = by_id.session_id
                    by_id.session_id = session_id
                    await mission_brain._save_mission(by_id)
                    logger.info(
                        f"🧷 Mission rebind: {by_id.mission_id} moved from session "
                        f"{old_session_id} → {session_id}"
                    )
        backend_step = 0
        if existing_mission and existing_mission.status in ("in_progress", "paused"):
            backend_step = len(existing_mission.action_history)
            if step_number == 0 and backend_step > 0:
                logger.warning(
                    f"🛡️ FRONTEND AMNESIA GUARD: Frontend sent step=0 but Redis has "
                    f"{backend_step} actions on mission {existing_mission.mission_id}. "
                    f"Trusting backend state."
                )
                step_number = backend_step
        elif existing_mission and existing_mission.status == "completed":
            # With Last-Mile enabled, a strategy-completed mission may still need terminal execution.
            _existing_phase = getattr(existing_mission, "phase", "strategy")
            _existing_domain = ""
            if existing_mission.schema:
                _existing_domain = getattr(existing_mission.schema, "domain", "") or ""
            if not _existing_domain and current_url and "://" in current_url:
                try:
                    from urllib.parse import urlparse
                    _existing_domain = (urlparse(current_url).netloc or "").replace("www.", "")
                except Exception:
                    _existing_domain = ""
            if _is_last_mile_enabled_for_domain(_existing_domain) and _existing_phase != "done":
                logger.info(
                    f"LAST_MILE_ENTER mission={existing_mission.mission_id} "
                    f"status=completed phase={_existing_phase} source=dedup_guard"
                )
            else:
                logger.info(f"🏁 Mission {existing_mission.mission_id} already complete. Halting.")
                return {
                    "success": True, "blocked": False, "complete": True,
                    "action": {"type": "answer", "speech": "I've already completed that task. What would you like to do next?"},
                    "speech": "I've already completed that task. What would you like to do next?",
                    "pipeline": "ultimate_tara_dedup_guard"
                }

        effective_step = max(step_number, backend_step)

        # Provisional context exists before Mind Reader so pre-intent telemetry can be retained.
        if not hasattr(app.state, "_pre_mission_context"):
            app.state._pre_mission_context = {}
        app.state._pre_mission_context[session_id] = {
            "goal": goal,
            "url": current_url,
            "step": effective_step,
            "ts": time.time(),
        }

        # Step 1: Parse intent with Mind Reader (CACHED after first step)
        # On step 0: full LLM parse + store in app-level cache
        # On step > 0: reuse cached schema (same goal, same intent)
        _cache = getattr(app.state, '_schema_cache', {})
        if not hasattr(app.state, '_schema_cache'):
            app.state._schema_cache = _cache

        _cached_entry = _cache.get(session_id)
        if effective_step > 0 and _cached_entry and _cached_entry.get('goal') == goal:
            schema = _cached_entry['schema']
            logger.info(f"🧠 Step 1: CACHED → {schema.action.value} on '{schema.target_entity}' (0 LLM calls)")
        else:
            logger.info("👁️ Step 0: Live Graph getting DOM nodes for Mind Reader...")
            nodes = await live_graph.get_visible_nodes(session_id)
            logger.info("🧠 Step 1: Mind Reader parsing intent...")
            schema = await mind_reader.translate(
                user_input=goal,
                current_url=current_url,
                previous_goal=previous_goal,  # 🔗 Enables: "now change to blue" → "blue shoes"
                nodes=nodes
            )
            logger.info(f"   ✅ Intent: {schema.action.value} on '{schema.target_entity}'")
            _cache[session_id] = {'schema': schema, 'goal': goal}

        # Prefer actual current host as the primary Hive retrieval domain.
        if current_url and "://" in current_url:
            try:
                from urllib.parse import urlparse
                current_host = (urlparse(current_url).netloc or "").replace("www.", "")
            except Exception:
                current_host = ""
            if current_host and getattr(schema, "domain", "") != current_host:
                logger.info(
                    f"DOMAIN_CONTEXT_OVERRIDE schema_domain={getattr(schema, 'domain', '') or 'none'} "
                    f"current_host={current_host}"
                )
                # TacticalSchema is frozen; create a replaced instance instead of mutating.
                try:
                    from dataclasses import replace
                    schema = replace(schema, domain=current_host)
                except Exception as _e:
                    logger.warning(f"DOMAIN_CONTEXT_OVERRIDE failed to replace schema: {_e}")

        schema_domain = getattr(schema, "domain", "") or ""
        if not schema_domain and current_url and "://" in current_url:
            try:
                from urllib.parse import urlparse
                schema_domain = (urlparse(current_url).netloc or "").replace("www.", "")
            except Exception:
                schema_domain = ""
        keyword_direct_v3_active = _is_v3_feature_enabled(schema_domain, ENABLE_KEYWORD_DIRECT_V3)
        subgoal_hint_query_active = _is_v3_feature_enabled(schema_domain, ENABLE_SUBGOAL_HINT_QUERY)
        verified_advance_active = _is_v3_feature_enabled(schema_domain, ENABLE_VERIFIED_ADVANCE)
        logger.info(
            f"V3_FEATURES domain={schema_domain or 'unknown'} "
            f"keyword_direct={keyword_direct_v3_active} "
            f"subgoal_hints={subgoal_hint_query_active} "
            f"verified_advance={verified_advance_active}"
        )
        last_mile_enabled = _is_last_mile_enabled_for_domain(schema_domain)

        # Step 2: GPS hints DISABLED — Hive visual_hints serve the same purpose.
        # Legacy _get_navigation_hints() added redundant Qdrant queries.
        hints = ""
        logger.debug("⏩ Step 2: GPS hints disabled (Hive visual_hints used instead)")

        # Step 3: Retrieve strategy + hints from Hive (SKIP on step > 0 in zero-shot)
        # Hive only has value on the FIRST call — subsequent steps always return empty.
        domain_known_for_hive = True
        if effective_step == 0:
            domain_known_for_hive = await hive_interface.is_domain_indexed(getattr(schema, "domain", "") or "")
            if domain_known_for_hive:
                logger.info("🧠 Step 3: Hive Interface retrieving strategy...")
                hive_response = await hive_interface.retrieve(schema)
                logger.info(f"   ✅ Strategy: {bool(hive_response.strategy)}, Hints: {len(hive_response.visual_hints)}")
            else:
                logger.info(
                    f"HIVE_BYPASS_UNKNOWN_DOMAIN domain={getattr(schema, 'domain', 'unknown')} "
                    "reason=domain_not_indexed -> zero_shot_local"
                )
                hive_response = HiveResponse(
                    strategy=None,
                    visual_hints=[],
                    cached=False,
                    query_time_ms=0,
                    strategy_score=0.0,
                    strategy_query_used="",
                    strategy_threshold_used=0.0,
                    strategy_accepted=False,
                )
            # Cache for later steps
            _cache_hive = getattr(app.state, '_hive_cache', {})
            if not hasattr(app.state, '_hive_cache'):
                app.state._hive_cache = _cache_hive
            _cache_hive[session_id] = hive_response
        else:
            _cache_hive = getattr(app.state, '_hive_cache', {})
            hive_response = _cache_hive.get(session_id)
            if not hive_response:
                domain_known_for_hive = await hive_interface.is_domain_indexed(getattr(schema, "domain", "") or "")
                if domain_known_for_hive:
                    hive_response = await hive_interface.retrieve(schema)
                else:
                    hive_response = HiveResponse(
                        strategy=None,
                        visual_hints=[],
                        cached=False,
                        query_time_ms=0,
                        strategy_score=0.0,
                        strategy_query_used="",
                        strategy_threshold_used=0.0,
                        strategy_accepted=False,
                    )
            logger.debug(f"⏩ Step 3: Using cached Hive response")

        # ═══════════════════════════════════════════════════════════
        # 🎯 LOCATION GUARD (deferred):
        # Compute eligibility early, execute only after mission state is known.
        # This prevents mid-plan premature completion.
        # ═══════════════════════════════════════════════════════════
        location_guard_candidate = False
        if effective_step >= 2:
            last_action_was_type = False
            if action_history:
                last_entry = action_history[-1]
                if isinstance(last_entry, str) and last_entry.startswith("type:"):
                    last_action_was_type = True
            if last_action_was_type:
                logger.info("⏩ LOCATION GUARD Skipped: last action was a search (type). Waiting for results to render.")
            else:
                location_guard_candidate = True

        # Step 3b: Domain Jump Decision Gate (Co-Domain Hive Jump)
        import re as _re
        is_zero_shot = False
        _cache_hive = getattr(app.state, '_hive_cache', {})

        # 🛡️ THE FIX: Check for strategy, not visual hints!
        if not hive_response.strategy:
            if not domain_known_for_hive:
                if schema.first_subgoal:
                    logger.info("🧭 ZERO-SHOT MODE: Hive bypassed (unknown domain), using Mind Reader first_subgoal.")
                else:
                    logger.info("🧭 ZERO-SHOT MODE: Hive bypassed (unknown domain), no first_subgoal.")
                is_zero_shot = True
                cross_response = None
            else:
                # ── A) Explicit jump detection (word-boundary safe) ──────────────
                CROSS_DOMAIN_TRIGGERS = [
                    r"\bgo to\b", r"\bopen\b", r"\bnavigate to\b",
                    r"\btake me to\b", r"\bswitch to\b"
                ]
                is_explicit_jump = any(
                    _re.search(t, goal.lower()) for t in CROSS_DOMAIN_TRIGGERS
                )

                # ── B) Check Hive for co-domain knowledge ─────────────────
                # 🚀 ARCHITECTURE UPGRADE: Mid-Mission Bypass
                # Skip expensive Qdrant cross-domain searches if we are already navigating, 
                # UNLESS the user explicitly commanded a domain jump.
                is_mid_mission = bool(action_history) or effective_step > 0

                cross_response = None
                if not is_mid_mission or is_explicit_jump:
                    cross_response = await hive_interface.retrieve_cross_domain(schema)
                else:
                    logger.info("⏩ Mid-mission detected. Skipping redundant cross-domain search.")

                bridge_domain = (
                    getattr(cross_response, 'cross_domain_target', None)
                    if cross_response else None
                )

                # 🛡️ Garbage filter
                _GARBAGE_DOMAINS = {"all", "none", "null", "", "any"}
                if bridge_domain and bridge_domain.lower().strip() in _GARBAGE_DOMAINS:
                    bridge_domain = None

                # ── Co-Domain check: same root TLD? ──────────────────────
                is_co_domain_jump = False
                if bridge_domain:
                    def _root(d: str) -> str:
                        parts = d.replace("https://", "").replace("http://", "").rstrip("/").split(".")
                        return ".".join(parts[-2:]) if len(parts) >= 2 else d

                    current_root = _root(schema.domain)
                    target_root  = _root(bridge_domain)
                    is_co_domain_jump = (current_root == target_root)

                # Promote cross-domain strategy only when jump is explicit/co-domain.
                if (
                    cross_response
                    and (cross_response.strategy or cross_response.visual_hints)
                    and bridge_domain
                    and (is_explicit_jump or is_co_domain_jump)
                ):
                    hive_response = cross_response
                    _cache_hive[session_id] = cross_response
                    app.state._hive_cache = _cache_hive
                    logger.info(
                        f"🧠 Cross-domain retrieval promoted: strategy={bool(cross_response.strategy)} "
                        f"hints={len(cross_response.visual_hints)} target={bridge_domain}"
                    )
                elif cross_response and (cross_response.strategy or cross_response.visual_hints):
                    logger.info(
                        f"CROSS_DOMAIN_REJECT reason=not_explicit_not_codomain target={bridge_domain} "
                        f"explicit={is_explicit_jump} co_domain={is_co_domain_jump}"
                    )

                # ── Decision Gate ────────────────────────────────────────
                if bridge_domain and (is_explicit_jump or is_co_domain_jump):
                    # If already on the bridge domain, continue with detective path.
                    current_host = schema.domain or ""
                    if current_url and "://" in current_url:
                        try:
                            from urllib.parse import urlparse
                            current_host = (urlparse(current_url).netloc or current_host).replace("www.", "")
                        except Exception:
                            pass
                    if current_host == bridge_domain:
                        logger.info(
                            f"🌐 Cross-domain bridge target '{bridge_domain}' equals current host; "
                            "continuing with semantic detective."
                        )
                    else:
                        logger.info(f"🌐 Cross-Domain Bridge APPROVED: routing to '{bridge_domain}'")
                        return {
                            "success": True,
                            "blocked": False,
                            "action": {
                                "type": "cross_domain_navigate",
                                "target_domain": bridge_domain,
                                "target_entity": schema.target_entity,
                                "hints": (
                                    [h.__dict__ for h in cross_response.visual_hints]
                                    if hasattr(cross_response, 'visual_hints') else []
                                ),
                                "speech": f"I know where to find '{schema.target_entity}' on {bridge_domain}. Taking you there."
                            },
                            "pipeline": "ultimate_tara_cross_domain",
                            "confidence": 0.90
                        }
                else:
                    # 🛡️ DOMAIN LOCK: Stay local.
                    if schema.first_subgoal:
                        logger.info(f"🧭 ZERO-SHOT MODE: Mind Reader provided first_subgoal → fast path (no ReAct needed)")
                    else:
                        logger.info(f"🧭 ZERO-SHOT MODE: No first_subgoal → will use ReAct LLM Router.")
                    is_zero_shot = True

        # Step 4: Get DOM from Live Graph (Moved up for zero-shot!)
        # (Nodes are now fetched before Step 1 to feed the generic Mind Reader)
        if 'nodes' not in locals():
            nodes = await live_graph.get_visible_nodes(session_id)
            logger.info(f"   ✅ DOM nodes: {len(nodes)}")

        # 🛡️ THE PHANTOM FIRE FIX: Prevent acting on an empty page transition
        if len(nodes) == 0:
            logger.warning("   🚫 DOM is empty! Frontend fired prematurely during navigation. Forcing a wait.")
            return {
                "success": False,
                "blocked": True,
                "reason": "Waiting for the page to finish loading...",
                "action": {"type": "wait"}
            }
        current_dom_signature = _build_dom_signature(nodes)
        excluded_ids = _drop_reclick_safe_exclusions(excluded_ids, nodes)

        # ═══════════════════════════════════════════════════════════
        # 🎯 FAST DOM GOAL CHECK — Runs BEFORE mission creation!
        # If the goal is already visible on screen, skip everything.
        # No mission, no ReAct, no detective, no Tier 3.
        # ═══════════════════════════════════════════════════════════
        if is_zero_shot and schema.action in (
            ActionIntent.EXTRACTION, ActionIntent.SEARCH, ActionIntent.NAVIGATION
        ):
            # Count DISTINCT content elements containing ALL goal words.
            # A search dropdown showing "nicole aniston" once ≠ goal achieved.
            # We need 5+ separate elements with the goal text to confirm
            # the page actually has content about the goal (not just a mention).
            goal_lower = schema.target_entity.lower()
            _noise = {'the', 'and', 'for', 'then', 'show', 'find', 'get', 'see', 'with', 'from', 'this', 'that', 'videos', 'pics', 'images', 'photos'}
            goal_words = [w for w in goal_lower.split() if len(w) > 2 and w not in _noise]

            # Only check content elements (skip inputs, search boxes, nav items)
            content_nodes = [
                n for n in nodes
                if n.text and len(n.text) > 5
                and n.tag not in ('input', 'select', 'textarea')
                and n.zone != 'nav'
            ]

            # Count elements where ALL goal words appear
            matching_elements = [
                n for n in content_nodes
                if all(w in n.text.lower() for w in goal_words)
            ]
            elem_count = len(matching_elements)

            logger.info(f"🎯 DOM CHECK: '{schema.target_entity}' found in {elem_count} content elements (need 5+)")

            if elem_count >= 5:
                logger.info(
                    f"🎯 FAST DOM CHECK: Goal '{schema.target_entity}' confirmed in {elem_count} elements. "
                    f"Completing immediately."
                )
                mission_for_fast_check = existing_mission
                if not mission_for_fast_check:
                    mission_for_fast_check = await mission_brain._load_session_mission(session_id)
                if not mission_for_fast_check and mission_id:
                    mission_for_fast_check = await mission_brain._load_mission(mission_id)
                end_result = await _validate_and_end_mission(
                    schema, nodes, mission_for_fast_check, mission_brain, app, start_time
                )
                if end_result:
                    return end_result
                logger.info("🎯 Fast DOM Check: validator said not done, continuing...")

        # Step 5: Get or resume mission (persists across requests)
        # Use existing_mission from Amnesia Guard if available, else frontend's mission_id
        _effective_mission_id = (existing_mission.mission_id if existing_mission else None) or mission_id
        mission = await mission_brain.get_or_create_mission(
            session_id=session_id,
            schema=schema,
            strategy=hive_response.strategy,
            nodes=nodes,
            app=app,
            zero_shot_mode=is_zero_shot,
            mission_id=_effective_mission_id
        )
        if getattr(hive_response, "strategy_accepted", False) and not getattr(mission, "strategy_locked", False):
            mission.strategy_locked = True
            mission.strategy_source = "hive_sequence"
            await mission_brain._save_mission(mission)
            logger.info("PLAN_LOCK strategy_locked=true source=hive_sequence")
        
        # 🆕 Zero-shot re-plan only when there are no actionable subgoals and we're not
        # about to hand off to Last-Mile in this turn.
        subgoal_count = len(mission.subgoals or [])
        strategy_exhausted = mission.current_subgoal_index >= subgoal_count
        should_consider_replan = bool(getattr(schema, "zero_shot_mode", False) or is_zero_shot)
        should_skip_replan_for_last_mile = bool(
            last_mile_enabled
            and strategy_exhausted
            and subgoal_count > 0
        )
        should_zero_shot_replan = bool(
            should_consider_replan
            and (subgoal_count == 0 or strategy_exhausted)
            and not should_skip_replan_for_last_mile
        )
        if should_skip_replan_for_last_mile:
            logger.info(
                f"ZERO_SHOT_REPLAN_DEFER mission={mission.mission_id} reason=last_mile_handoff "
                f"subgoal_index={mission.current_subgoal_index} total={subgoal_count}"
            )

        if should_zero_shot_replan:
            new_subgoals = await mission_brain._react_generate_subgoal(schema, nodes, app, mission=mission)
            if not new_subgoals:
                return {
                    "success": False,
                    "blocked": True,
                    "reason": f"I couldn't find a safe next step for '{schema.target_entity}' here on {schema.domain}.",
                    "action": {"type": "clarify", "speech": f"I need a more specific page cue to continue toward '{schema.target_entity}'."},
                    "pipeline": "ultimate_tara",
                    "mission_id": mission.mission_id if mission else None,
                    "no_legacy_fallback": True
                }

            # 🏁 ReAct said is_done → validate completion & generate end dialogue
            if len(new_subgoals) == 1 and "extract and present" in new_subgoals[0].lower():
                logger.info("🏁 ReAct signals DONE. Validating mission completion...")
                end_result = await _validate_and_end_mission(
                    schema, nodes, mission, mission_brain, app, start_time
                )
                if end_result:
                    return end_result
                # Validation said NOT done → fall through and keep going
                logger.info("🔄 Validator says NOT done yet. Continuing mission.")
                new_subgoals = [f"Navigate to {schema.target_entity}"]

            mission.subgoals = new_subgoals
            mission.current_subgoal_index = 0

            # 🛡️ THE FIX: Wake the mission back up!
            mission.status = "in_progress"

            await mission_brain._save_mission(mission)
            logger.info(f"🧭 Zero-Shot re-plan: new subgoal = '{new_subgoals[0]}'")

        logger.info(
            f"   ✅ Mission: {mission.mission_id}, Subgoal(indexed): "
            f"{_mission_progress_label(mission)}"
        )

        if is_zero_shot and not mission.subgoals:
            return {
                "success": False,
                "blocked": True,
                "reason": f"I couldn't find '{schema.target_entity}' here on {schema.domain}.",
                "action": {"type": "clarify", "speech": f"I couldn't find a grounded step for '{schema.target_entity}' yet."},
                "pipeline": "ultimate_tara",
                "mission_id": mission.mission_id if mission else None,
                "no_legacy_fallback": True
            }

        # 🏁 TERMINAL STATE:
        # If Last-Mile reasoning is enabled, strategy completion transitions to last_mile phase.
        if mission and mission.status in ("completed", "paused"):
            if last_mile_enabled and getattr(mission, "phase", "strategy") != "done":
                logger.info(
                    f"LAST_MILE_ENTER mission={mission.mission_id} "
                    f"status={mission.status} phase={getattr(mission, 'phase', 'strategy')} "
                    "reason=mission_terminal_state"
                )
                mission.status = "in_progress"
                mission.phase = "last_mile"
                if not getattr(mission, "last_mile_started_at", None):
                    mission.last_mile_started_at = time.time()
                await mission_brain._save_mission(mission)
            else:
                logger.info("🏁 Mission already complete. Generating end dialogue...")
                end_result = await _validate_and_end_mission(
                    schema, nodes, mission, mission_brain, app, start_time
                )
                if end_result:
                    return end_result
                # Fallback if end dialogue generation fails
                return {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "answer",
                        "speech": f"I've completed the task for {schema.target_entity}. What would you like to do next?"
                    },
                    "complete": True,
                    "pipeline": "ultimate_tara",
                    "confidence": 1.0
                }

        # Merge mission's own CLICK history into excluded_ids (not type actions)
        for action_key in mission.action_history:
            if ":" in action_key:
                action_type_str, target = action_key.split(":", 1)
                if action_type_str.strip().lower() == "click":
                    excluded_ids.add(target.strip())
        excluded_ids = _drop_reclick_safe_exclusions(excluded_ids, nodes)

        # Verified advancement: only advance subgoal when a prior click/type caused observable effect.
        if verified_advance_active and getattr(mission, "pending_action", None):
            verified, verify_reason = _verify_pending_action_effect(
                mission,
                nodes,
                current_url,
                current_dom_signature,
            )
            pending_action_type = ((mission.pending_action or {}).get("type") or "").lower()
            if (
                verified
                and getattr(mission, "strategy_locked", False)
                and pending_action_type == "click"
                and mission.current_subgoal_index < len(mission.subgoals or [])
            ):
                pending_subgoal = mission.subgoals[mission.current_subgoal_index]
                if not _subgoal_focus_visible(pending_subgoal, nodes):
                    verified = False
                    verify_reason = "effect_without_subgoal_focus"

            logger.info(
                f"PENDING_ACTION_VERIFY success={verified} reason={verify_reason} "
                f"mission={mission.mission_id}"
            )
            if verified:
                await mission_brain.advance_subgoal_verified(
                    mission.mission_id,
                    is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
                    verification_reason=verify_reason,
                )
                mission = await mission_brain._load_mission(mission.mission_id) or mission
                _register_v3_success(getattr(schema, "domain", ""))
            else:
                if verify_reason == "effect_without_subgoal_focus":
                    pending_target = (mission.pending_action or {}).get("target_id", "")
                    if pending_target:
                        excluded_ids.add(pending_target)
                    mission.pending_action = None
                    mission.pending_verify_attempts = 0
                    logger.warning(
                        f"PENDING_ACTION_DROP reason=effect_without_subgoal_focus mission={mission.mission_id} "
                        f"target={pending_target or 'none'}"
                    )
                    await mission_brain._save_mission(mission)
                    logger.info(
                        f"   ✅ Mission(after-verify): {mission.mission_id}, Subgoal(indexed): "
                        f"{_mission_progress_label(mission)}"
                    )
                    # Keep same subgoal and continue routing this turn with the bad target excluded.
                else:
                    mission.pending_verify_attempts = int(getattr(mission, "pending_verify_attempts", 0) or 0) + 1
                    if mission.pending_verify_attempts >= 2:
                        pending_target = (mission.pending_action or {}).get("target_id", "")
                        if pending_target:
                            excluded_ids.add(pending_target)
                        mission.pending_action = None
                        mission.pending_verify_attempts = 0
                        logger.warning(
                            f"PENDING_ACTION_DROP reason=max_verify_attempts mission={mission.mission_id} "
                            f"target={pending_target or 'none'}"
                        )
                        _register_v3_pending_drop(getattr(schema, "domain", ""))
                    await mission_brain._save_mission(mission)

            logger.info(
                f"   ✅ Mission(after-verify): {mission.mission_id}, Subgoal(indexed): "
                f"{_mission_progress_label(mission)}"
            )

        if mission and mission.status in ("completed", "paused"):
            if last_mile_enabled and getattr(mission, "phase", "strategy") != "done":
                logger.info(
                    f"LAST_MILE_ENTER mission={mission.mission_id} "
                    f"status={mission.status} phase={getattr(mission, 'phase', 'strategy')} "
                    "reason=verified_advance_terminal_state"
                )
                mission.status = "in_progress"
                mission.phase = "last_mile"
                if not getattr(mission, "last_mile_started_at", None):
                    mission.last_mile_started_at = time.time()
                await mission_brain._save_mission(mission)

        # Determine query: use current subgoal if available, else target_entity
        current_subgoal = (
            mission.subgoals[mission.current_subgoal_index]
            if mission.current_subgoal_index < len(mission.subgoals)
            else schema.target_entity
        )
        query = current_subgoal
        _subgoal_total = len(mission.subgoals or [])
        _subgoal_display_idx = (
            min(max(0, mission.current_subgoal_index), max(0, _subgoal_total - 1))
            if _subgoal_total > 0
            else mission.current_subgoal_index
        )
        logger.info(
            f"   🎯 Query (subgoal {_subgoal_display_idx}): '{query}'"
        )
        domain_name = getattr(schema, "domain", "unknown")
        pre_subgoal_mode = _classify_subgoal_mode(query)
        total_subgoals_pre = len(mission.subgoals or [])
        on_final_strategy_subgoal_pre = (
            total_subgoals_pre > 0
            and mission.current_subgoal_index >= max(0, total_subgoals_pre - 1)
        )

        # Final strategy read should hand off to Last-Mile planner, not complete in read-only path.
        if (
            last_mile_enabled
            and pre_subgoal_mode == "cognitive_read"
            and on_final_strategy_subgoal_pre
            and getattr(mission, "phase", "strategy") != "last_mile"
        ):
            logger.info(
                f"LAST_MILE_HANDOFF mission={mission.mission_id} "
                f"reason=final_strategy_read subgoal={min(mission.current_subgoal_index + 1, max(1, total_subgoals_pre))}/{total_subgoals_pre}"
            )
            mission.phase = "last_mile"
            mission.status = "in_progress"
            mission.main_goal = mission.main_goal or goal or schema.target_entity
            # Mark strategy as consumed so Last-Mile gate activates immediately this turn.
            mission.current_subgoal_index = len(mission.subgoals or [])
            if not getattr(mission, "last_mile_started_at", None):
                mission.last_mile_started_at = time.time()
            await mission_brain._save_mission(mission)
            query = mission.main_goal or schema.target_entity

        enter_last_mile, last_mile_reason = _should_enter_last_mile(mission, query, schema)

        if last_mile_enabled and (
            enter_last_mile
        ):
            logger.info(
                f"LAST_MILE_ENTER mission={mission.mission_id} "
                f"status={mission.status} phase={getattr(mission, 'phase', 'strategy')} "
                f"reason={last_mile_reason}"
            )
            mission.phase = "last_mile"
            mission.status = "in_progress"
            mission.main_goal = mission.main_goal or goal or schema.target_entity

            queue = list(getattr(mission, "last_mile_queue", []) or [])
            while queue:
                step = queue.pop(0)
                ok, reason = _validate_last_mile_step(step, nodes, excluded_ids)
                if not ok:
                    logger.warning(f"LAST_MILE_STEP_REJECT reason={reason} step={step}")
                    continue
                mission.last_mile_queue = queue
                await mission_brain._save_mission(mission)
                action_type = step.get("action")
                target_id = step.get("target_id", "")
                logger.info(f"LAST_MILE_STEP_EXEC action={action_type} id={target_id}")

                if action_type == "answer":
                    speech = step.get("text") or step.get("why") or f"I've completed {schema.target_entity}."
                    mission.phase = "done"
                    mission.status = "completed"
                    await mission_brain._save_mission(mission)
                    logger.info("LAST_MILE_EXIT done=True reason=queue_answer")
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "answer", "speech": speech, "text": speech},
                        "speech": speech,
                        "complete": True,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": 1.0,
                    }

                if action_type in {"click", "select"}:
                    await mission_brain.record_action(mission.mission_id, "click", target_id, True)
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "click", "target_id": target_id, "text": step.get("text", "")},
                        "speech": f"Executing last-mile step: {step.get('why') or 'clicking'}",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": "high"
                    }

                if action_type == "type_text":
                    text_to_type = step.get("text") or schema.target_entity
                    await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text_to_type)
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "type_text", "target_id": target_id, "text": text_to_type, "press_enter": bool(step.get("press_enter", False))},
                        "speech": f"Executing last-mile step: typing '{text_to_type}'.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": "high"
                    }

                if action_type == "scroll":
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "scroll"},
                        "speech": "Executing last-mile step: scrolling.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": "medium"
                    }

            attempts = int(getattr(mission, "last_mile_attempts", 0) or 0)
            if attempts >= LAST_MILE_MAX_ATTEMPTS:
                logger.warning(f"LAST_MILE_EXIT done=False reason=max_attempts attempts={attempts}")
                mission.status = "paused"
                await mission_brain._save_mission(mission)
                return {
                    "success": False,
                    "blocked": True,
                    "reason": f"I reached the target area but couldn't safely complete the final steps for '{mission.main_goal}'.",
                    "action": {
                        "type": "clarify",
                        "speech": f"I reached the right area but need guidance to finish '{mission.main_goal}'. Should I continue with a broader search on this page?"
                    },
                    "pipeline": "ultimate_tara_last_mile",
                    "no_legacy_fallback": True
                }

            plan = await mission_brain.plan_last_mile(schema, mission, nodes, app=app)
            mission.last_mile_attempts = attempts + 1
            mission.last_mile_last_plan_hash = _hash_last_mile_sequence(plan.final_sequence)
            logger.info(
                f"LAST_MILE_PLAN steps={len(plan.final_sequence)} done={plan.is_done} "
                f"impossible={plan.is_impossible} attempts={mission.last_mile_attempts} "
                f"hash={mission.last_mile_last_plan_hash}"
            )

            if plan.is_done and (_goal_completion_guard(mission.main_goal or schema.target_entity, nodes) or plan.completion_answer):
                speech = plan.completion_answer or f"I've completed the task for {schema.target_entity}."
                mission.phase = "done"
                mission.status = "completed"
                await mission_brain._save_mission(mission)
                logger.info("LAST_MILE_EXIT done=True reason=planner_done")
                return {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "answer", "speech": speech, "text": speech},
                    "speech": speech,
                    "complete": True,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": 1.0
                }

            if plan.is_impossible:
                interactive_count = len([n for n in nodes if getattr(n, "interactive", False)])
                if interactive_count > 0:
                    logger.info(
                        "LAST_MILE_IMPOSSIBLE_OVERRIDE reason=interactive_nodes_available "
                        f"count={interactive_count} action=scroll"
                    )
                    mission.status = "in_progress"
                    await mission_brain._save_mission(mission)
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "scroll"},
                        "speech": "I still have actionable elements here. Scrolling to reveal more relevant results.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": "medium",
                        "no_legacy_fallback": True
                    }
                mission.status = "paused"
                await mission_brain._save_mission(mission)
                logger.info("LAST_MILE_EXIT done=False reason=planner_impossible")
                return {
                    "success": False,
                    "blocked": True,
                    "reason": f"I reached the right area but couldn't complete '{mission.main_goal}'.",
                    "action": {
                        "type": "clarify",
                        "speech": f"I can't complete '{mission.main_goal}' from the current page state. Do you want me to try a different path?"
                    },
                    "pipeline": "ultimate_tara_last_mile",
                    "no_legacy_fallback": True
                }

            valid_steps: List[Dict[str, Any]] = []
            for step in plan.final_sequence:
                ok, reason = _validate_last_mile_step(step, nodes, excluded_ids)
                if ok:
                    valid_steps.append(step)
                else:
                    logger.warning(f"LAST_MILE_STEP_REJECT reason={reason} step={step}")
            mission.last_mile_queue = valid_steps
            await mission_brain._save_mission(mission)
            if valid_steps:
                step = valid_steps[0]
                remaining = valid_steps[1:]
                mission.last_mile_queue = remaining
                await mission_brain._save_mission(mission)
                action_type = step.get("action")
                target_id = step.get("target_id", "")
                logger.info(f"LAST_MILE_STEP_EXEC action={action_type} id={target_id} source=fresh_plan")

                if action_type == "answer":
                    speech = step.get("text") or step.get("why") or f"I've completed {schema.target_entity}."
                    mission.phase = "done"
                    mission.status = "completed"
                    await mission_brain._save_mission(mission)
                    logger.info("LAST_MILE_EXIT done=True reason=fresh_plan_answer")
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "answer", "speech": speech, "text": speech},
                        "speech": speech,
                        "complete": True,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": 1.0
                    }
                if action_type in {"click", "select"}:
                    await mission_brain.record_action(mission.mission_id, "click", target_id, True)
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "click", "target_id": target_id, "text": step.get("text", "")},
                        "speech": f"Executing last-mile step: {step.get('why') or 'clicking'}",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": "high"
                    }
                if action_type == "type_text":
                    text_to_type = step.get("text") or schema.target_entity
                    await mission_brain.record_action(mission.mission_id, "type", target_id, True, text=text_to_type)
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "type_text", "target_id": target_id, "text": text_to_type, "press_enter": bool(step.get("press_enter", False))},
                        "speech": f"Executing last-mile step: typing '{text_to_type}'.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": "high"
                    }
                if action_type == "scroll":
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "scroll"},
                        "speech": "Executing last-mile step: scrolling.",
                        "mission_id": mission.mission_id,
                        "subgoal_index": mission.current_subgoal_index,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": "medium"
                    }
            else:
                # Deterministic live-graph fallback: answer from visible evidence when possible.
                evidence = _extract_model_usage_evidence(mission.main_goal or schema.target_entity, nodes)
                model_usage_evidence = bool(evidence)
                if not evidence:
                    evidence = _extract_visible_goal_evidence(mission.main_goal or schema.target_entity, nodes)
                if evidence and (model_usage_evidence or _goal_completion_guard(mission.main_goal or schema.target_entity, nodes)):
                    speech = evidence
                    mission.phase = "done"
                    mission.status = "completed"
                    await mission_brain._save_mission(mission)
                    logger.info(
                        f"LAST_MILE_EXIT done=True reason={'model_usage_evidence_fallback' if model_usage_evidence else 'visible_evidence_fallback'}"
                    )
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "answer", "speech": speech, "text": speech},
                        "speech": speech,
                        "complete": True,
                        "pipeline": "ultimate_tara_last_mile",
                        "confidence": 0.8,
                    }

                # If no grounded click/type remains, prefer scrolling to reveal more data.
                logger.info("LAST_MILE_FALLBACK action=scroll reason=no_grounded_plan_steps")
                mission.status = "in_progress"
                await mission_brain._save_mission(mission)
                return {
                    "success": True,
                    "blocked": False,
                    "action": {"type": "scroll"},
                    "speech": "I couldn't safely ground a click, so I'm scrolling to reveal more relevant usage details.",
                    "mission_id": mission.mission_id,
                    "subgoal_index": mission.current_subgoal_index,
                    "pipeline": "ultimate_tara_last_mile",
                    "confidence": "medium",
                    "no_legacy_fallback": True
                }
        elif last_mile_enabled:
            logger.info(
                f"LAST_MILE_DEFER mission={mission.mission_id} "
                f"phase={getattr(mission, 'phase', 'strategy')} reason={last_mile_reason}"
            )

        subgoal_mode = _classify_subgoal_mode(query)
        strategy_authoritative = bool(getattr(mission, "strategy_locked", False))
        explicit_target_id_in_query = _extract_explicit_target_id(query)
        allow_first_subgoal_fallback = (not strategy_authoritative) and (not explicit_target_id_in_query)
        if explicit_target_id_in_query:
            logger.info(
                f"SUBGOAL_CONTEXT_LOCK reason=explicit_id_query id={explicit_target_id_in_query} "
                "first_subgoal_fallback=false"
            )
        label_candidates = _extract_label_candidates(
            query,
            schema,
            allow_first_subgoal_fallback=allow_first_subgoal_fallback
        )
        logger.info(
            f"TURN_DIAG strategy_locked={getattr(mission, 'strategy_locked', False)} "
            f"strategy_score={getattr(hive_response, 'strategy_score', 0.0):.2f} "
            f"subgoal_idx={mission.current_subgoal_index + 1}/{max(1, len(mission.subgoals))}"
        )
        overridden_mode = _override_mode_for_labels(subgoal_mode, label_candidates)
        if keyword_direct_v3_active and overridden_mode != subgoal_mode:
            logger.info(
                f"ROUTER_MODE_OVERRIDE reason=label_present old={subgoal_mode} "
                f"new={overridden_mode} labels={label_candidates[:2]}"
            )
            subgoal_mode = overridden_mode

        effective_hive_hints = list(hive_response.visual_hints or [])
        if subgoal_hint_query_active:
            subgoal_hint_queries: List[str] = []
            subgoal_hint_queries.append(query)
            subgoal_hint_queries.extend(label_candidates)
            first_sub = getattr(schema, "first_subgoal", "") or ""
            strategy_is_authoritative = bool(getattr(mission, "strategy_locked", False))
            # Avoid polluting per-step hint retrieval with stale step-0 first_subgoal
            # when we already have a multi-step Hive strategy.
            if first_sub and not strategy_is_authoritative:
                subgoal_hint_queries.append(first_sub)
            dedup_queries: List[str] = []
            seen_queries = set()
            for q in subgoal_hint_queries:
                sq = (q or "").strip()
                if not sq:
                    continue
                k = sq.lower()
                if k in seen_queries:
                    continue
                seen_queries.add(k)
                dedup_queries.append(sq)
            if dedup_queries:
                mission.subgoal_hint_queries = dedup_queries[:6]
                await mission_brain._save_mission(mission)
                if strategy_is_authoritative:
                    logger.info(
                        f"SUBGOAL_QUERY_LOCK query='{query[:80]}' source=strategy_only "
                        f"queries={dedup_queries[:3]}"
                    )
                try:
                    extra_hints = await hive_interface.retrieve_visual_hints_for_queries(
                        schema=schema,
                        queries=dedup_queries[:6],
                        use_cache=True,
                    )
                    if extra_hints:
                        merged = []
                        seen_hint = set()
                        for hint in effective_hive_hints + extra_hints:
                            key = (
                                getattr(hint, "selector", ""),
                                getattr(hint, "element_type", ""),
                                getattr(hint, "zone", ""),
                                getattr(hint, "text_pattern", "") or "",
                            )
                            if key in seen_hint:
                                continue
                            seen_hint.add(key)
                            merged.append(hint)
                        effective_hive_hints = merged
                except Exception as e:
                    logger.warning(f"HIVE_HINT_QUERY source=subgoal failed: {e}")

        # Authoritative explicit-ID route: if the subgoal provides a concrete live ID,
        # execute it directly and skip label fallback/semantic drift.
        if explicit_target_id_in_query and subgoal_mode in {"literal_click", "literal_type"}:
            explicit_node = next(
                (n for n in nodes if getattr(n, "id", "") == explicit_target_id_in_query and getattr(n, "interactive", False)),
                None
            )
            if explicit_node:
                explicit_action = "click" if subgoal_mode == "literal_click" else "type_text"
                if explicit_action == "click":
                    ok_target, reason_target = _validate_action_target(
                        "click", explicit_target_id_in_query, nodes, excluded_ids=excluded_ids
                    )
                    if ok_target:
                        logger.info(
                            f"ID_AUTHORITATIVE_HIT action=click id={explicit_target_id_in_query}"
                        )
                        _next_idx = await _record_and_maybe_advance(
                            mission_brain=mission_brain,
                            mission=mission,
                            action_type="click",
                            target_id=explicit_target_id_in_query,
                            nodes=nodes,
                            current_url=current_url,
                            dom_signature=current_dom_signature,
                            verified_advance_active=verified_advance_active,
                            is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                        )
                        return {
                            "success": True,
                            "blocked": False,
                            "action": {
                                "type": "click",
                                "target_id": explicit_target_id_in_query,
                                "text": (getattr(explicit_node, "text", "") or "")[:80],
                            },
                            "speech": f"Clicking {(getattr(explicit_node, 'text', '') or 'the selected element')[:60]}...",
                            "mission_id": mission.mission_id,
                            "subgoal_index": _next_idx,
                            "confidence": "high",
                            "pipeline": "ultimate_tara_id_authoritative",
                            "pending_verification": verified_advance_active,
                            "timing_ms": int((time.time() - start_time) * 1000),
                        }
                    logger.info(
                        f"ID_AUTHORITATIVE_SKIP action=click id={explicit_target_id_in_query} reason={reason_target}"
                    )

        # ═══════════════════════════════════════════════════════════
        # 🎯 ROBUST LLM LOCATION GUARD — terminal-phase only
        # Run ONLY on final subgoal (or no explicit plan), never mid-plan.
        # ═══════════════════════════════════════════════════════════
        if location_guard_candidate and subgoal_mode != "cognitive_read":
            total_subgoals = len(mission.subgoals or [])
            current_idx = mission.current_subgoal_index
            has_plan = total_subgoals > 0
            on_final_subgoal = (not has_plan) or (current_idx >= max(0, total_subgoals - 1))
            if not on_final_subgoal:
                logger.info(
                    f"⏩ LOCATION GUARD Skipped: mission still in progress "
                    f"({current_idx + 1}/{total_subgoals}). Guard runs at terminal phase only."
                )
            else:
                logger.info(f"🎯 LOCATION GUARD: Terminal-phase LLM check for '{schema.target_entity}'...")
                nodes = await live_graph.get_visible_nodes(session_id)
                is_arrived, arrival_reason = await _check_if_arrived(
                    app, session_id, goal, current_url, nodes, schema
                )
                if is_arrived:
                    logger.info("🎯 LOCATION GUARD: LLM confirmed arrival at terminal phase. Generating final answer...")
                    visible_text = " ".join([n.text for n in nodes if n.text])
                    try:
                        summary_prompt = f"The user asked '{goal}'. Summarize the answer briefly using this page content: {visible_text[:3000]}"
                        if hasattr(app.state, 'mind_reader') and hasattr(app.state.mind_reader, 'llm'):
                            final_answer = await app.state.mind_reader.llm.generate(
                                summary_prompt,
                                model="llama-3.3-70b-versatile",
                                temperature=0.3
                            )
                        else:
                            final_answer = f"I've reached the '{schema.target_entity}' documentation."
                    except Exception as e:
                        logger.error(f"Failed to generate LLM summary for location guard: {e}")
                        final_answer = f"I've reached the '{schema.target_entity}' documentation, but had trouble reading the text."
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {
                            "type": "answer",
                            "text": final_answer,
                        },
                        "speech": final_answer,
                        "complete": True,
                        "pipeline": "ultimate_tara",
                        "confidence": 1.0
                    }

        total_subgoals = len(mission.subgoals or [])
        current_idx = mission.current_subgoal_index
        on_final_strategy_subgoal = (total_subgoals > 0) and (current_idx >= max(0, total_subgoals - 1))

        # Keep detective/router active for intermediate read-like subgoals.
        # For the final strategy read step, prefer read-only extraction.
        if (
            subgoal_mode == "cognitive_read"
            and getattr(mission, "phase", "strategy") != "last_mile"
            and not on_final_strategy_subgoal
        ):
            logger.info(
                f"⏩ READ MODE BYPASS: intermediate subgoal ({current_idx + 1}/{total_subgoals}); "
                "routing through detective."
            )
            subgoal_mode = "ambiguous"
        elif subgoal_mode == "cognitive_read" and on_final_strategy_subgoal:
            logger.info(
                f"🧠 READ MODE: final strategy subgoal ({current_idx + 1}/{total_subgoals}); "
                "running read-only extraction."
            )

        if subgoal_mode == "cognitive_read":
            logger.info("🧠 READ MODE: terminal read-only path (no click/type fallback).")
            read_result = await _run_read_only_terminal(
                session_id=session_id,
                app=app,
                goal=goal,
                query=query,
                schema=schema,
                nodes=nodes,
                mission=mission,
                mission_brain=mission_brain,
                semantic_detective=semantic_detective,
                hive_hints=effective_hive_hints,
                excluded_ids=excluded_ids,
                start_time=start_time,
                is_zero_shot=is_zero_shot,
                current_url=current_url,
                dom_signature=current_dom_signature,
                verified_advance_active=verified_advance_active,
            )
            return read_result

        if subgoal_mode in {"literal_click", "cognitive_navigate", "ambiguous"} and _is_gallery_subgoal(query):
            gallery_target = _find_gallery_click_target(nodes, excluded_ids)
            if gallery_target:
                resolved_id, raw_id = gallery_target
                ok, reason = _validate_action_target("click", resolved_id, nodes, excluded_ids=excluded_ids)
                if ok:
                    logger.info(
                        f"GALLERY_DIRECT_HIT target={resolved_id} raw={raw_id} "
                        f"query='{query[:80]}'"
                    )
                    _next_idx = await _record_and_maybe_advance(
                        mission_brain=mission_brain,
                        mission=mission,
                        action_type="click",
                        target_id=resolved_id,
                        nodes=nodes,
                        current_url=current_url,
                        dom_signature=current_dom_signature,
                        verified_advance_active=verified_advance_active,
                        is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                    )
                    node = next((n for n in nodes if getattr(n, "id", "") == resolved_id), None)
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "click", "target_id": resolved_id, "text": (node.text[:60] if node and node.text else "")},
                        "speech": "Opening gallery item...",
                        "mission_id": mission.mission_id,
                        "subgoal_index": _next_idx,
                        "confidence": "high",
                        "timing_ms": int((time.time() - start_time) * 1000),
                        "pipeline": "ultimate_tara_gallery_direct",
                        "router_mode": "literal_click",
                        "detective_used": False,
                        "detective_score": 0.0,
                        "fallback_tier": "gallery_direct",
                        "pending_verification": verified_advance_active,
                    }
                logger.info(f"GALLERY_DIRECT_MISS reason={reason}")

        label_policy = _build_label_policy(
            query,
            schema,
            subgoal_mode,
            label_candidates=label_candidates,
            allow_first_subgoal_fallback=allow_first_subgoal_fallback
        )
        # Deterministic explicit-label route (authoritative before semantic routing).
        if label_policy["has_explicit_label"]:
            hard_match = _find_hard_keyword_match(
                nodes=nodes,
                labels=label_policy["label_candidates"],
                domain=domain_name,
                subgoal_mode=subgoal_mode,
                excluded_ids=excluded_ids
            )
            if (
                subgoal_mode == "literal_click"
                and strategy_authoritative
                and hard_match.candidate_id
                and hard_match.match_mode == "token_overlap"
            ):
                retry_exact = _find_hard_keyword_match(
                    nodes=nodes,
                    labels=label_policy["label_candidates"],
                    domain=domain_name,
                    subgoal_mode=subgoal_mode,
                    excluded_ids=set(),
                )
                if retry_exact.candidate_id and retry_exact.match_mode in {"exact", "synonym"}:
                    logger.info(
                        f"KEYWORD_DIRECT_RETRY_HIT reason=excluded_exact_label node={retry_exact.candidate_id} "
                        f"mode={retry_exact.match_mode}"
                    )
                    hard_match = retry_exact

            if hard_match.candidate_id:
                label_policy["match_mode"] = hard_match.match_mode
                label_policy["matched_label"] = hard_match.matched_label
                label_policy["raw_node_id"] = hard_match.raw_node_id
                if (
                    subgoal_mode == "literal_click"
                    and strategy_authoritative
                    and label_policy["match_mode"] == "token_overlap"
                ):
                    label_policy["miss_reason"] = "weak_keyword_overlap"
                    logger.info(
                        f"KEYWORD_DIRECT_MISS reason=weak_keyword_overlap labels={label_policy['label_candidates'][:2]}"
                    )
                    hard_match = MatchResult(None, "", "none", None, "weak_keyword_overlap")
                if subgoal_mode == "literal_click" and hard_match.candidate_id:
                    target_id = hard_match.candidate_id
                    resolved_id, resolve_reason = _resolve_clickable_target_id(
                        target_id, nodes, excluded_ids=excluded_ids
                    )
                    if resolved_id:
                        target_id = resolved_id
                    else:
                        peer_id, peer_reason = _resolve_clickable_by_label_context(
                            raw_node_id=target_id,
                            labels=label_policy["label_candidates"],
                            domain=domain_name,
                            nodes=nodes,
                            excluded_ids=excluded_ids,
                        )
                        if peer_id:
                            target_id = peer_id
                            logger.info(
                                f"KEYWORD_DIRECT_HIT label={label_policy['matched_label']} "
                                f"mode={label_policy['match_mode']} node={label_policy['raw_node_id']} "
                                f"resolved={target_id} resolver=label_peer"
                            )
                        else:
                            label_policy["miss_reason"] = "no_clickable_ancestor"
                            logger.info(
                                f"KEYWORD_DIRECT_MISS reason=no_clickable_ancestor labels={label_policy['label_candidates'][:2]} "
                                f"fallback={peer_reason or resolve_reason}"
                            )
                            target_id = ""
                    if target_id:
                        ok, reason = _validate_action_target("click", target_id, nodes, excluded_ids=excluded_ids)
                        if ok:
                            label_policy["resolved_target_id"] = target_id
                            logger.info(
                                f"KEYWORD_DIRECT_HIT label={label_policy['matched_label']} "
                                f"mode={label_policy['match_mode']} node={label_policy['raw_node_id']} "
                                f"resolved={target_id}"
                            )
                            _next_idx = await _record_and_maybe_advance(
                                mission_brain=mission_brain,
                                mission=mission,
                                action_type="click",
                                target_id=target_id,
                                nodes=nodes,
                                current_url=current_url,
                                dom_signature=current_dom_signature,
                                verified_advance_active=verified_advance_active,
                                is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                            )
                            _click_node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
                            return {
                                "success": True,
                                "blocked": False,
                                "action": {"type": "click", "target_id": target_id, "text": _click_node.text[:60] if _click_node and _click_node.text else ""},
                                "speech": f"Clicking on {(_click_node.text[:40].strip() if _click_node and _click_node.text else 'the element')}...",
                                "mission_id": mission.mission_id,
                                "subgoal_index": _next_idx,
                                "confidence": "high",
                                "timing_ms": int((time.time() - start_time) * 1000),
                                "pipeline": "ultimate_tara_router_keyword_direct_hard",
                                "router_mode": subgoal_mode,
                                "detective_used": False,
                                "detective_score": 0.0,
                                "fallback_tier": "keyword_direct_hard",
                                "pending_verification": verified_advance_active,
                            }
                        logger.warning(f"KEYWORD_DIRECT_MISS reason={reason} labels={label_policy['label_candidates'][:2]}")
                        label_policy["miss_reason"] = reason
                elif subgoal_mode == "literal_type":
                    ok, reason = _validate_action_target("type_text", hard_match.candidate_id, nodes, excluded_ids=excluded_ids)
                    if ok:
                        label_policy["resolved_target_id"] = hard_match.candidate_id
                        text_to_type = _extract_type_text(query, schema.target_entity)
                        logger.info(
                            f"KEYWORD_DIRECT_HIT label={label_policy['matched_label']} "
                            f"mode={label_policy['match_mode']} node={label_policy['raw_node_id']} "
                            f"resolved={hard_match.candidate_id}"
                        )
                        _next_idx = await _record_and_maybe_advance(
                            mission_brain=mission_brain,
                            mission=mission,
                            action_type="type_text",
                            target_id=hard_match.candidate_id,
                            nodes=nodes,
                            current_url=current_url,
                            dom_signature=current_dom_signature,
                            text=text_to_type,
                            verified_advance_active=verified_advance_active,
                            is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                        )
                        return {
                            "success": True,
                            "blocked": False,
                            "action": {
                                "type": "type_text",
                                "target_id": hard_match.candidate_id,
                                "text": text_to_type,
                                "press_enter": True
                            },
                            "speech": f"Typing '{text_to_type}' and searching...",
                            "mission_id": mission.mission_id,
                            "subgoal_index": _next_idx,
                            "confidence": "high",
                            "timing_ms": int((time.time() - start_time) * 1000),
                            "pipeline": "ultimate_tara_router_keyword_direct_hard",
                            "router_mode": subgoal_mode,
                            "detective_used": False,
                            "detective_score": 0.0,
                            "fallback_tier": "keyword_direct_hard",
                            "pending_verification": verified_advance_active,
                        }
                    logger.warning(f"KEYWORD_DIRECT_MISS reason={reason} labels={label_policy['label_candidates'][:2]}")
                    label_policy["miss_reason"] = reason
            else:
                label_policy["miss_reason"] = hard_match.reason
                logger.info(
                    f"KEYWORD_DIRECT_MISS reason={hard_match.reason} labels={label_policy['label_candidates'][:2]}"
                )

        should_use_detective_v2 = subgoal_mode in {"literal_click", "literal_type", "ambiguous"}
        bypass_reason_v2 = "" if should_use_detective_v2 else f"subgoal_mode={subgoal_mode}"
        logger.info(
            f"ROUTER_DECISION mode={subgoal_mode} detective_used={should_use_detective_v2} "
            f"domain={domain_name} tier=precheck enabled={TARA_ROUTER_V2_ENABLED} "
            f"shadow={TARA_ROUTER_V2_SHADOW} canary={_is_canary_domain(domain_name)}"
        )

        # ═══════════════════════════════════════════════════════════
        # ⚡ FAST PATH: If ReAct subgoal has explicit [ID: xxx],
        # extract ID and act directly — skip detective + reranker + tier3.
        # Accepts ANY interactive element (not just input/textarea).
        # For search: type + press_enter in one action.
        # ═══════════════════════════════════════════════════════════
        import re as _re_fast

        # Pattern 1: Type 'X' in ... [ID: xxx] or [xxx]
        _type_shortcut = _re_fast.match(
            r"Type\s+'([^']+)'\s+.*?\[(?:ID:\s*)?(\S+?)\]",
            query, _re_fast.IGNORECASE
        )
        # Pattern 2: Click 'X' [ID: xxx] or Click ... [xxx]
        _click_shortcut = _re_fast.match(
            r"Click\s+.*?\[(?:ID:\s*)?(\S+?)\]",
            query, _re_fast.IGNORECASE
        ) if not _type_shortcut else None

        if _type_shortcut:
            _text_to_type = _type_shortcut.group(1)
            _target_id = _type_shortcut.group(2).rstrip("]")
            # Try exact ID match first
            _target_node = next(
                (n for n in nodes if n.id == _target_id and n.interactive),
                None
            )
            # Fallback: if ID not found, find any search input/textarea in DOM
            if not _target_node:
                _target_node = next(
                    (n for n in nodes if n.interactive and n.tag in ('input', 'textarea')
                     and (getattr(n, 'role', '') == 'searchbox'
                          or 'search' in (n.text or '').lower()
                          or 'search' in (getattr(n, 'placeholder', '') or '').lower()
                          or 'search' in (n.id or '').lower())),
                    None
                )
                if _target_node:
                    logger.info(f"   ⚡ FAST PATH: ID '{_target_id}' not found, using search input '{_target_node.id}' instead")
                    _target_id = _target_node.id

            # ✅ PREFIX CHECK: history stores "type:id:typed_text" since we added text param.
            # A plain `"type:id" in list` misses this format (list `in` is exact-match).
            # Use any(entry.startswith(...)) to correctly detect already-typed inputs.
            _already_typed = any(
                entry.startswith(f"type:{_target_id}")
                for entry in mission.action_history
            )

            if _target_node and not _already_typed:
                logger.info(f"   ⚡ FAST PATH: TYPE '{_text_to_type}' into {_target_id} (tag={_target_node.tag}) + press_enter")
                _next_idx = await _record_and_maybe_advance(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="type_text",
                    target_id=_target_id,
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=current_dom_signature,
                    text=_text_to_type,
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                )
                return {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "type_text",
                        "target_id": _target_id,
                        "text": _text_to_type,
                        "press_enter": True  # Search = type + submit in one go
                    },
                    "speech": f"Searching for {_text_to_type}...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_fast_path",
                    "pending_verification": verified_advance_active,
                }
            elif _already_typed:
                logger.info(
                    f"   ⚡ FAST PATH: Already typed into '{_target_id}'. "
                    f"{'Waiting for verification.' if verified_advance_active else 'Auto-advancing to next subgoal.'}"
                )
                if not verified_advance_active:
                    await mission_brain.advance_subgoal(mission.mission_id, is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot)
                    mission = await mission_brain._load_mission(mission.mission_id)
                    if mission and mission.current_subgoal_index < len(mission.subgoals):
                        query = mission.subgoals[mission.current_subgoal_index]
                        logger.info(f"   ⏩ New query (subgoal {mission.current_subgoal_index}): '{query}'")
                    else:
                        logger.info(f"   🏁 All subgoals exhausted after auto-advance.")
            else:
                logger.info(f"   ⚡ FAST PATH: ID '{_target_id}' not found as interactive in DOM, falling through")

        elif _click_shortcut:
            _target_id = _click_shortcut.group(1).rstrip("]")
            _generic_click_shortcut = bool(
                _re_fast.match(r"^\s*Click\s+element\s+\[(?:ID:\s*)?\S+?\]\s*$", query, _re_fast.IGNORECASE)
            )
            _target_node = next(
                (n for n in nodes if n.id == _target_id and n.interactive),
                None
            )
            if _generic_click_shortcut and _target_node:
                logger.info(
                    "   ⚡ FAST PATH: generic 'Click element [ID: ...]' resolved via grounded ID."
                )
            if _target_node and f"click:{_target_id}" not in mission.action_history:
                _retargeted_node, _retarget_reason = _retarget_click_to_nav_duplicate_if_needed(
                    target_node=_target_node,
                    nodes=nodes,
                    query=query,
                    goal=goal,
                )
                if _retargeted_node and getattr(_retargeted_node, "id", "") != _target_id:
                    logger.info(
                        f"   ⚡ FAST PATH RETARGET: {_target_id} -> {_retargeted_node.id} "
                        f"reason={_retarget_reason}"
                    )
                    _target_node = _retargeted_node
                    _target_id = _retargeted_node.id
                _btn_label = (_target_node.text[:40].strip() if _target_node.text else "the element")
                logger.info(f"   ⚡ FAST PATH: CLICK '{_target_node.text[:30] if _target_node.text else ''}' [ID: {_target_id}]")
                _next_idx = await _record_and_maybe_advance(
                    mission_brain=mission_brain,
                    mission=mission,
                    action_type="click",
                    target_id=_target_id,
                    nodes=nodes,
                    current_url=current_url,
                    dom_signature=current_dom_signature,
                    verified_advance_active=verified_advance_active,
                    is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                )
                return {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "click",
                        "target_id": _target_id,
                        "text": _target_node.text[:60] if _target_node.text else ""
                    },
                    "speech": f"Clicking on {_btn_label}...",
                    "mission_id": mission.mission_id,
                    "subgoal_index": _next_idx,
                    "confidence": "high",
                    "timing_ms": int((time.time() - start_time) * 1000),
                    "pipeline": "ultimate_tara_fast_path",
                    "pending_verification": verified_advance_active,
                }

        # ═══════════════════════════════════════════════════════════
        # 💬 CONVERSATIONAL FALLBACK: Intercept ReAct 'clarify' or 'done'
        # If ReAct outputted a conversational subgoal, we shouldn't ask
        # Semantic Detective to click an "Ask user" button...
        # ═══════════════════════════════════════════════════════════
        if query.lower().startswith("ask user:"):
            logger.info(f"   💬 ReAct wants to clarify: '{query}' — skipping Semantic Detective.")
            speech = query[9:].strip()
            # Mark mission as paused so when user replies, we resume seamlessly
            mission.status = "paused"
            await mission_brain._save_mission(mission)
            return {
                "success": True,
                "blocked": False,
                "action": {
                    "type": "clarify",
                    "speech": speech
                },
                "mission_id": mission.mission_id,
                "subgoal_index": mission.current_subgoal_index,
                "confidence": "high",
                "timing_ms": int((time.time() - start_time) * 1000),
                "pipeline": "ultimate_tara"
            }
        
        if "extract and present" in query.lower():
            logger.info("   🏁 ReAct signaled completion: validating and ending mission.")
            end_result = await _validate_and_end_mission(
                schema, nodes, mission, mission_brain, app, start_time
            )
            if end_result:
                return end_result

        # ── Router V2: lexical stage + cognitive bypass ─────────────────────
        lexical_hit = _lexical_ground_candidate(
            query=query,
            schema=schema,
            nodes=nodes,
            subgoal_mode=subgoal_mode,
            excluded_ids=excluded_ids
        ) if subgoal_mode in {"literal_click", "literal_type"} else None

        if lexical_hit and TARA_ROUTER_V2_SHADOW:
            logger.info(
                f"ROUTER_SHADOW lexical_hit score={lexical_hit['score']:.2f} "
                f"node={lexical_hit['node'].id} mode={subgoal_mode}"
            )
        if TARA_ROUTER_V2_SHADOW and (not TARA_ROUTER_V2_ENABLED) and not should_use_detective_v2:
            logger.info(
                f"ROUTER_SHADOW would_bypass_detective mode={subgoal_mode} "
                f"reason={bypass_reason_v2 or 'cognitive'}"
            )

        canary_active = _is_canary_domain(domain_name)
        allow_lexical_direct = not label_policy.get("has_explicit_label", False)
        direct_threshold = (
            LEXICAL_DIRECT_ACCEPT_TYPE if subgoal_mode == "literal_type" else LEXICAL_DIRECT_ACCEPT_CLICK
        )
        lexical_direct_safe = True
        if lexical_hit:
            if subgoal_mode == "literal_click":
                lexical_direct_safe = bool(
                    lexical_hit.get("zone_match", True)
                    and (
                        lexical_hit.get("label_exact", False)
                        or lexical_hit.get("explicit_overlap", 0) >= 1
                    )
                )
            elif subgoal_mode == "literal_type":
                lexical_direct_safe = bool(
                    lexical_hit.get("label_exact", False)
                    or lexical_hit.get("explicit_overlap", 0) >= 1
                )
            if strategy_authoritative and subgoal_mode == "literal_click":
                lexical_direct_safe = bool(
                    lexical_direct_safe
                    and _node_matches_strategy_focus(lexical_hit["node"], query)
                )

        router_v2_active = TARA_ROUTER_V2_ENABLED or canary_active
        if router_v2_active:
            if canary_active and not TARA_ROUTER_V2_ENABLED:
                logger.info(
                    f"ROUTER_DECISION canary_override active domain={domain_name} "
                    "global_enabled=False"
                )
            if lexical_hit and not canary_active:
                logger.info(
                    f"ROUTER_SHADOW lexical_rejected reason=domain_not_in_canary "
                    f"domain={domain_name} score={lexical_hit['score']:.2f}"
                )
            if lexical_hit and canary_active and lexical_hit["score"] < direct_threshold:
                logger.info(
                    f"ROUTER_SHADOW lexical_rejected reason=below_threshold "
                    f"domain={domain_name} score={lexical_hit['score']:.2f} threshold={direct_threshold:.2f}"
                )
            if (
                lexical_hit
                and canary_active
                and strategy_authoritative
                and subgoal_mode == "literal_click"
                and not _node_matches_strategy_focus(lexical_hit["node"], query)
            ):
                logger.info(
                    "ROUTER_SHADOW lexical_rejected reason=strategy_subgoal_mismatch "
                    f"domain={domain_name} query='{query[:80]}'"
                )
            if lexical_hit and canary_active and not lexical_direct_safe:
                logger.info(
                    "ROUTER_SHADOW lexical_rejected reason=failed_explicit_match "
                    f"domain={domain_name} score={lexical_hit['score']:.2f} "
                    f"explicit_overlap={lexical_hit.get('explicit_overlap', 0)} "
                    f"label_exact={lexical_hit.get('label_exact', False)} "
                    f"zone_match={lexical_hit.get('zone_match', True)} "
                    f"terms={lexical_hit.get('explicit_terms', [])} "
                    f"zones={lexical_hit.get('target_zones', [])}"
                )
            if lexical_hit and canary_active and not allow_lexical_direct:
                logger.info("ROUTER_SHADOW lexical_rejected reason=explicit_label_policy")
            if lexical_hit and canary_active and lexical_hit["score"] >= direct_threshold and lexical_direct_safe and allow_lexical_direct:
                ln = lexical_hit["node"]
                if subgoal_mode == "literal_type":
                    text_to_type = _extract_type_text(query, schema.target_entity)
                    logger.info(
                        f"ROUTER_DECISION mode={subgoal_mode} detective_used=False "
                        f"score={lexical_hit['score']:.2f} threshold={direct_threshold:.2f} "
                        f"domain={domain_name} tier=lexical_direct"
                    )
                    _next_idx = await _record_and_maybe_advance(
                        mission_brain=mission_brain,
                        mission=mission,
                        action_type="type_text",
                        target_id=ln.id,
                        nodes=nodes,
                        current_url=current_url,
                        dom_signature=current_dom_signature,
                        text=text_to_type,
                        verified_advance_active=verified_advance_active,
                        is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                    )
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {
                            "type": "type_text",
                            "target_id": ln.id,
                            "text": text_to_type,
                            "press_enter": True
                        },
                        "speech": f"Typing '{text_to_type}' and searching...",
                        "mission_id": mission.mission_id,
                        "subgoal_index": _next_idx,
                        "confidence": "high",
                        "timing_ms": int((time.time() - start_time) * 1000),
                        "pipeline": "ultimate_tara_router_lexical",
                        "router_mode": subgoal_mode,
                        "detective_used": False,
                        "detective_score": 0.0,
                        "fallback_tier": "lexical",
                        "pending_verification": verified_advance_active,
                    }
                else:
                    logger.info(
                        f"ROUTER_DECISION mode={subgoal_mode} detective_used=False "
                        f"score={lexical_hit['score']:.2f} threshold={direct_threshold:.2f} "
                        f"domain={domain_name} tier=lexical_direct"
                    )
                    _next_idx = await _record_and_maybe_advance(
                        mission_brain=mission_brain,
                        mission=mission,
                        action_type="click",
                        target_id=ln.id,
                        nodes=nodes,
                        current_url=current_url,
                        dom_signature=current_dom_signature,
                        verified_advance_active=verified_advance_active,
                        is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                    )
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {"type": "click", "target_id": ln.id, "text": ln.text[:60] if ln.text else ""},
                        "speech": f"Clicking on {(ln.text[:40].strip() if ln.text else 'the element')}...",
                        "mission_id": mission.mission_id,
                        "subgoal_index": _next_idx,
                        "confidence": "high",
                        "timing_ms": int((time.time() - start_time) * 1000),
                        "pipeline": "ultimate_tara_router_lexical",
                        "router_mode": subgoal_mode,
                        "detective_used": False,
                        "detective_score": 0.0,
                        "fallback_tier": "lexical",
                        "pending_verification": verified_advance_active,
                    }

            if not should_use_detective_v2:
                tier3_result = await _run_tier3_fallback(
                    app=app,
                    goal=goal,
                    query=query,
                    nodes=nodes,
                    mission=mission,
                    mission_brain=mission_brain,
                    schema=schema,
                    is_zero_shot=is_zero_shot,
                    start_time=start_time,
                    forced_reason=bypass_reason_v2 or "cognitive_bypass",
                    excluded_ids=excluded_ids,
                    expected_labels=label_policy["label_candidates"] if label_policy.get("has_explicit_label") else None,
                    expected_domain=domain_name,
                    current_url=current_url,
                    dom_signature=current_dom_signature,
                    verified_advance_active=verified_advance_active,
                )
                if tier3_result:
                    tier3_result["router_mode"] = subgoal_mode
                    tier3_result["detective_used"] = False
                    tier3_result["detective_score"] = 0.0
                    tier3_result["fallback_tier"] = "tier3_direct"
                    return tier3_result
                return {
                    "success": False,
                    "blocked": True,
                    "reason": f"I cannot confidently execute '{query}' on this screen.",
                    "action": None,
                    "router_mode": subgoal_mode,
                    "detective_used": False,
                    "detective_score": 0.0,
                    "fallback_tier": "tier3_failed"
                }

        if subgoal_mode == "literal_type":
            heuristic_type_node = _find_best_type_target(nodes, query, excluded_ids)
            if heuristic_type_node:
                text_to_type = _extract_type_text(query, schema.target_entity)
                ok, reason = _validate_action_target("type_text", heuristic_type_node.id, nodes, excluded_ids=excluded_ids)
                if ok:
                    logger.info(
                        f"ROUTER_DECISION mode={subgoal_mode} detective_used=False "
                        f"tier=heuristic_type target={heuristic_type_node.id}"
                    )
                    _next_idx = await _record_and_maybe_advance(
                        mission_brain=mission_brain,
                        mission=mission,
                        action_type="type_text",
                        target_id=heuristic_type_node.id,
                        nodes=nodes,
                        current_url=current_url,
                        dom_signature=current_dom_signature,
                        text=text_to_type,
                        verified_advance_active=verified_advance_active,
                        is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
                    )
                    return {
                        "success": True,
                        "blocked": False,
                        "action": {
                            "type": "type_text",
                            "target_id": heuristic_type_node.id,
                            "text": text_to_type,
                            "press_enter": True
                        },
                        "speech": f"Typing '{text_to_type}' and searching...",
                        "mission_id": mission.mission_id,
                        "subgoal_index": _next_idx,
                        "confidence": "high",
                        "timing_ms": int((time.time() - start_time) * 1000),
                        "pipeline": "ultimate_tara_router_heuristic_type",
                        "router_mode": subgoal_mode,
                        "detective_used": False,
                        "detective_score": 0.0,
                        "fallback_tier": "heuristic_type",
                        "pending_verification": verified_advance_active,
                    }
                logger.warning(f"ROUTER_SHADOW heuristic_type_rejected reason={reason}")

        # Step 6: Investigate with Semantic Detective (with exclusions)
        if label_policy.get("has_explicit_label"):
            logger.info(
                f"SEMANTIC_FALLBACK_AFTER_KEYWORD_MISS reason={label_policy.get('miss_reason') or 'unknown'}"
            )
        logger.info(f"🔍 Step 6: Semantic Detective investigating (excluding {len(excluded_ids)} IDs)...")
        _reject_all = getattr(app.state, "_detective_reject_signatures", {})
        if not hasattr(app.state, "_detective_reject_signatures"):
            app.state._detective_reject_signatures = _reject_all
        _reject_session = _reject_all.setdefault(session_id, {})

        try:
            detective_report = await semantic_detective.investigate(
                session_id=session_id,
                query=query,
                hive_hints=effective_hive_hints,
                action_intent=schema.action,
                excluded_ids=list(excluded_ids),
                subgoal_mode=subgoal_mode
            )
        except TypeError as e:
            # Backward-compat: some deployed images may still load old
            # SemanticDetective.investigate() without subgoal_mode support.
            if "unexpected keyword argument 'subgoal_mode'" not in str(e):
                raise
            logger.warning(
                "⚠️ Detective compatibility fallback: investigate() has no "
                "subgoal_mode. Retrying with legacy signature."
            )
            detective_report = await semantic_detective.investigate(
                session_id=session_id,
                query=query,
                hive_hints=effective_hive_hints,
                action_intent=schema.action,
                excluded_ids=list(excluded_ids)
            )

        if (
            label_policy.get("has_explicit_label")
            and not label_policy.get("resolved_target_id")
            and detective_report.candidates
        ):
            node_by_id = {getattr(n, "id", ""): n for n in nodes}
            for cand in detective_report.candidates:
                node = node_by_id.get(getattr(cand, "node_id", ""))
                is_type_compatible = (
                    subgoal_mode == "literal_type"
                    and (cand.tag or "").lower() in _TYPE_TAGS
                )
                if is_type_compatible:
                    continue
                if not _node_matches_expected_labels(node, label_policy["label_candidates"], domain_name):
                    cand.final_score = max(0.0, cand.final_score * 0.2)
                    cand.hybrid_score = cand.final_score
                    if isinstance(getattr(cand, "score_breakdown", None), dict):
                        cand.score_breakdown["label_floor_penalty"] = 0.2
                        cand.score_breakdown["final"] = cand.final_score
                    if "label_floor_penalty" not in (cand.reasons or []):
                        cand.reasons.append("label_floor_penalty")
            detective_report.candidates.sort(
                key=lambda c: getattr(c, "final_score", getattr(c, "hybrid_score", 0.0)),
                reverse=True,
            )
            detective_report.best_match = detective_report.candidates[0] if detective_report.candidates else None

        # Strong detector accept: skip reranker/tier3 demotion when match is clear.
        strong_detector_accept = False
        if detective_report.best_match:
            _best = detective_report.best_match
            _score = getattr(_best, "final_score", _best.hybrid_score)
            _zone_ok = (_best.zone or "").lower() in {"nav", "sidebar", "header", "main"}
            _compat = _action_tag_compatible(subgoal_mode, _best)
            if _score >= 0.70 and _compat and _zone_ok:
                strong_detector_accept = True
                logger.info(
                    f"ROUTER_GUARD override=strong_detector_accept "
                    f"score={_score:.2f} tag={_best.tag} zone={_best.zone}"
                )

        pre_rerank_best = detective_report.best_match
        pre_rerank_best_id = getattr(pre_rerank_best, "node_id", "") if pre_rerank_best else ""
        pre_rerank_best_score = (
            getattr(pre_rerank_best, "final_score", getattr(pre_rerank_best, "hybrid_score", 0.0))
            if pre_rerank_best
            else 0.0
        )
        pre_rerank_best_label_match = False
        if label_policy.get("has_explicit_label") and pre_rerank_best:
            _pre_node = next((n for n in nodes if getattr(n, "id", "") == pre_rerank_best_id), None)
            pre_rerank_best_label_match = _node_matches_expected_labels(
                _pre_node, label_policy["label_candidates"], domain_name
            )

        skip_llm_rerank = bool(
            strategy_authoritative
            and label_policy.get("has_explicit_label")
            and pre_rerank_best
            and pre_rerank_best_label_match
            and pre_rerank_best_score >= 0.90
            and _action_tag_compatible(subgoal_mode, pre_rerank_best)
        )
        if skip_llm_rerank:
            logger.info(
                f"ROUTER_GUARD rerank_bypass=strategy_label_lock score={pre_rerank_best_score:.2f} "
                f"candidate='{getattr(pre_rerank_best, 'text', '')[:80]}'"
            )

        # 🧠 LLM Reranking: analyst prompt over top 10 candidates (when not bypassed).
        if (not strong_detector_accept) and (not skip_llm_rerank) and detective_report.candidates and len(detective_report.candidates) > 1 and hasattr(app.state, 'mind_reader') and hasattr(app.state.mind_reader, 'llm'):
            top_n = detective_report.candidates[:10]
            logger.info(f"🧠 LLM deciding the absolute Best Match from top {len(top_n)} candidates...")

            previous_subgoal = ""
            if mission.current_subgoal_index > 0 and mission.current_subgoal_index - 1 < len(mission.subgoals):
                previous_subgoal = mission.subgoals[mission.current_subgoal_index - 1]

            candidates_text = "\n".join([
                f"[{i}]: text='{c.text[:50]}' tag='{c.tag}' zone='{c.zone}' "
                f"score={getattr(c, 'final_score', c.hybrid_score):.2f}"
                for i, c in enumerate(top_n)
            ])

            prompt = (
                "You are a strict UI action analyst.\n"
                "Choose the single best candidate for the current subgoal using mission context.\n"
                f"User Goal: {goal}\n"
                f"Main Goal (schema): {schema.target_entity}\n"
                f"Previous Subgoal: {previous_subgoal or 'none'}\n"
                f"Current Subgoal: {query}\n"
                f"Action Mode: {subgoal_mode}\n"
                f"Strategy Locked: {strategy_authoritative}\n"
                f"Explicit Labels: {label_policy.get('label_candidates', [])}\n"
                f"Top {len(top_n)} Candidates:\n{candidates_text}\n\n"
                "Rules:\n"
                "1) Prefer exact/near-exact text match to Current Subgoal labels.\n"
                "2) If strategy is locked, do not drift from current subgoal intent.\n"
                "3) For typing/search tasks choose only input/textarea/select.\n"
                "4) For click tasks choose actionable/clickable targets.\n"
                "5) If no candidate fits, return -1.\n\n"
                f"Return ONLY integer index [0-{len(top_n)-1}] or -1."
                "\nNo explanation."
            )

            try:
                llm_response = await app.state.mind_reader.llm.generate(
                    prompt,
                    model="llama-3.1-8b-instant",
                    temperature=0.0
                )

                import re
                match = re.search(r"-?\d+", llm_response)
                if match:
                    chosen_idx = int(match.group())

                    if chosen_idx == -1:
                        logger.warning("   🛑 LLM Reranker REJECTED all candidates. Forcing V1 Fallback.")
                        if detective_report.best_match:
                            detective_report.best_match.hybrid_score = 0.1
                            detective_report.best_match.final_score = 0.1
                            detective_report.confidence = "low"

                    elif 0 <= chosen_idx < len(top_n):
                        chosen_candidate = top_n[chosen_idx]
                        is_typing_action = "type" in query.lower() or "search" in query.lower()
                        is_input_tag = chosen_candidate.tag.lower() in ["input", "textarea", "select"]
                        chosen_node = next(
                            (n for n in nodes if getattr(n, "id", "") == getattr(chosen_candidate, "node_id", "")),
                            None
                        )
                        label_safe = True
                        if label_policy.get("has_explicit_label"):
                            label_safe = _node_matches_expected_labels(
                                chosen_node, label_policy["label_candidates"], domain_name
                            )

                        if is_typing_action and not is_input_tag:
                            logger.warning(f"   🛑 TAG MISMATCH: Subgoal wants to TYPE, but candidate is '{chosen_candidate.tag}'. Tanking score to force V1 Fallback.")
                            detective_report.best_match = chosen_candidate
                            detective_report.best_match.hybrid_score = 0.1
                            detective_report.best_match.final_score = 0.1
                            detective_report.confidence = "low"
                            _sig = _candidate_signature(chosen_candidate.text, chosen_candidate.tag, chosen_candidate.zone)
                            _reject_session[_sig] = _reject_session.get(_sig, 0) + 1
                        elif subgoal_mode == "literal_click" and not _action_tag_compatible(subgoal_mode, chosen_candidate):
                            logger.warning(
                                f"   🛑 TAG MISMATCH: Subgoal wants CLICK, but candidate is '{chosen_candidate.tag}'. "
                                "Keeping detector-selected actionable candidate."
                            )
                        elif label_policy.get("has_explicit_label") and not label_safe:
                            logger.warning(
                                "ROUTER_GUARD rerank_reject=label_mismatch "
                                f"candidate='{chosen_candidate.text[:80]}' labels={label_policy['label_candidates'][:2]}"
                            )
                            if pre_rerank_best:
                                detective_report.best_match = pre_rerank_best
                                detective_report.confidence = "medium" if pre_rerank_best_score >= DETECTIVE_MIN_SCORE else detective_report.confidence
                                logger.info(
                                    f"ROUTER_GUARD rerank_revert candidate='{getattr(pre_rerank_best, 'text', '')[:80]}' "
                                    f"score={pre_rerank_best_score:.2f}"
                                )
                        else:
                            logger.info(f"   🤖 LLM ranked candidate {chosen_idx} as best: '{chosen_candidate.text}' (was original rank #{chosen_idx+1})")
                            detective_report.best_match = chosen_candidate
                            if getattr(detective_report.best_match, "final_score", detective_report.best_match.hybrid_score) < 0.35:
                                detective_report.confidence = "low"
                    else:
                        logger.warning(f"   ⚠️ LLM returned out of bounds index: {chosen_idx}")
                else:
                    logger.warning(f"   ⚠️ LLM returned non-integer: '{llm_response}'")
            except Exception as e:
                logger.error(f"   ⚠️ LLM reranking failed: {e}")

        # Router V2 compatibility guardrails
        if label_policy.get("has_explicit_label") and detective_report.best_match:
            _best_node = next(
                (n for n in nodes if getattr(n, "id", "") == getattr(detective_report.best_match, "node_id", "")),
                None
            )
            if not _node_matches_expected_labels(_best_node, label_policy["label_candidates"], domain_name):
                logger.warning(
                    "SEMANTIC_FALLBACK_AFTER_KEYWORD_MISS reason=best_candidate_label_mismatch "
                    f"labels={label_policy['label_candidates'][:2]}"
                )
                if pre_rerank_best and pre_rerank_best_label_match:
                    detective_report.best_match = pre_rerank_best
                    logger.info(
                        f"ROUTER_GUARD rerank_revert reason=label_mismatch candidate='{getattr(pre_rerank_best, 'text', '')[:80]}' "
                        f"score={pre_rerank_best_score:.2f}"
                    )
                else:
                    detective_report.best_match.hybrid_score = 0.1
                    detective_report.best_match.final_score = 0.1
                    detective_report.confidence = "low"

        _compat_ok = _action_tag_compatible(subgoal_mode, detective_report.best_match) if detective_report.best_match else False
        if detective_report.best_match and not _compat_ok:
            logger.warning(
                f"   🛑 ROUTER_GUARD: candidate incompatible with mode={subgoal_mode} "
                f"(tag={detective_report.best_match.tag}). Forcing Tier 3."
            )
            _sig = _candidate_signature(
                detective_report.best_match.text,
                detective_report.best_match.tag,
                detective_report.best_match.zone
            )
            _reject_session[_sig] = _reject_session.get(_sig, 0) + 1
            detective_report.best_match.hybrid_score = 0.1
            detective_report.best_match.final_score = 0.1
            detective_report.confidence = "low"

        if detective_report.best_match and strategy_authoritative and subgoal_mode == "literal_click":
            _strategy_node = next(
                (n for n in nodes if getattr(n, "id", "") == getattr(detective_report.best_match, "node_id", "")),
                None
            )
            if _strategy_node and not _node_matches_strategy_focus(_strategy_node, query):
                logger.warning(
                    "ROUTER_GUARD strategy_locked_mismatch=true "
                    f"query='{query[:80]}' candidate='{getattr(detective_report.best_match, 'text', '')[:80]}'"
                )
                detective_report.best_match.hybrid_score = 0.1
                detective_report.best_match.final_score = 0.1
                detective_report.confidence = "low"
            elif _strategy_node:
                _score = getattr(detective_report.best_match, "final_score", detective_report.best_match.hybrid_score)
                if _score >= DETECTIVE_MIN_SCORE and _action_tag_compatible(subgoal_mode, detective_report.best_match):
                    if detective_report.confidence == "low":
                        logger.info(
                            "ROUTER_GUARD override=strategy_focus_match "
                            f"query='{query[:80]}' candidate='{getattr(detective_report.best_match, 'text', '')[:80]}' "
                            f"score={_score:.2f}"
                        )
                    detective_report.confidence = "medium"

        if detective_report.best_match and TARA_ROUTER_V2_ENABLED:
            _sig = _candidate_signature(
                detective_report.best_match.text,
                detective_report.best_match.tag,
                detective_report.best_match.zone
            )
            if _reject_session.get(_sig, 0) >= MAX_DETECTIVE_RETRIES_PER_SUBGOAL:
                logger.warning(
                    f"   🛑 LOOP_GUARD: repeated rejected signature '{_sig[:72]}' "
                    f"count={_reject_session.get(_sig, 0)}. Forcing Tier 3."
                )
                detective_report.best_match.hybrid_score = 0.1
                detective_report.best_match.final_score = 0.1
                detective_report.confidence = "low"

        logger.info(f"   ✅ Final Best Match: {detective_report.best_match.text if detective_report.best_match else 'none'} "
                   f"(score: {getattr(detective_report.best_match, 'final_score', detective_report.best_match.hybrid_score) if detective_report.best_match else 0:.2f})")
        explicit_label_lock = False
        if (
            detective_report.candidates
            and len(detective_report.candidates) > 1
            and detective_report.best_match
        ):
            # Compare against the best *different* candidate.
            # After LLM rerank, best_match may be candidates[1]; using candidates[1]
            # directly yields delta=0 and triggers false ambiguity.
            _best_id = getattr(detective_report.best_match, "node_id", "")
            _best_text_can = _canonicalize_label(getattr(detective_report.best_match, "text", "") or "")
            _next_other = next(
                (
                    c for c in detective_report.candidates
                    if getattr(c, "node_id", "") != _best_id
                    and _canonicalize_label(getattr(c, "text", "") or "") != _best_text_can
                ),
                None
            )
            if _next_other is None:
                _delta = 1.0
            else:
                _delta = (
                    getattr(detective_report.best_match, "final_score", detective_report.best_match.hybrid_score)
                    - getattr(_next_other, "final_score", _next_other.hybrid_score)
                )
            if (not strong_detector_accept) and _delta < DETECTIVE_AMBIGUOUS_BAND:
                detective_report.confidence = "low"
                logger.warning(
                    f"   ⚠️ ROUTER_GUARD: Ambiguous detector ranking delta={_delta:.2f} "
                    f"< band={DETECTIVE_AMBIGUOUS_BAND:.2f}. Forcing low confidence."
                )
        if label_policy.get("has_explicit_label") and detective_report.best_match:
            _best_node = next(
                (n for n in nodes if getattr(n, "id", "") == getattr(detective_report.best_match, "node_id", "")),
                None
            )
            _best_score = getattr(detective_report.best_match, "final_score", detective_report.best_match.hybrid_score)
            if (
                _best_node
                and _node_matches_expected_labels(_best_node, label_policy["label_candidates"], domain_name)
                and _best_score >= max(0.50, DETECTIVE_MIN_SCORE)
                and _action_tag_compatible(subgoal_mode, detective_report.best_match)
            ):
                if detective_report.confidence == "low":
                    logger.info(
                        f"ROUTER_GUARD override=explicit_label_match score={_best_score:.2f} "
                        f"labels={label_policy['label_candidates'][:2]}"
                    )
                detective_report.confidence = "medium"
                explicit_label_lock = True

        # Step 7: Audit action with Mission Brain (constraint enforcement)
        if detective_report.best_match:

            # ═══════════════════════════════════════════════════════════
            # TIER 2 → TIER 3 CONFIDENCE GUARDRAIL
            # If Semantic Detective (Tier 1) + LLM Reranker (Tier 2)
            # both fail (score < 0.35), activate the V1 Full-DOM Fallback (Tier 3)
            # to let the Multi-Action agent handle complex inputs.
            # ═══════════════════════════════════════════════════════════

            best_score = getattr(detective_report.best_match, "final_score", detective_report.best_match.hybrid_score)
            needs_tier3 = ((not explicit_label_lock and detective_report.confidence == "low") or best_score < DETECTIVE_MIN_SCORE)
            if strong_detector_accept and best_score >= DETECTIVE_MIN_SCORE:
                needs_tier3 = False
                logger.info("ROUTER_GUARD bypass=tier3 strong_detector_accept=true")
            if needs_tier3:
                _sig = _candidate_signature(
                    detective_report.best_match.text,
                    detective_report.best_match.tag,
                    detective_report.best_match.zone
                )
                _reject_session[_sig] = _reject_session.get(_sig, 0) + 1
                tier3_result = await _run_tier3_fallback(
                    app=app,
                    goal=goal,
                    query=query,
                    nodes=nodes,
                    mission=mission,
                    mission_brain=mission_brain,
                    schema=schema,
                    is_zero_shot=is_zero_shot,
                    start_time=start_time,
                    forced_reason=f"detective_score={best_score:.2f}",
                    excluded_ids=excluded_ids,
                    expected_labels=label_policy["label_candidates"] if label_policy.get("has_explicit_label") else None,
                    expected_domain=domain_name,
                    current_url=current_url,
                    dom_signature=current_dom_signature,
                    verified_advance_active=verified_advance_active,
                )
                if tier3_result:
                    tier3_result["router_mode"] = subgoal_mode
                    tier3_result["detective_used"] = True
                    tier3_result["detective_score"] = best_score
                    tier3_result["fallback_tier"] = "tier3_after_detective"
                    return tier3_result
                logger.warning("   🚫 All 3 tiers failed. Blocking action.")
                return {
                    "success": False,
                    "blocked": True,
                    "reason": f"I cannot confidently find '{query}' on this screen.",
                    "action": None,
                    "mission_id": mission.mission_id,
                    "no_legacy_fallback": bool(getattr(mission, "strategy_locked", False)),
                    "router_mode": subgoal_mode,
                    "detective_used": True,
                    "detective_score": best_score,
                    "fallback_tier": "tier3_failed"
                }

            logger.info("🛡️ Step 7: Mission Brain auditing action...")
            approved, reason = await mission_brain.audit_action(
                mission_id=mission.mission_id,
                action_type=detective_report.recommended_action,
                target_id=detective_report.best_match.node_id,
                target_text=detective_report.best_match.text,
                detective_report=detective_report
            )

            if not approved:
                logger.warning(f"   🚫 Action BLOCKED: {reason}")
                # Advance subgoal on block (try next subgoal instead of repeating)
                await mission_brain.advance_subgoal(mission.mission_id, is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot)
                return {
                    "success": False,
                    "blocked": True,
                    "reason": reason,
                    "action": None
                }

            logger.info(f"   ✅ Action APPROVED: {detective_report.recommended_action}")

            action_type = detective_report.recommended_action
            node_id = detective_report.best_match.node_id if detective_report.best_match else ""
            text_to_type = ""
            pending_action_type = "click"
            if action_type in {"type", "type_text"}:
                pending_action_type = "type_text"
                text_to_type = _extract_type_text(query, schema.target_entity)

            _final_subgoal_idx = await _record_and_maybe_advance(
                mission_brain=mission_brain,
                mission=mission,
                action_type=pending_action_type,
                target_id=node_id,
                nodes=nodes,
                current_url=current_url,
                dom_signature=current_dom_signature,
                text=text_to_type,
                verified_advance_active=verified_advance_active,
                is_zero_shot=getattr(schema, 'zero_shot_mode', False) or is_zero_shot,
            )

        if "_final_subgoal_idx" not in locals():
            _final_subgoal_idx = mission.current_subgoal_index

        # Build response
        elapsed_ms = int((time.time() - start_time) * 1000)
        _det_label = (detective_report.best_match.text[:40].strip() if detective_report.best_match and detective_report.best_match.text else "the element")
        _det_action = detective_report.recommended_action
        _action_wire = "type_text" if _det_action in {"type", "type_text"} else "click"
        _det_speech = f"{'Clicking on' if _action_wire == 'click' else 'Typing into'} {_det_label}..."

        return {
            "success": True,
            "blocked": False,
            "action": {
                "type": _action_wire,
                "target_id": detective_report.best_match.node_id if detective_report.best_match else "",
                "text": detective_report.best_match.text if detective_report.best_match else "",
            },
            "speech": _det_speech,
            "mission_id": mission.mission_id,
            "subgoal_index": mission.current_subgoal_index,
            "confidence": detective_report.confidence,
            "timing_ms": elapsed_ms,
            "pipeline": "ultimate_tara",
            "gps_hints": hints,
            "router_mode": subgoal_mode,
            "detective_used": True,
            "detective_score": getattr(detective_report.best_match, "final_score", detective_report.best_match.hybrid_score) if detective_report.best_match else 0.0,
            "fallback_tier": "detective",
            "pending_verification": verified_advance_active,
        }
        
    except Exception as e:
        logger.error(f"❌ Ultimate TARA pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        return None


async def ultimate_update_constraint(
    app,
    mission_id: str,
    constraint_name: str,
    value: str
) -> Dict[str, Any]:
    """
    Update constraint value (e.g., user selected size).
    """
    mission_brain = app.state.mission_brain
    
    if not mission_brain:
        return {"success": False, "error": "Mission Brain not available"}
    
    try:
        from tara_models import ConstraintStatus
        
        success = await mission_brain.update_constraint(
            mission_id=mission_id,
            constraint_name=constraint_name,
            value=value,
            status=ConstraintStatus.FILLED
        )
        
        if success:
            status = await mission_brain.get_mission_status(mission_id)
            return {
                "success": True,
                "constraint": constraint_name,
                "value": value,
                "mission_status": status
            }
        else:
            return {"success": False, "error": "Failed to update constraint"}
            
    except Exception as e:
        logger.error(f"❌ Constraint update failed: {e}")
        return {"success": False, "error": str(e)}

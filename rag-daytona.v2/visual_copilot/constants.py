"""Feature flags and constants for Visual CoPilot modular runtime."""

import os
from typing import Dict


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
ENABLE_PRE_ROUTER_VISION = _env_bool("ENABLE_PRE_ROUTER_VISION", False)
PRE_ROUTER_VISION_MIN_CONF = float(os.getenv("PRE_ROUTER_VISION_MIN_CONF", "0.75"))
PRE_ROUTER_VISION_MODEL = os.getenv(
    "PRE_ROUTER_VISION_MODEL",
    "meta-llama/llama-4-scout-17b-16e-instruct",
)
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
    if not host or host == "unknown":
        return True
    if host in LAST_MILE_CANARY_DOMAINS:
        return True
    host_root = _root_domain(host)
    return host_root in {_root_domain(d) for d in LAST_MILE_CANARY_DOMAINS}

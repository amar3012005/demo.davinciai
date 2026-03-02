import re
import unicodedata
from typing import List
from visual_copilot.constants import _NOISE_WORDS, _ZONE_HINTS, _GALLERY_WORDS

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

def _extract_type_text(query: str, default_text: str) -> str:
    m = re.search(r"type\s+'([^']+)'", query, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"search for\s+(.+)$", query, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return (default_text or "").strip()[:80]

def _candidate_signature(text: str, tag: str, zone: str) -> str:
    compact = re.sub(r"\s+", " ", (text or "").lower()).strip()[:96]
    return f"{tag.lower()}|{zone.lower()}|{compact}"

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

def _override_mode_for_labels(subgoal_mode: str, label_candidates: List[str]) -> str:
    if subgoal_mode in {"cognitive_navigate", "ambiguous"} and label_candidates:
        return "literal_click"
    return subgoal_mode

def _classify_subgoal_mode(query: str) -> str:
    q = (query or "").lower()
    commerce_terms = {
        "product", "products", "results", "gallery", "item", "items",
        "filter", "filters", "size", "color", "price", "sort",
        "cart", "buy", "shop", "checkout", "wishlist",
    }
    has_commerce_intent = any(t in q for t in commerce_terms)
    is_question_like = ("?" in q) or q.startswith(("what ", "which ", "how ", "why ", "where ", "when "))

    # For commerce/search workflows, "review/check" should not force terminal read-only mode.
    if has_commerce_intent and not is_question_like:
        if any(k in q for k in ["type", "enter", "search for", "fill", "input"]):
            return "literal_type"
        if any(k in q for k in ["click", "open tab", "select", "press", "toggle", " tab", "button", "link", "menu", "locate", "open "]):
            return "literal_click"
        return "ambiguous"

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

def _extract_explicit_target_id(query: str) -> str:
    if not query:
        return ""
    m = re.search(r"\[(?:ID:\s*)?([^\]\s]+)\]", query, re.IGNORECASE)
    if not m:
        return ""
    return (m.group(1) or "").strip().rstrip("]")

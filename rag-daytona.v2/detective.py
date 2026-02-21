"""
TARA v5.1 — Python Detective (Layer 2)

Zero-LLM pre-compute that runs every step (~0-2ms).
Scores elements, detects obstacles, classifies page state,
and routes to the correct model (shortcut / 8B / 120B).

Usage:
    from detective import investigate, DetectiveReport
    report = investigate(goal, subgoal, dom_elements, ...)
    if report.recommended_model == "shortcut": ...
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set

logger = logging.getLogger(__name__)

# ── Keyword Sets ──────────────────────────────────────────────────────────────

_DISMISS_KEYWORDS = frozenset([
    "accept", "accept all", "close", "dismiss", "got it", "agree",
    "ok", "okay", "i agree", "i understand", "continue", "no thanks",
    "reject all", "deny", "x", "\u2715", "\u00d7", "skip",
])

_OBSTACLE_TEXT = frozenset([
    "cookie", "consent", "privacy", "gdpr", "we use cookies",
    "accept all", "manage preferences", "terms of service",
])

_INPUT_TYPES = frozenset(["input", "textarea", "select"])

_NAV_KEYWORDS = frozenset([
    "dashboard", "home", "settings", "account", "billing",
    "usage", "api keys", "profile", "docs", "documentation",
    "pricing", "support", "help", "sign in", "log in", "sign out",
])


# ── DetectiveReport ───────────────────────────────────────────────────────────

@dataclass
class DetectiveReport:
    """Pre-computed analysis: situation + candidates + routing decision."""

    # Situation
    page_identity: str = ""
    page_url: str = ""
    on_target: bool = False

    # Obstacles
    has_obstacle: bool = False
    obstacle_type: str = "none"          # modal | cookie_banner | overlay | none
    obstacle_dismiss_id: str = ""

    # Page classification
    page_type: str = "nav"               # form | nav | data | search_results | error
    form_fields: List[dict] = field(default_factory=list)
    next_empty_field: Optional[dict] = None

    # Evidence (answer already visible?)
    answer_found: bool = False
    evidence: str = ""

    # Candidates: scored interactive elements
    candidates: List[dict] = field(default_factory=list)

    # History awareness
    tried_and_failed: List[str] = field(default_factory=list)
    blocked_ids: Set[str] = field(default_factory=set)

    # Routing decision
    complexity: str = "simple"           # trivial | simple | complex
    recommended_model: str = "8b"        # shortcut | 8b | 120b
    recommended_action: str = "click"    # click | type | select | answer | dismiss | scroll
    routing_reason: str = ""


# ── Main Investigation ────────────────────────────────────────────────────────

def investigate(
    goal: str,
    subgoal: str,
    dom_elements: list,
    action_history: list = None,
    reflexion_entries: list = None,
    url: str = "",
    active_states: dict = None,
    data_tables: list = None,
    page_title: str = "",
    stagnation_action: str = "continue",
    requires_reasoning: bool = False,
) -> DetectiveReport:
    """
    The Detective: zero-LLM pre-compute that runs every step.
    Produces a DetectiveReport with scored candidates and routing decision.
    """
    report = DetectiveReport()
    report.page_url = url
    report.page_identity = _identify_page(url, page_title, active_states)

    goal_lower = goal.lower()
    subgoal_lower = subgoal.lower()
    goal_words = _extract_words(goal_lower)
    subgoal_words = _extract_words(subgoal_lower)
    all_words = goal_words | subgoal_words

    # ── 1. Build blocked set from history ──────────────────────────────────
    action_history = action_history or []
    report.blocked_ids = set()
    report.tried_and_failed = []
    for record in action_history:
        target_id = getattr(record, "target_id", "") or ""
        outcome = getattr(record, "actual_outcome", "") or ""
        if target_id and ("fail" in outcome.lower() or "no effect" in outcome.lower()
                          or "no observable" in outcome.lower()):
            report.blocked_ids.add(target_id)
            target_text = getattr(record, "target_text", "") or target_id
            report.tried_and_failed.append(target_text)

    # ── 2. Obstacle detection ──────────────────────────────────────────────
    _detect_obstacles(report, dom_elements)

    # ── 3. Page type classification ────────────────────────────────────────
    _classify_page(report, dom_elements)

    # ── 4. Check if answer is visible ──────────────────────────────────────
    _check_answer_visible(report, dom_elements, data_tables, goal_lower, goal_words)

    # ── 5. Score and rank candidates ───────────────────────────────────────
    _score_candidates(report, dom_elements, all_words, subgoal_lower, url)

    # ── 6. On-target check ─────────────────────────────────────────────────
    if subgoal_lower:
        # Check if current page/state already satisfies the sub-goal
        if "read" in subgoal_lower or "answer" in subgoal_lower:
            if report.answer_found:
                report.on_target = True
        elif url:
            # Check for URL-based sub-goal completion
            url_lower = url.lower()
            for word in subgoal_words:
                if len(word) > 4 and word in url_lower:
                    report.on_target = True
                    break

    # ── 7. Route to the right model ────────────────────────────────────────
    _route_complexity(report, stagnation_action, requires_reasoning)

    return report


# ── Obstacle Detection ────────────────────────────────────────────────────────

def _detect_obstacles(report: DetectiveReport, dom_elements: list):
    """Detect modals, cookie banners, overlays that block interaction."""
    for el in dom_elements:
        role = el.get("role", "")
        zone = el.get("zone", "")
        text = (el.get("text", "") or "").lower()
        el_type = el.get("type", "")

        is_modal = (
            role in ("dialog", "alertdialog")
            or zone == "modal"
        )
        is_cookie = any(kw in text for kw in _OBSTACLE_TEXT)

        if is_modal or is_cookie:
            report.has_obstacle = True
            report.obstacle_type = "cookie_banner" if is_cookie else "modal"

            # Find dismiss button nearby
            _find_dismiss_button(report, dom_elements, el.get("id", ""))
            break


def _find_dismiss_button(report: DetectiveReport, dom_elements: list, modal_id: str):
    """Find the most likely dismiss/close button for an obstacle."""
    best_score = 0
    best_id = ""
    for el in dom_elements:
        if not el.get("interactive"):
            continue
        text = (el.get("text", "") or "").lower().strip()
        el_zone = el.get("zone", "")

        # Must be in modal zone or near the modal
        if el_zone != "modal" and report.obstacle_type == "modal":
            continue

        for kw in _DISMISS_KEYWORDS:
            if kw == text or kw in text:
                score = 10 if kw == text else 5  # exact match > partial
                if "accept" in text:
                    score += 3  # prefer "accept" for cookie banners
                if score > best_score:
                    best_score = score
                    best_id = el.get("id", "")
                break

    report.obstacle_dismiss_id = best_id


# ── Page Classification ───────────────────────────────────────────────────────

def _classify_page(report: DetectiveReport, dom_elements: list):
    """Classify page type and extract form fields if applicable."""
    input_elements = []
    error_count = 0

    for el in dom_elements:
        el_type = el.get("type", "")
        text = (el.get("text", "") or "").lower()

        if el_type in _INPUT_TYPES:
            input_elements.append(el)

        if "error" in text[:50] or "failed" in text[:50]:
            error_count += 1

    if error_count >= 2:
        report.page_type = "error"
    elif len(input_elements) >= 3:
        report.page_type = "form"
        report.form_fields = _extract_form_fields(input_elements)
        report.next_empty_field = _find_next_empty(report.form_fields)
    elif len(input_elements) == 1:
        # Single input = likely search
        report.page_type = "search"
    else:
        report.page_type = "nav"


def _extract_form_fields(input_elements: list) -> list:
    """Extract structured form field metadata."""
    fields = []
    for el in input_elements:
        label = (
            el.get("ariaLabel", "")
            or el.get("placeholder", "")
            or el.get("text", "")
            or ""
        )
        fields.append({
            "id": el.get("id", ""),
            "type": el.get("inputType", el.get("type", "text")),
            "label": label.strip()[:60],
            "required": bool(el.get("required") or el.get("ariaRequired") == "true"),
            "filled": bool(el.get("value", "")),
            "value": el.get("value", ""),
        })
    return fields


def _find_next_empty(form_fields: list) -> Optional[dict]:
    """Find next unfilled required field, or first unfilled field."""
    for f in form_fields:
        if f["required"] and not f["filled"]:
            return f
    for f in form_fields:
        if not f["filled"]:
            return f
    return None


# ── Answer Detection ──────────────────────────────────────────────────────────

def _check_answer_visible(
    report: DetectiveReport,
    dom_elements: list,
    data_tables: list,
    goal_lower: str,
    goal_words: set,
):
    """Check if the answer to the goal is already visible in DOM or tables."""
    # Check tables first (structured data)
    if data_tables:
        for table in data_tables:
            headers = [str(h).lower() for h in table.get("headers", [])]
            rows = table.get("rows", [])
            # Do any headers match goal words?
            header_match = sum(1 for w in goal_words if any(w in h for h in headers))
            if header_match >= 1 and rows:
                # Format evidence from table
                header_str = " | ".join(str(h) for h in table["headers"])
                evidence_lines = [header_str]
                for row in rows[:8]:
                    evidence_lines.append(" | ".join(str(c) for c in row))
                report.answer_found = True
                report.evidence = "\n".join(evidence_lines)
                return

    # Check DOM text for data patterns (numbers, prices, stats)
    data_patterns = re.compile(r'(\$[\d,.]+|[\d,]+\s*(?:tokens?|requests?|calls?|%)|[\d,.]+[MKBmkb]\b)')
    matching_data = []
    for el in dom_elements:
        text = el.get("text", "") or ""
        if not text:
            continue
        text_lower = text.lower()
        # Check if element text matches goal words AND contains data
        word_match = sum(1 for w in goal_words if w in text_lower)
        if word_match >= 1 and data_patterns.search(text):
            matching_data.append(text[:100])

    if len(matching_data) >= 2:
        report.answer_found = True
        report.evidence = "\n".join(matching_data[:6])


# ── Candidate Scoring ─────────────────────────────────────────────────────────

def _score_candidates(
    report: DetectiveReport,
    dom_elements: list,
    all_words: set,
    subgoal_lower: str,
    url: str,
):
    """Score interactive elements by relevance to sub-goal. Top 8 returned."""
    scored = []

    for el in dom_elements:
        if not el.get("interactive"):
            continue

        el_id = el.get("id", "")
        if not el_id:
            continue
        if el_id in report.blocked_ids:
            continue  # Skip tried-and-failed elements

        text = (el.get("text", "") or "").lower()
        el_type = el.get("type", "")
        zone = el.get("zone", "main")
        score = 0
        reasons = []

        # 1. Direct text match with sub-goal words (+10 each)
        for word in all_words:
            if word in text:
                score += 10
                reasons.append(f"text:'{word}'")

        # 2. Exact sub-goal element reference (+20)
        # Sub-goal often contains "(id: t-xxx)" or "'element text'"
        if el_id in subgoal_lower:
            score += 20
            reasons.append("id_in_subgoal")
        # Check quoted text in sub-goal: 'Dashboard', 'Docs', etc.
        quoted = re.findall(r"'([^']+)'", subgoal_lower)
        for q in quoted:
            if q.lower() in text:
                score += 15
                reasons.append(f"quoted:'{q}'")

        # 3. Navigation zone bonus (+5)
        if zone == "nav":
            score += 5
            reasons.append("nav")

        # 4. Element type relevance
        if el_type in ("a", "button") and score > 0:
            score += 3
        if el_type in ("input", "textarea", "select"):
            score += 2

        # 5. ARIA active state — boost if relevant, penalize if already visited
        aria_current = el.get("ariaCurrent", "")
        if aria_current in ("page", "true", "step"):
            # Already active = probably already on this page
            score -= 5
            reasons.append("already_active")

        # 6. Viewport — prefer visible elements
        if el.get("inViewport", True):
            score += 2

        if score > 0:
            scored.append({
                "id": el_id,
                "text": (el.get("text", "") or "")[:60],
                "type": el_type,
                "zone": zone,
                "score": score,
                "reasons": reasons,
            })

    # Sort by score descending, keep top 8
    scored.sort(key=lambda x: -x["score"])
    report.candidates = scored[:8]


# ── Complexity Routing ────────────────────────────────────────────────────────

def _route_complexity(
    report: DetectiveReport,
    stagnation_action: str,
    requires_reasoning: bool,
):
    """Determine which model to use based on situation analysis."""

    # Priority 1: Obstacle present
    if report.has_obstacle:
        if report.obstacle_dismiss_id:
            report.complexity = "trivial"
            report.recommended_model = "shortcut"
            report.recommended_action = "dismiss"
            report.routing_reason = f"Obstacle ({report.obstacle_type}) with clear dismiss button"
        else:
            report.complexity = "complex"
            report.recommended_model = "120b"
            report.recommended_action = "dismiss"
            report.routing_reason = f"Obstacle ({report.obstacle_type}) with no obvious dismiss"
        return

    # Priority 2: Answer already visible
    if report.answer_found and report.evidence:
        report.complexity = "trivial"
        report.recommended_model = "shortcut"
        report.recommended_action = "answer"
        report.routing_reason = "Answer data visible in DOM/tables"
        return

    # Priority 3: Form filling
    if report.page_type == "form" and report.next_empty_field:
        ftype = report.next_empty_field.get("type", "text")
        if ftype in ("select", "dropdown", "date"):
            report.complexity = "complex"
            report.recommended_model = "120b"
            report.recommended_action = "select"
            report.routing_reason = f"Form field type '{ftype}' needs reasoning"
        else:
            report.complexity = "simple"
            report.recommended_model = "8b"
            report.recommended_action = "type"
            report.routing_reason = f"Simple form field '{ftype}'"
        return

    # Priority 4: Stagnation escalation
    if stagnation_action in ("clarify_with_user", "navigate_direct", "skip_subgoal"):
        report.complexity = "complex"
        report.recommended_model = "120b"
        report.recommended_action = "click"
        report.routing_reason = f"Stagnation: {stagnation_action}"
        return

    if len(report.tried_and_failed) >= 2:
        report.complexity = "complex"
        report.recommended_model = "120b"
        report.recommended_action = "click"
        report.routing_reason = f"{len(report.tried_and_failed)} prior failures — need 120B"
        return

    # Priority 5: Candidate-based routing
    if not report.candidates:
        report.complexity = "complex"
        report.recommended_model = "120b"
        report.recommended_action = "scroll"
        report.routing_reason = "No matching candidates — 120B needed"
        return

    top = report.candidates[0]
    runner_up = report.candidates[1]["score"] if len(report.candidates) > 1 else 0
    gap = top["score"] - runner_up

    if top["score"] >= 25 and gap > 10:
        # Clear winner — skip LLM entirely
        report.complexity = "trivial"
        report.recommended_model = "shortcut"
        report.recommended_action = "click"
        report.routing_reason = f"Clear winner: '{top['text'][:30]}' (score={top['score']}, gap={gap})"
        return

    if top["score"] >= 10 and gap > 5:
        report.complexity = "simple"
        report.recommended_model = "8b"
        report.recommended_action = "click"
        report.routing_reason = f"Good match: '{top['text'][:30]}' (score={top['score']}, gap={gap})"
        return

    if gap <= 5 and len(report.candidates) >= 2:
        # Close race — 120B tie-breaks
        report.complexity = "complex"
        report.recommended_model = "120b"
        report.recommended_action = "click"
        report.routing_reason = (
            f"Tie: '{report.candidates[0]['text'][:20]}'({top['score']}) "
            f"vs '{report.candidates[1]['text'][:20]}'({runner_up})"
        )
        return

    # Default: 8B
    report.complexity = "simple"
    report.recommended_model = "8b"
    report.recommended_action = "click"
    report.routing_reason = f"Default 8B selection from {len(report.candidates)} candidates"

    # Override: sub-goal marked as requires_reasoning
    if requires_reasoning:
        report.recommended_model = "120b"
        report.complexity = "complex"
        report.routing_reason += " [override: requires_reasoning=true]"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _identify_page(url: str, title: str, active_states: dict = None) -> str:
    """Build a human-readable page identity string."""
    parts = []
    if title:
        parts.append(title)
    if url:
        # Extract path component
        from urllib.parse import urlparse
        path = urlparse(url).path.strip("/")
        if path:
            parts.append(f"/{path}")
    if active_states and active_states.get("activePage"):
        parts.append(f"[active: {active_states['activePage']}]")
    return " | ".join(parts) if parts else "Unknown page"


def _extract_words(text: str) -> set:
    """Extract meaningful words (>2 chars, stripped of punctuation)."""
    return {
        w.strip(".,;:!?()[]{}\"'")
        for w in text.lower().split()
        if len(w.strip(".,;:!?()[]{}\"'")) > 2
    }

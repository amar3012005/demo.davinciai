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

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx
from visual_copilot.constants import _CLICK_TAGS, _CLICK_ROLES

logger = logging.getLogger(__name__)
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

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


def _is_clickable_interactive_node(node: Any) -> bool:
    """Strict clickability gate: exists, interactive, not hidden, and clickable semantics."""
    if not node:
        return False
    if not bool(getattr(node, "interactive", False)):
        return False
    visible = getattr(node, "visible", True)
    if isinstance(visible, bool) and not visible:
        return False
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
    "for", "from", "of", "is", "are", "at",
}


def _goal_entity_and_qualifier_terms(goal: str) -> Tuple[List[str], List[str]]:
    g = (goal or "").strip().lower()
    if not g:
        return [], []
    g = re.sub(
        r"^(show me|find me|find|show|open|click|select|choose|go to|navigate to|take me to)\s+",
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


def _is_search_like_node(node: Any) -> bool:
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
    """Render vision analysis into execution-oriented plain text for the compound loop."""
    answer_visible = bool(hints.get("answer_visible"))
    best_target = str(hints.get("best_target_id") or "").strip()
    best_target_label = str(hints.get("best_target_label") or "").strip()
    if best_target and not best_target_label:
        best_target_label = _resolve_target_label(nodes, best_target)
    recommended_tool = str(hints.get("recommended_tool") or "").strip() or "read_page_content"
    evidence_summary = str(hints.get("evidence_summary") or "").strip()
    blocking_reason = str(hints.get("blocking_reason") or "").strip()
    candidate_targets = hints.get("candidate_targets") or []
    actions = hints.get("recommended_actions") or []

    lines: List[str] = []
    lines.append("Vision Strategic Brief:")
    lines.append("- Intent: choose fastest safe path to goal using currently visible controls.")
    lines.append(f"- Answer visible now: {'yes' if answer_visible else 'no'}")
    if evidence_summary:
        lines.append(f"- What is visible: {evidence_summary}")
    if blocking_reason:
        lines.append(f"- What is missing: {blocking_reason}")
    if best_target:
        if best_target_label:
            lines.append(f"- Best clickable target: {best_target_label} ({best_target})")
        else:
            lines.append(f"- Best clickable target: {best_target}")
    if candidate_targets:
        lines.append("- Ranked clickable options:")
        for t in sorted(candidate_targets, key=lambda x: int(x.get("priority", 99)))[:3]:
            tid = str(t.get("target_id") or "").strip()
            lbl = str(t.get("label") or "").strip()
            if not lbl:
                lbl = _resolve_target_label(nodes, tid)
            why = str(t.get("why") or "").strip()
            p = int(t.get("priority", 99))
            lines.append(f"  - P{p}: {lbl or 'unknown'} ({tid or 'n/a'}) — {why or 'viable route'}")
    lines.append(f"- Suggested primary tool: {recommended_tool}")
    if actions:
        lines.append("- Suggested next steps:")
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
                lines.append(f"  {idx}. click -> target={target_desc} force_click={force_click} ({why or 'navigate to relevant section'})")
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

    # Build grounded prompt — include DOM IDs so vision model can map visuals to known elements
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
        "You are TARA's strategic visual analyst for Last-Mile execution.\n"
        "Think like a senior operator: brief, precise, action-first.\n"
        "Do NOT return JSON. Return plain text in this exact structure:\n\n"
        "Vision Strategic Brief:\n"
        "Answer visible now: yes|no\n"
        "Visible evidence: <one sentence, <=22 words>\n"
        "Missing evidence: <one sentence, <=16 words>\n"
        "Primary tool: click_element|type_text|wait_for_ui|scroll_page|read_page_content|complete_mission\n"
        "Best target: <label> (id=t-xxxx)   [omit id if none]\n"
        "Candidate targets:\n"
        "1) <label> (id=t-xxxx) - <why>\n"
        "2) <label> (id=t-yyyy) - <why>\n"
        "3) <label> (id=t-zzzz) - <why>\n"
        "Next steps:\n"
        "1) <tool> -> <target label/id or n/a> | why: <reason>\n"
        "2) <tool> -> <target label/id or n/a> | why: <reason>\n"
        "3) <tool> -> <target label/id or n/a> | why: <reason>\n\n"
        "Rules:\n"
        "- Use ONLY IDs from KNOWN DOM element IDs when suggesting targets.\n"
        "- Prefer 2-3 candidate targets, not only one.\n"
        "- Prefer extraction (read_page_content) before more navigation when docs content is already visible.\n"
        "- If answer is visible now, set Primary tool=complete_mission and include concrete evidence text.\n"
        "- Keep output concise and strategic.\n\n"
        f"KNOWN DOM element IDs:\n{id_context}"
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
            logger.warning(f"👁️ VISION PROVIDER FAILED, trying direct Groq HTTP fallback: {e}")

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
                    "You are TARA's visual action planner. "
                    "Return concise strategic plain text (no JSON), grounded strictly to visible UI and provided DOM IDs."
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
        logger.error(f"👁️ VISION CALL FAILED (direct fallback): {e}")
        return f"System: Groq Vision call failed ({e}). Rely on the provided DOM text."


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
    logger.info(f"⚙️ EXECUTING TOOL: {tool_name} | Args: {args}")

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
        logger.info(f"📖 READ_PAGE_CONTENT: focus='{focus}' results={len(result_lines)}")
        return False, f"Page Content (focus: {focus or 'general'}):\n{content_text}", None

    if tool_name == "click_element":
        target_id = args.get("target_id", "")
        if not _is_valid_id(target_id, nodes):
            logger.warning(f"🛡️ GUARDRAIL: Hallucinated ID -> {target_id}")
            return (
                False,
                f"Error: ID '{target_id}' does not exist in the current DOM. "
                f"Please review the provided elements and use a valid ID, "
                f"or call request_vision to see the page visually.",
                None,
            )
        if excluded_ids and target_id in excluded_ids:
            logger.warning(f"🛡️ GUARDRAIL: Re-click blocked -> {target_id} (already clicked)")
            return (
                False,
                f"Error: ID '{target_id}' was already clicked in a previous step. "
                f"Do NOT click it again. Choose a different element or use type_text to search instead.",
                None,
            )

        # Strict clickability gate: reject non-clickable/hidden generic elements.
        if not _is_clickable_interactive_node(target_node):
            logger.warning(
                f"🛡️ CLICKABILITY_GATE rejected non-clickable target -> {target_id} "
                f"(tag='{getattr(target_node, 'tag', '') if target_node else ''}' "
                f"role='{getattr(target_node, 'role', '') if target_node else ''}')"
            )
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
        target_node = next((n for n in nodes if getattr(n, "id", "") == target_id), None)
        target_text = (getattr(target_node, "text", "") or "").lower() if target_node else ""
        target_has_entity = _text_has_terms(target_text, entity_terms, min_hits=1) if entity_terms else False
        target_is_search = _is_search_like_node(target_node) if target_node else False

        # Anti-recursion guard: block redundant sidebar/nav section reclicks.
        if _is_same_section_reclick_blocked(nodes, target_node):
            logger.warning(
                "🛡️ SECTION_RECLICK_GUARD blocked redundant section nav click "
                f"(target={target_id}, text='{(getattr(target_node, 'text', '') or '')[:40]}')"
            )
            return (
                False,
                "REJECTED: This sidebar/nav section already appears active on the current page. "
                "Do not reclick the same section. Use in-page controls (filters, tabs, dropdowns, date ranges) "
                "or read_page_content to extract the requested metric.",
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
                    logger.info(
                        "🎯 ENTITY_GUARD override click target "
                        f"{target_id} -> {best_id} for goal='{main_goal[:80]}'"
                    )
                    target_id = best_id
                    target_node = best_entity
                    target_text = (getattr(target_node, "text", "") or "").lower()
                    target_has_entity = True
            elif not target_is_search:
                qualifier_hint = " ".join(qualifier_terms[:2]) if qualifier_terms else "requested qualifier"
                logger.warning(
                    "🛡️ ENTITY_GUARD blocked generic click without visible entity/search target "
                    f"(target={target_id}, goal='{main_goal[:80]}')"
                )
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

        return False, f"Action 'click_element' on '{target_id}' queued. You may queue more actions, or call `wait_for_ui` to execute the queue.", {
            "type": "click",
            "target_id": target_id,
            "text": (getattr(target_node, "text", "") or "").strip()[:120] if target_node else "",
            "speech": args.get("why", "Clicking element."),
            "force_click": args.get("force_click", False),
        }

    elif tool_name == "type_text":
        target_id = args.get("target_id", "")
        text = args.get("text", "")
        press_enter = args.get("press_enter", True)
        if not _is_valid_id(target_id, nodes):
            logger.warning(f"🛡️ GUARDRAIL: Hallucinated ID -> {target_id}")
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
        seconds = args.get("seconds", 2)
        return True, "", {"type": "wait", "seconds": seconds, "speech": "Waiting briefly for the page to update."}

    elif tool_name == "request_vision":
        # ═══════════════════════════════════════════════════════════════════
        # 👁️ REAL VISION — Groq llama-4-scout-17b-16e-instruct
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
        logger.info(f"👁️ VISION REQUESTED: {enriched_reason}")


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
                    logger.info("👁️ Using fresh broker screenshot (primary)")
            except Exception as e:
                logger.error(f"👁️ Screenshot broker error: {e}")

        # 2) Fallback to screenshot from current request
        if not image_b64 and screenshot_b64:
            logger.info("👁️ Using pre-captured screenshot_b64 fallback")
            image_b64 = screenshot_b64

        # 3) Then try session-level cached screenshot saved by app.py
        if not image_b64 and app and session_id:
            try:
                cache = getattr(app.state, "latest_screenshots", None) or {}
                cached_b64 = cache.get(session_id)
                if cached_b64:
                    logger.info("👁️ Using app.state.latest_screenshots cache fallback")
                    image_b64 = cached_b64
            except Exception as e:
                logger.warning(f"👁️ Screenshot cache lookup failed: {e}")

        if image_b64:
            vision_result = await _call_groq_vision(
                image_b64=image_b64,
                reason=enriched_reason,
                nodes=nodes,
                app=app,
            )
            logger.info(f"👁️ VISION RAW_RESULT:\n{vision_result}\n")
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
                if brief_parts:
                    hint_suffix += "\n\n**Vision Brief:** " + " | ".join(brief_parts)
                actions = hints.get("recommended_actions") or []
                if actions:
                    rows = []
                    for idx, a in enumerate(actions[:3], start=1):
                        rows.append(
                            f"{idx}. {a.get('tool')} "
                            f"(target={a.get('target_id') or 'n/a'} "
                            f"text={a.get('text') or 'n/a'} "
                            f"press_enter={a.get('press_enter', False)} "
                            f"seconds={a.get('seconds', 2)} "
                            f"force_click={a.get('force_click', False)}) "
                            f"why={a.get('why') or ''}"
                        )
                    hint_suffix += "\n\n**Vision Action Plan (execute first if valid):**\n" + "\n".join(rows)
                vision_result += hint_suffix
                brief_text = _render_vision_reasoning_brief(hints, nodes)
                logger.info(
                    f"👁️ VISION HINTS: answer_visible={hints['answer_visible']} "
                    f"target={hints['best_target_id']} tool={hints['recommended_tool']} "
                    f"plan_steps={len(actions)}"
                )
                logger.info(f"👁️ VISION BRIEF:\n{brief_text}")
                # Use plain-text reasoning brief in compound loop context (avoid raw JSON blobs).
                vision_result = brief_text
            except Exception as hint_err:
                logger.warning(f"👁️ Vision hint parsing failed: {hint_err}")

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
        session_clicks = 0
        if session_id:
            session_clicks = len(get_click_tracker(session_id).click_history)

        # ── Intent-based completion gate: require UI interaction for action intents ──
        if (
            status == "success"
            and _requires_interaction_before_completion(main_goal, schema_action)
            and session_clicks == 0
        ):
            logger.warning(
                "🛡️ COMPLETION_GATE interaction_required_blocked: no click before completion | "
                f"goal='{main_goal[:80]}' action='{schema_action}' response='{response[:80]}'"
            )
            return (
                False,
                "REJECTED: This goal is action-oriented and requires at least one concrete UI interaction "
                "before completion. Do not complete from partial text-only evidence. "
                "Next step: click/select the most relevant result or control, then verify the updated content.",
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

            if (
                _requires_interaction_before_completion(main_goal, schema_action)
                and (
                    _weak_presence_claim(response)
                    or (entity_terms and not entity_click_seen and not dom_joint_evidence)
                    or (entity_terms and qualifier_terms and not (has_entity_in_answer and has_qualifier_in_answer) and not dom_joint_evidence)
                )
            ):
                logger.warning(
                    "🛡️ COMPLETION_GATE goal_not_achieved_blocked: "
                    f"entity_click_seen={entity_click_seen} dom_joint={dom_joint_evidence} "
                    f"entity_terms={entity_terms} qualifier_terms={qualifier_terms} "
                    f"response='{response[:90]}'"
                )
                return (
                    False,
                    "REJECTED: Goal not fully achieved yet. Do not complete from name/category presence only. "
                    "You must navigate to the target entity result and verify the requested qualifier "
                    "(for example color/outfit/category) in the visible result before complete_mission.",
                    None,
                )

        # ── Low-Confidence Interception: Reject and force re-exploration ──
        if status == "success" and answer_confidence == "low":
            logger.warning(
                f"🛡️ COMPLETION_GATE REJECTED low-confidence answer: "
                f"response='{response[:80]}' evidence='{evidence_refs[:50]}'"
            )
            return (
                False,
                "REJECTED: Your answer_confidence is 'low', which means you are NOT confident "
                "this is correct. Do NOT call complete_mission with low confidence. Instead:\n"
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
                logger.warning(
                    f"🛡️ COMPLETION_GATE metric_mismatch_suspect: "
                    f"response has money refs but no token refs, confidence={answer_confidence}"
                )
                return (
                    False,
                    "WARNING: Your answer contains dollar amounts/cost data but no token/usage metrics. "
                    "If the user's goal asks for tokens, usage, or counts — this is the WRONG metric. "
                    "Look for tabs or toggles that switch between 'Cost' and 'Usage/Tokens' views. "
                    "If the goal IS about cost/pricing, re-call complete_mission with confidence='high'.",
                    None,
                )

        # ── Entity Anchor Guard ──
        # Strict validation ensuring the target entity exists in the evidence
        if status == "success" and target_entity:
            entity_lower = target_entity.lower()
            evidence_lower = (evidence_refs + " " + response).lower()
            # Split into tokens if it's multiple words, checking for any match to allow fuzzy but strict
            entity_tokens = [t for t in re.findall(r"[a-z0-9]+", entity_lower) if len(t) > 2]
            
            # Require at least one significant entity keyword to be present
            if entity_tokens and not any(et in evidence_lower for et in entity_tokens):
                logger.warning(
                    f"🛡️ COMPLETION_GATE ENTITY_ANCHOR blocked: requested '{target_entity}' "
                    f"not found in evidence. Response: '{response[:80]}'"
                )
                return (
                    False,
                    f"REJECTED: Target entity '{target_entity}' not found in your evidence. "
                    f"You are looking at the wrong data or a placeholder. "
                    f"Use click_element or scroll_page to navigate to '{target_entity}' data before completing.",
                    None,
                )

        # Completion gate: warn if answer is suspiciously short/empty for success
        if status == "success" and len(response.strip()) < 15 and not evidence_refs:
            logger.warning(
                f"LAST_MILE_COMPLETION_GATE weak_completion: response='{response[:50]}' "
                f"confidence={answer_confidence} evidence_refs='{evidence_refs[:50]}'"
            )
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
                    logger.info(f"LAST_MILE_COMPLETION_GATE evidence_rescue len={len(response)}")

        return True, "", {
            "type": "answer",
            "speech": response,
            "text": response,
            "status": status,
            "evidence_refs": evidence_refs,
            "answer_confidence": answer_confidence,
        }

    else:
        return False, f"Error: Unknown tool '{tool_name}'.", None

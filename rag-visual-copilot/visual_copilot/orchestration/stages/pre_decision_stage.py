"""
pre_decision_stage.py

PURPOSE: Non-vision pre-decision gate using LiveGraph DOM context + quick Hive probe.
         Replaces the vision-based pre_router_stage for routing decisions.
         Runs on both /api/v1/get_map_hints and /api/v1/plan_next_step.

INPUTS:  user goal, current URL, compact LiveGraph context, quick Hive probe result
OUTPUTS: structured JSON deciding:
         - execution_mode: mission | last_mile
         - route: current_domain_hive | current_domain_last_mile

TARGET LATENCY: <=700ms (P50), <=1200ms (P95)
NO SCREENSHOTS — uses only DOM + Hive data.
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from visual_copilot.orchestration.stages.page_index import (
    is_domain_indexed as _page_index_available,
    traverse_index as _page_index_traverse,
    load_index as _page_index_load,
)

from visual_copilot.constants import (
    PRE_DECISION_MODEL,
    PRE_DECISION_TIMEOUT_MS,
    PRE_DECISION_MAX_COMPLETION_TOKENS,
    PRE_DECISION_REASONING_LOG_MAX_CHARS,
    PRE_DECISION_MIN_CONF,
    PRE_DECISION_CACHE_TTL_MS,
    PRE_DECISION_MAX_INTERACTIVES,
    PRE_DECISION_MAX_READABLE,
)

logger = logging.getLogger("vc.stage.pre_decision")

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# ── in-memory result cache (session+goal+domain → decision, 3s TTL) ──
_cache: Dict[str, Dict[str, Any]] = {}
_cache_ts: Dict[str, float] = {}


def _get_api_key() -> Optional[str]:
    return os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")


def _safe_json_parse(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _parse_confidence(raw: Any) -> float:
    if raw is None:
        return 0.0
    try:
        if isinstance(raw, str):
            s = raw.strip().replace("%", "")
            if not s:
                return 0.0
            val = float(s)
            if val > 1.0:
                val = val / 100.0
        else:
            val = float(raw)
        return max(0.0, min(1.0, val))
    except Exception:
        return 0.0


def _synthesize_fallback_sequence(goal: str, nodes: List[Any], hive_probe: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a deterministic pre-route sequence when LLM call/parsing fails.
    Keeps pipeline strategy-first and avoids dropping to MindReader.
    """
    quick_seq = list((hive_probe or {}).get("strategy_sequence") or [])
    probe_conf = float((hive_probe or {}).get("probe_confidence") or 0.0)

    if quick_seq:
        if not any(str(s).strip().upper().startswith("LAST_MILE:") for s in quick_seq):
            quick_seq.append(f"LAST_MILE: {goal}")
        return {
            "execution_mode": "mission",
            "route": "current_domain_hive",
            "confidence": max(0.65, min(0.9, probe_conf or 0.72)),
            "start_with_strategy": True,
            "recommended_strategy_order": quick_seq,
            "reason": "LLM failed; using quick-hive strategy sequence fallback.",
            "evidence": {
                "visible_goal_signals": 0,
                "obvious_controls": 1,
                "hive_support_score": probe_conf,
            },
        }

    goal_tokens = [t for t in (goal or "").lower().split() if len(t) > 2]
    best_click_label = ""
    docs_like_label = ""
    interactive_count = 0
    for n in nodes or []:
        if not getattr(n, "interactive", False):
            continue
        txt = (getattr(n, "text", "") or "").strip()
        if not txt:
            continue
        interactive_count += 1
        low = txt.lower()
        if not docs_like_label and any(k in low for k in ("docs", "documentation", "guide", "learn", "help")):
            docs_like_label = txt[:60]
        if not best_click_label and any(tok in low for tok in goal_tokens):
            best_click_label = txt[:60]
        if best_click_label and docs_like_label:
            break

    click_label = best_click_label or docs_like_label
    if click_label:
        return {
            "execution_mode": "mission",
            "route": "current_domain_hive",
            "confidence": 0.62,
            "start_with_strategy": True,
            "recommended_strategy_order": [f"Click {click_label}", f"LAST_MILE: {goal}"],
            "reason": "LLM failed; using DOM-derived clickable fallback sequence.",
            "evidence": {
                "visible_goal_signals": 0,
                "obvious_controls": 1 if interactive_count > 0 else 0,
                "hive_support_score": probe_conf,
            },
        }

    return {
        "execution_mode": "last_mile",
        "route": "current_domain_last_mile",
        "confidence": 0.55,
        "start_with_strategy": True,
        "recommended_strategy_order": [f"LAST_MILE: {goal}"],
        "reason": "LLM failed; no clear clickable controls, using direct last_mile fallback.",
        "evidence": {
            "visible_goal_signals": 0,
            "obvious_controls": 0,
            "hive_support_score": probe_conf,
        },
    }


# ═══════════════════════════════════════════════════════════
# LiveGraph Context Compression
# ═══════════════════════════════════════════════════════════

def _compress_dom_context(
    nodes: List[Any],
    current_url: str,
    max_interactives: int = PRE_DECISION_MAX_INTERACTIVES,
    max_readable: int = PRE_DECISION_MAX_READABLE,
) -> str:
    """
    Deterministic DOM summarizer for the reasoning model.
    Prioritizes: interactive controls, headings, nav/sidebar labels, URL path.
    Keeps token footprint bounded.
    """
    parts: List[str] = []

    # URL context
    try:
        parsed = urlparse(current_url if "://" in current_url else f"http://{current_url}")
        host = (parsed.netloc or "").replace("www.", "")
        path = parsed.path or "/"
        query_keys = [k for k in (parsed.query or "").split("&") if "=" in k]
        query_keys_str = ", ".join(k.split("=")[0] for k in query_keys[:5])
        parts.append(f"URL: {host}{path}" + (f" (params: {query_keys_str})" if query_keys_str else ""))
    except Exception:
        parts.append(f"URL: {current_url}")

    if not nodes:
        parts.append("DOM: (empty — page may be loading)")
        return "\n".join(parts)

    # Interactive controls (inputs, buttons, links)
    interactive = []
    headings = []
    nav_labels = []
    for n in nodes:
        tag = getattr(n, "tag", "") or ""
        text = (getattr(n, "text", "") or "")[:50]
        role = getattr(n, "role", "") or ""
        zone = getattr(n, "zone", "") or ""
        nid = getattr(n, "id", "") or ""
        is_interactive = getattr(n, "interactive", False)
        state = getattr(n, "state", "") or ""

        if is_interactive and len(interactive) < max_interactives:
            entry = f"  [{nid}] {tag}"
            if role:
                entry += f" role={role}"
            if zone:
                entry += f" zone={zone}"
            if text:
                entry += f" text='{text}'"
            if state:
                entry += f" state={state}"
            interactive.append(entry)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and text:
            headings.append(f"  <{tag}> {text}")
        elif zone in ("nav", "sidebar") and text and len(nav_labels) < 15:
            nav_labels.append(f"  [{zone}] {text}")

    if interactive:
        parts.append(f"INTERACTIVE ({len(interactive)}):")
        parts.extend(interactive[:max_interactives])
    if headings:
        parts.append(f"HEADINGS ({len(headings)}):")
        parts.extend(headings[:max_readable])
    if nav_labels:
        parts.append(f"NAV/SIDEBAR ({len(nav_labels)}):")
        parts.extend(nav_labels)

    # Readable text evidence (non-interactive, non-heading content)
    readable = []
    for n in nodes:
        tag = getattr(n, "tag", "") or ""
        text = (getattr(n, "text", "") or "").strip()
        zone = getattr(n, "zone", "") or ""
        is_interactive = getattr(n, "interactive", False)
        if (
            not is_interactive
            and tag not in ("h1", "h2", "h3", "h4", "h5", "h6")
            and zone not in ("nav", "sidebar")
            and text
            and len(text) > 8
            and len(readable) < max_readable
        ):
            readable.append(f"  [{zone or 'main'}] {text[:60]}")
    if readable:
        parts.append(f"CONTENT ({len(readable)}):")
        parts.extend(readable)

    return "\n".join(parts)


def _format_hive_probe(probe: Dict[str, Any]) -> str:
    """Format quick_probe result for the reasoning prompt."""
    if not probe or not probe.get("domain_indexed"):
        return "HIVE: domain not indexed (zero-shot mode)"

    parts = ["HIVE:"]
    matched_domain = str(probe.get("matched_domain") or "").strip()
    if matched_domain:
        parts.append(f"  matched_domain: {matched_domain}")
    if probe.get("has_strategy"):
        seq = probe.get("strategy_sequence", [])
        parts.append(f"  strategy: {' -> '.join(seq[:5])}")
    else:
        parts.append("  strategy: none")

    hints = probe.get("top_hints", [])
    if hints:
        parts.append(f"  hints ({len(hints)}):")
        for h in hints[:5]:
            parts.append(f"    - {h.get('text_pattern', h.get('selector', '?'))} ({h.get('element_type','?')} in {h.get('zone','?')})")
    else:
        parts.append("  hints: none")

    conf = probe.get("probe_confidence", 0.0)
    parts.append(f"  probe_confidence: {conf:.2f}")
    return "\n".join(parts)


def _tokenize_text(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2]


def _extract_hint_terms(hint: Dict[str, Any]) -> List[str]:
    fields = [
        str(hint.get("text_pattern", "") or ""),
        str(hint.get("selector", "") or ""),
        str(hint.get("element_type", "") or ""),
        str(hint.get("zone", "") or ""),
    ]
    raw = " ".join(fields)
    return _tokenize_text(raw)


def _derive_hive_visual_candidates(
    *,
    nodes: List[Any],
    hive_probe: Dict[str, Any],
    goal: str,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    hints = list((hive_probe or {}).get("top_hints") or [])
    if not hints:
        return []

    goal_terms = set(_tokenize_text(goal))
    candidates: List[Dict[str, Any]] = []
    seen = set()

    for n in nodes or []:
        if not getattr(n, "interactive", False):
            continue
        label = (getattr(n, "text", "") or "").strip()
        if not label:
            continue
        nid = str(getattr(n, "id", "") or "")
        zone = str(getattr(n, "zone", "") or "")
        tag = str(getattr(n, "tag", "") or "")
        key = (label.lower(), zone, tag)
        if key in seen:
            continue
        seen.add(key)

        label_terms = set(_tokenize_text(label))
        hint_score = 0.0
        matched_hints = 0
        for h in hints:
            hint_terms = set(_extract_hint_terms(h))
            if not hint_terms:
                continue
            overlap = len(label_terms & hint_terms)
            if overlap > 0:
                matched_hints += 1
                hint_score += overlap / max(len(hint_terms), 1)

        goal_overlap = len(label_terms & goal_terms)
        zone_boost = 0.25 if zone in {"nav", "sidebar"} else 0.0
        search_boost = 0.20 if any(t in label.lower() for t in ("search", "find", "filter")) else 0.0
        score = hint_score + (0.22 * goal_overlap) + zone_boost + search_boost
        if score <= 0:
            continue

        candidates.append(
            {
                "id": nid,
                "label": label[:80],
                "zone": zone,
                "tag": tag,
                "score": round(score, 3),
                "goal_overlap": goal_overlap,
                "matched_hints": matched_hints,
            }
        )

    candidates.sort(
        key=lambda c: (
            c["score"],
            c["matched_hints"],
            c["goal_overlap"],
            1 if c["zone"] in {"nav", "sidebar"} else 0,
        ),
        reverse=True,
    )
    return candidates[:limit]


def _format_visual_candidates(candidates: List[Dict[str, Any]]) -> str:
    if not candidates:
        return "HIVE_VISUAL_CANDIDATES: none"
    parts = [f"HIVE_VISUAL_CANDIDATES ({len(candidates)}):"]
    for c in candidates:
        parts.append(
            f"  - {c.get('label')} [id={c.get('id')} zone={c.get('zone')} score={c.get('score')} "
            f"hints={c.get('matched_hints')} goal={c.get('goal_overlap')}]"
        )
    return "\n".join(parts)


def _derive_dom_obvious_controls(goal: str, nodes: List[Any]) -> int:
    """
    Conservative DOM-based obvious-control detector.
    Used as a post-LLM floor so evidence does not underreport visible controls.
    """
    goal_l = (goal or "").lower()
    goal_terms = set(_tokenize_text(goal_l))
    nav_controls = 0
    search_controls = 0
    goal_overlap_controls = 0

    for n in nodes or []:
        if not getattr(n, "interactive", False):
            continue
        txt = (getattr(n, "text", "") or "").strip().lower()
        if not txt:
            continue
        zone = (getattr(n, "zone", "") or "").strip().lower()
        if zone in {"nav", "sidebar"}:
            nav_controls += 1
            if any(k in txt for k in ("pornstars", "docs", "categories", "tags", "channels", "people", "profiles")):
                goal_overlap_controls += 1
        if any(k in txt for k in ("search", "find", "filter")):
            search_controls += 1
        txt_terms = set(_tokenize_text(txt))
        if goal_terms and len(txt_terms & goal_terms) > 0:
            goal_overlap_controls += 1

    # At least 1 if nav/search surface exists and goal is non-trivial.
    if (nav_controls > 0 or search_controls > 0) and len(goal_terms) >= 2:
        return max(1, goal_overlap_controls)
    return max(0, goal_overlap_controls)


# ═══════════════════════════════════════════════════════════
# Reasoning Prompt
# ═══════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
You are TARA's pre-decision routing controller.
Given a user goal, current URL, DOM context, and Hive knowledge-base probe,
decide the routing path. Return ONLY valid JSON.
Keep reasoning concise and task-focused.
Speed and token discipline are mandatory:
- Keep internal reasoning under 600 tokens.
- Do not repeat observations.
- Prefer short decisive judgments over long analysis.

Decision policy:
1. execution_mode=last_mile: Use when the goal's target is ALREADY VISIBLE on the page
   OR there is an OBVIOUS interactive control (button, link, input) that directly matches the goal.
2. execution_mode=mission: Use when location is unclear, the goal requires navigating to a different
   section/page, or strategic guidance from the knowledge base is needed.
3. If uncertain, default to execution_mode=mission.
4. NEVER output cross-domain routing from this gate.
5. Keep route aligned with execution_mode:
   - mission -> current_domain_hive
   - last_mile -> current_domain_last_mile

CRITICAL STRATEGY RULES:
6. ALWAYS set start_with_strategy=true and ALWAYS provide a non-empty recommended_strategy_order.
7. OPTIMIZE STRATEGY: Look at the provided Hive strategy. Remove useless, redundant, or out-of-order subgoals. Only keep what is needed.
8. The LAST item in recommended_strategy_order MUST ALWAYS be a "LAST_MILE:" prefixed entry describing the final goal to achieve on the target page.
   Examples:
   - For mission: ["Click Docs link", "Click Prompt Caching", "LAST_MILE: Read about prompt caching"]
   - For last_mile (goal visible): ["LAST_MILE: Find the best reasoning model on this page"]
   - For last_mile (one click away): ["Click Prompt Caching link", "LAST_MILE: Read prompt caching documentation"]
9. NAVIGATION SUBGOAL RULES (THINK LOGICALLY):
   - Every intermediate subgoal (non-LAST_MILE) SHOULD generally be "Click [LABEL]".
   - KNOWLEDGE-BASE VETO: If the provided HIVE strategy or VISUAL HINTS describe specific navigation steps (labels) that are NOT in the current DOM list, you MUST still include them if they represent a known-good route. Trust the Knowledge Base over your current localized perception for navigation.
   - DATA-DRIVEN NAVIGATION: If Hive says "Click Dashboard -> Click Usage", but you only see "Dashboard", your sequence MUST be: ["Click Dashboard", "Click Usage", "LAST_MILE: ..."]. DO NOT fold "Usage" into LAST_MILE just because it is not visible yet.
   - FORBIDDEN in intermediate subgoals:
     a) Observation verbs: "Look at X", "Read X", "Observe X", "Scroll to X", "Find X", "Check X", "View X" — these are NOT clickable!
     b) URL path navigation: "Navigate to /path/here" — the router CANNOT do URL navigation! Only use "Click [label]".
   - If the Hive strategy references pages or elements not visible in the current DOM, fold those steps into the LAST_MILE entry.
   - GOOD: ["Click Dashboard", "Click Usage tab", "LAST_MILE: Find last month spend and token usage"]
   - BAD:  ["Click Dashboard", "Navigate to /dashboard/usage", "LAST_MILE: ..."]  ← URL path BREAKS the router!
   - BAD:  ["Click Dashboard", "Click Hannover", "LAST_MILE: ..."]  ← if 'Hannover' is not in the DOM lists!
10. HIVE SUPPORT PROTOCOLS (CRITICAL):
    - FULL OR PARTIAL HIVE (Hints exist): If you see VISUAL HINTS or STRATEGY in the prompt, USE THEM extensively to build a multi-step sequence of CLICKABLE steps leading toward the goal. Do NOT arbitrarily truncate your sequence if the hints tell you how to get there!
    - ABSOLUTE ZERO-SHOT (No strategy AND No hints): ONLY if there are exactly ZERO hints and ZERO strategy available, you MUST strongly lean towards execution_mode=last_mile and provide AT MOST ONE navigation subgoal before declaring the LAST_MILE entry. Do not hallucinate long paths you cannot see.
11. ACTION RELEVANCE RANKING (MANDATORY):
    - Before outputting sequence, rank candidate clickable actions by relevance using:
      a) goal-token overlap,
      b) URL/page context fit,
      c) Hive strategy sequence fit,
      d) Hive visual-hint fit.
    - Keep only the highest-value clicks; remove weak/redundant clicks.
12. PRIORITY HEURISTIC (MANDATORY):
    - Prefer controls in this order when reasonable:
      NAV/SIDEBAR controls > SEARCH controls (search input/button/icon) > main content links/buttons.
    - If strategy is absent but visual hints exist, derive your click plan from visual hints + NAV/SIDEBAR + SEARCH first.
13. SEARCH-FIRST LOGIC (WHEN GOAL IS A PERSON/ENTITY/ITEM NOT VISIBLE):
    - If target entity is not visible and a search control exists in INTERACTIVE/NAV/SIDEBAR, prioritize a search-related click as the first action.
    - Allowed intermediate subgoals are still click-only labels from provided DOM lists (no typing in pre-route).
14. HIVE-AWARE REWRITE:
    - If Hive strategy contains stale or non-visible labels, rewrite to nearest visible NAV/SIDEBAR/SEARCH clicks while preserving intent.
    - If Hive strategy is missing but hints exist, still build concrete click sequence from those hints.
15. HIVE CANDIDATE ENFORCEMENT:
    - If HIVE_VISUAL_CANDIDATES are provided and non-empty, your first click SHOULD come from the top-ranked candidates unless clearly irrelevant.
    - If Hive support score >= 0.40 and at least 2 relevant candidates exist, prefer 2 clickable subgoals before LAST_MILE when those labels are visible.
    - Do not collapse to a single generic click (e.g., just "Click Pics") when better ranked candidate labels are available.
16. QUALITY BAR:
    - The sequence must be minimal but strong: each click should increase probability of reaching the goal.
    - Avoid generic filler steps like clicking unrelated categories when a better NAV/SEARCH path exists.

Output schema (strict JSON, no extra text):
{
  "execution_mode": "mission | last_mile",
  "route": "current_domain_hive | current_domain_last_mile",
  "confidence": 0.0,
  "start_with_strategy": true,
  "recommended_strategy_order": [],
  "reason": "short reason including which priority signals were used (nav/sidebar/search/hive)",
  "evidence": {
    "visible_goal_signals": 0,
    "obvious_controls": 0,
    "hive_support_score": 0.0
  }
}

Respond in json format."""


_DECISION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "execution_mode",
        "route",
        "confidence",
        "start_with_strategy",
        "recommended_strategy_order",
        "reason",
        "evidence",
    ],
    "properties": {
        "execution_mode": {
            "type": "string",
            "enum": ["mission", "last_mile"],
        },
        "route": {
            "type": "string",
            "enum": ["current_domain_hive", "current_domain_last_mile"],
        },
        "confidence": {"type": "number"},
        "start_with_strategy": {"type": "boolean"},
        "recommended_strategy_order": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reason": {"type": "string"},
        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["visible_goal_signals", "obvious_controls", "hive_support_score"],
            "properties": {
                "visible_goal_signals": {"type": "integer"},
                "obvious_controls": {"type": "integer"},
                "hive_support_score": {"type": "number"},
            },
        },
    },
}


def _build_user_prompt(goal: str, dom_context: str, hive_context: str, visual_candidates: str) -> str:
    return (
        f"User goal: {goal}\n\n"
        f"{dom_context}\n\n"
        f"{hive_context}\n\n"
        f"{visual_candidates}\n\n"
        "Decide the route now."
    )


def _deterministic_fallback_decision(
    *,
    goal: str,
    nodes: List[Any],
    hive_probe: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    goal_l = (goal or "").lower()
    obvious_controls = 0
    visible_goal_signals = 0

    for n in nodes[:200]:
        txt = (getattr(n, "text", "") or "").lower()
        if not txt:
            continue
        if any(tok in txt for tok in [t for t in goal_l.split() if len(t) > 3][:4]):
            visible_goal_signals += 1
            if getattr(n, "interactive", False):
                obvious_controls += 1

    hive_support_score = float(hive_probe.get("probe_confidence") or 0.0) if hive_probe else 0.0
    has_strategy = bool(hive_probe.get("has_strategy")) if hive_probe else False
    raw_strategy = list((hive_probe or {}).get("strategy_sequence") or [])

    if obvious_controls > 0 or visible_goal_signals >= 2:
        execution_mode = "last_mile"
        route = "current_domain_last_mile"
        confidence = 0.74
    else:
        execution_mode = "mission"
        route = "current_domain_hive"
        confidence = 0.68 if has_strategy or hive_support_score > 0.0 else 0.60

    # Always ensure strategy ends with LAST_MILE:
    strategy = list(raw_strategy)
    has_last_mile_entry = any(s.startswith("LAST_MILE:") for s in strategy)
    if not has_last_mile_entry:
        strategy.append(f"LAST_MILE: {goal}")

    return {
        "execution_mode": execution_mode,
        "route": route,
        "confidence": confidence,
        "start_with_strategy": True,
        "recommended_strategy_order": strategy,
        "reason": reason[:180],
        "evidence": {
            "visible_goal_signals": int(visible_goal_signals),
            "obvious_controls": int(obvious_controls),
            "hive_support_score": float(hive_support_score),
        },
    }


async def _call_groq(payload: Dict[str, Any], api_key: str, timeout_s: float) -> Tuple[str, str]:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(
            GROQ_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        return message.get("content", ""), message.get("reasoning", "")


def _http_error_details(err: Exception, max_len: int = 4000) -> str:
    """Best-effort extraction of HTTP error status/body for debugging provider failures."""
    if isinstance(err, httpx.HTTPStatusError) and err.response is not None:
        status = err.response.status_code
        body = (err.response.text or "").strip()
        if len(body) > max_len:
            body = body[:max_len] + " ...<truncated>"
        return f"status={status} body={body}"
    return repr(err)


# ═══════════════════════════════════════════════════════════
# Main Gate
# ═══════════════════════════════════════════════════════════

async def run_pre_decision_gate(
    *,
    goal: str,
    current_url: str,
    nodes: List[Any],
    hive_probe: Dict[str, Any],
    session_id: str = "",
    logger: Any = logger,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Non-vision pre-decision gate using DOM context + Hive probe.

    Returns:
        (early_response, decision)
        - early_response: if non-None, pipeline should return immediately
          (currently always None — this gate only routes, never short-circuits)
        - decision: routing hint dict for downstream stages
    """
    gate_start = time.time()

    api_key = _get_api_key()
    if not api_key:
        logger.info("PRE_DECISION_GATE_FALLBACK: missing API key")
        return None, None

    # ── Cache check ──
    domain = ""
    try:
        parsed = urlparse(current_url if "://" in current_url else f"http://{current_url}")
        domain = (parsed.netloc or "").replace("www.", "").lower()
    except Exception:
        pass
    normalized_goal = (goal or "").strip().lower()[:120]
    cache_key = f"{session_id}:{normalized_goal}:{domain}"
    now = time.time()
    cache_ttl_s = PRE_DECISION_CACHE_TTL_MS / 1000.0
    cached_ts = _cache_ts.get(cache_key, 0.0)
    if now - cached_ts < cache_ttl_s and cache_key in _cache:
        decision = _cache[cache_key]
        logger.info(
            f"PRE_DECISION_GATE_RESULT (cached) route={decision.get('route')} "
            f"conf={decision.get('confidence', 0.0):.2f}"
        )
        return None, decision

    # ══════════════════════════════════════════════════════════════
    # ── PageIndex Fast-Path (Vectorless Reasoning) ──
    # If the domain has a site_map.json, use deterministic tree
    # traversal instead of LLM reasoning. This reduces latency
    # from ~700ms to <5ms while being more accurate.
    # ══════════════════════════════════════════════════════════════
    try:
        from visual_copilot.orchestration.stages.page_index import traverse_index_with_llm as _page_index_traverse_with_llm
        if _page_index_available(domain):
            idx_result = _page_index_traverse_with_llm(current_url, goal, nodes)
            if idx_result.get("has_index") and idx_result.get("confidence", 0.0) >= 0.5:
                strategy = idx_result.get("recommended_strategy_order", [])
                conf = idx_result.get("confidence", 0.85)
                target_node = idx_result.get("target_node") or {}
                current_node = idx_result.get("current_node") or {}

                # Determine execution mode from strategy length
                non_lm = [s for s in strategy if not str(s).upper().startswith("LAST_MILE:")]
                if len(non_lm) == 0:
                    # Already at target → last_mile
                    execution_mode = "last_mile"
                    route = "current_domain_last_mile"
                else:
                    execution_mode = "mission"
                    route = "current_domain_hive"

                decision = {
                    "execution_mode": execution_mode,
                    "route": route,
                    "confidence": conf,
                    "start_with_strategy": True,
                    "recommended_strategy_order": strategy,
                    "reason": f"PageIndex: {idx_result.get('reasoning', 'tree traversal')}",
                    "reasoning": idx_result.get("reasoning", ""),
                    "evidence": {
                        "visible_goal_signals": 1 if target_node else 0,
                        "obvious_controls": len(non_lm),
                        "hive_support_score": conf,
                    },
                    "page_index": {
                        "current_node": current_node.get("node_id", ""),
                        "target_node": target_node.get("node_id", ""),
                        "traverse_ms": idx_result.get("traverse_ms", 0),
                    },
                }

                _cache[cache_key] = decision
                _cache_ts[cache_key] = now

                latency_ms = int((time.time() - gate_start) * 1000)
                logger.info(
                    f"PRE_DECISION_GATE_RESULT (PageIndex) session={session_id} "
                    f"mode={execution_mode} route={route} conf={conf:.2f} "
                    f"strategy_len={len(strategy)} "
                    f"current={current_node.get('node_id', 'none')} "
                    f"target={target_node.get('node_id', 'none')} "
                    f"traverse_ms={idx_result.get('traverse_ms', 0)} "
                    f"total_ms={latency_ms}"
                )
                return None, decision
    except Exception as idx_err:
        logger.warning(f"PRE_DECISION_GATE: PageIndex fast-path failed: {idx_err}")
        # Fall through to legacy Hive + LLM path

    # ══════════════════════════════════════════════════════════════
    # ── Legacy Path: Hive Probe + LLM Reasoning ──
    # ══════════════════════════════════════════════════════════════

    # ── Build compressed context ──
    dom_context = _compress_dom_context(nodes, current_url)
    hive_context = _format_hive_probe(hive_probe)
    visual_candidates_data = _derive_hive_visual_candidates(nodes=nodes, hive_probe=hive_probe, goal=goal)
    visual_candidates = _format_visual_candidates(visual_candidates_data)

    # ── Call reasoning model ──
    user_prompt = _build_user_prompt(goal, dom_context, hive_context, visual_candidates)
    payload = {
        "model": PRE_DECISION_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_completion_tokens": PRE_DECISION_MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "pre_decision_result",
                "schema": _DECISION_JSON_SCHEMA,
                "strict": True,
            },
        },
    }

    # Fallback payload if strict structured output validation fails.
    fallback_payload = {
        "model": PRE_DECISION_MODEL,
        "messages": [
            {
                "role": "system",
                "content": _SYSTEM_PROMPT
                + "\nReturn ONLY a single JSON object. No prose, no markdown. Keep reasoning concise and under 600 tokens.",
            },
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_completion_tokens": PRE_DECISION_MAX_COMPLETION_TOKENS,
    }

    timeout_s = PRE_DECISION_TIMEOUT_MS / 1000.0
    # Allow slightly more network time than the ideal target to reduce false fallbacks.
    http_timeout = max(timeout_s, 3.0)

    try:
        logger.info(f"PRE_DECISION_GATE_START session={session_id} domain={domain}")
        llm_start = time.time()
        try:
            content, reasoning = await _call_groq(payload, api_key, http_timeout)
        except httpx.HTTPStatusError as e:
            # Some models can intermittently fail strict JSON validation.
            # Retry once without response_format and parse robustly.
            if e.response is not None and e.response.status_code == 400:
                logger.warning(
                    "PRE_DECISION_GATE: strict call 400 details: "
                    f"{_http_error_details(e)}"
                )
                logger.warning(
                    "PRE_DECISION_GATE: strict call failed with 400, "
                    "retrying with non-strict JSON-only prompt"
                )
                try:
                    content, reasoning = await _call_groq(fallback_payload, api_key, http_timeout)
                except Exception as fallback_err:
                    logger.warning(
                        "PRE_DECISION_GATE: fallback call failed details: "
                        f"{_http_error_details(fallback_err)}"
                    )
                    raise
            else:
                raise
        
        if reasoning:
            logger.info(f"PRE_DECISION_GATE_REASONING:\n{reasoning}\n")
            
        data = _safe_json_parse(content)
        llm_ms = int((time.time() - llm_start) * 1000)
    except Exception as e:
        latency_ms = int((time.time() - gate_start) * 1000)
        fallback = _synthesize_fallback_sequence(goal, nodes, hive_probe or {})
        _cache[cache_key] = fallback
        _cache_ts[cache_key] = now
        logger.warning(
            f"PRE_DECISION_GATE_FALLBACK: {repr(e)} ({latency_ms}ms) "
            f"route={fallback['route']} conf={fallback['confidence']:.2f} "
            f"seq_len={len(fallback.get('recommended_strategy_order') or [])}"
        )
        return None, fallback

    # ── Parse decision ──
    execution_mode = str(data.get("execution_mode", "")).strip().lower()
    if execution_mode not in {"mission", "last_mile"}:
        execution_mode = ""

    route = str(data.get("route", "")).strip().lower()
    allowed_routes = {"current_domain_hive", "current_domain_last_mile"}
    if execution_mode == "last_mile":
        route = "current_domain_last_mile"
    elif execution_mode == "mission":
        route = "current_domain_hive"
    elif route not in allowed_routes:
        route = "current_domain_hive"
        execution_mode = "mission"

    if not execution_mode:
        execution_mode = "last_mile" if route == "current_domain_last_mile" else "mission"

    confidence = _parse_confidence(data.get("confidence"))
    reason = str(data.get("reason") or "").strip()[:180]
    start_with_strategy = True  # Always true — pre-route always provides strategy
    recommended_strategy_order = data.get("recommended_strategy_order") or []
    evidence = data.get("evidence") or {}

    # Ensure LAST_MILE: is always the final entry
    has_last_mile = any(str(s).strip().upper().startswith("LAST_MILE:") for s in recommended_strategy_order)
    if not has_last_mile:
        recommended_strategy_order.append(f"LAST_MILE: {goal}")

    visible_goal_signals = int(evidence.get("visible_goal_signals", 0))
    obvious_controls = int(evidence.get("obvious_controls", 0))
    hive_support_score = float(evidence.get("hive_support_score", 0.0))

    # Post-LLM evidence floor from real DOM to avoid under-reporting obvious controls.
    dom_obvious_controls = _derive_dom_obvious_controls(goal, nodes)
    if dom_obvious_controls > obvious_controls:
        obvious_controls = dom_obvious_controls

    # Strategic repair: if Hive support exists + ranked visual candidates exist,
    # avoid collapsing to a single generic nav click before LAST_MILE.
    non_last_mile_steps = [
        str(s).strip()
        for s in recommended_strategy_order
        if not str(s).strip().upper().startswith("LAST_MILE:")
    ]
    # --- Deterministic Overrides Disabled ---
    # We now trust the LLM's strategic reasoning and optimized sequence
    # from the prompt as-is, following the principle of "Intelligence over Heuristics".


    # Calibrate zero-confidence responses
    if confidence <= 0.0:
        if visible_goal_signals > 0 or obvious_controls > 0:
            confidence = 0.85
        elif route == "current_domain_hive" and reason:
            confidence = 0.70
        else:
            confidence = 0.55

    decision = {
        "execution_mode": execution_mode,
        "route": route,
        "confidence": confidence,
        "start_with_strategy": start_with_strategy,
        "recommended_strategy_order": recommended_strategy_order,
        "reason": reason,
        "reasoning": reasoning or "",  # Store full reasoning for downstream use
        "evidence": {
            "visible_goal_signals": visible_goal_signals,
            "obvious_controls": obvious_controls,
            "hive_support_score": hive_support_score,
        },
    }

    # ── Cache ──
    _cache[cache_key] = decision
    _cache_ts[cache_key] = now

    latency_ms = int((time.time() - gate_start) * 1000)
    logger.info(
        f"PRE_DECISION_GATE_RESULT session={session_id} mode={execution_mode} route={route} "
        f"conf={confidence:.2f} strategy={start_with_strategy} "
        f"visible={visible_goal_signals} controls={obvious_controls} "
        f"hive_score={hive_support_score:.2f} reason={reason or 'n/a'} "
        f"PRE_DECISION_GATE_LLM_MS={llm_ms} PRE_DECISION_GATE_LATENCY_MS={latency_ms}"
    )

    return None, decision

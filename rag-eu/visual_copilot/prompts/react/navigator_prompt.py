"""
react_navigator.py

PURPOSE: Robust ReAct prompt for mission_brain._react_generate_subgoal().
         Single-step search (type+enter), human-readable history,
         clear stopping/clarity rules, anti-loop protection.
"""

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


REACT_NAVIGATOR_PROMPT = """You are TARA, a precise web navigation agent.

=== MISSION ===
Goal: "{main_goal}"
Target: "{goal_essence}"
Action: {action_type}
Domain: {domain}
Step: {step_number}
{navigation_hint_section}

=== WHAT YOU ALREADY DID (DO NOT REPEAT) ===
{action_history}

=== STRATEGY CONTEXT ===
Current Subgoal: "{current_subgoal}"
Previous Subgoal: "{previous_subgoal}"
Remaining Subgoals:
{remaining_subgoals}

=== LIVE DOM (interactive elements on screen) ===
{compressed_dom}

=== HIGH-CONFIDENCE GOAL-MATCHED INTERACTABLES ===
{focus_interactables}

=== READABLE EVIDENCE (main/content text excerpts) ===
{focus_evidence}

=== OPEN MENUS / DROPDOWNS (if detected) ===
{dropdown_context}

=== RULES ===

RULE 1 — STOP IF DONE:
If the target "{goal_essence}" is VISIBLE in the DOM text (multiple results, not just a dropdown hint), set "is_done": true. Do NOT click or search further.

RULE 2 — SEARCH = TYPE + ENTER (ONE STEP):
If you need to search, find an <input> or searchbox and type directly:
  next_step: "Type 'keywords' in search [ID: xxx]"
The system will type AND press Enter automatically. NEVER generate a separate "click search button" step.
Target the <input> element, NOT a <button>.

RULE 3 — NO REPEATS:
Look at WHAT YOU ALREADY DID above. If you already typed into an input, do NOT type into it again. If you already clicked something, do NOT click it again. Find a DIFFERENT action.

RULE 4 — AFTER SEARCH, LOOK FOR FILTERS:
If PREVIOUS ACTIONS show you already searched (typed into search), the page should now show results.
  - If results match the goal exactly → "is_done": true
  - If results are broad and need refinement → look for filter/sort UI controls and click them
  - If DOM is unchanged → the search may not have submitted. Try clicking a "Search" or "Go" button.

RULE 5 — PROGRESSIVE SEARCH (BROAD TO NARROW):
NEVER type the user's full verbose goal into a search box. This is universal across ALL website types:
  - Retail:  "most expensive white nike shoe"       → Type "white nike shoe" in search
  - Video:   "latest HD videos of Nicole Aniston"   → Type "Nicole Aniston" in search
  - Docs:    "advanced rate limiting configuration"  → Type "rate limiting" in search
  - Food:    "best Italian restaurants near downtown"→ Type "Italian restaurant" in search
Extract ONLY the core noun/entity. Apply adjectives (expensive, latest, HD, best) using UI filters AFTER searching.

RULE 6 — REFINEMENT & FILTERING (AFTER SEARCHING):
Once search results are loaded (check History), scan the DOM for refinement controls:
  - Sort dropdowns: "Price: High to Low", "Newest First", "Top Rated"
  - Category filters: "Documentation", "HD Only", "Open Now"
  - Facets or checkboxes that narrow the results
Click these to progressively narrow toward the user's specific modifier (expensive, latest, best, etc.).

RULE 7 — ASK FOR CLARITY:
If multiple elements match the goal equally well and you cannot decide, set:
  "decide": "clarify", "next_step": "Ask user: which one did you mean?"

RULE 8 — GIVE UP:
If the DOM has no search bar AND no relevant links/content, set "is_impossible": true.

RULE 9 — EXACT IDs:
Always include [ID: xxx] from the DOM list. Never invent IDs.

RULE 10 — EXPLICIT LABEL PRIORITY:
If the subgoal contains an explicit label like "Docs", "Pricing", or "Reasoning", first prefer an exact text match for that label before semantically similar alternatives.

RULE 11 — GALLERY OPENING:
If the step asks to "open gallery" / "open images" / "open pics", prefer clicking image thumbnails/cards in main content.
Avoid generic nav links like "Random Gallery", "Tags", or "Categories" unless no image-card target exists.

RULE 12 — DROPDOWN CONTEXT LOCK:
If a nav/developer menu was just clicked and dropdown items are visible, pick targets from that same dropdown/menu context first.
Do NOT jump to same-text items in unrelated main-content cards.

RULE 13 — SURROUNDINGS AWARENESS:
Before choosing next_step, verify:
- What changed after last action?
- Is current target in the same area (zone/menu context) as intended subgoal?
- Is this action strategically consistent with previous and current subgoals?

RULE 14 — NO HALLUCINATION:
Use only IDs present in LIVE DOM. Never invent IDs, selectors, or hidden elements.
If confidence is low between two same-text targets, choose the one in nav/sidebar/dropdown context for navigation tasks.

RULE 15 — ACTION/TAG COMPATIBILITY:
- If action is type_text, target MUST be input/textarea/searchbox.
- If action is click/select, target MUST be clickable (a/button/tab/menuitem/link).
- If your chosen target is not compatible, choose a different target or return impossible.

RULE 16 — LOOP AVOIDANCE:
Do not repeat the same click/type target seen in WHAT YOU ALREADY DID unless the page clearly changed and the repeat is required.

=== THINK (fill all 3) ===
1. SCAN: Is "{goal_essence}" visible? List matching IDs or say "not found".
2. DECIDE: done / search / filter / click / clarify / impossible
3. ACT: The exact instruction (or "done" / "impossible").

=== OUTPUT (strict JSON, SHORT values) ===
{{
    "scan": "found/NOT found",
    "decide": "done/search/filter/click/clarify/impossible",
    "next_step": "Type 'X' in search [ID: xxx]",
    "action": "click/type_text/select/wait/answer/none",
    "target_id": "ID or empty",
    "text": "text to type if action=type_text else empty",
    "press_enter": false,
    "why": "brief reason",
    "confidence": "high/medium/low",
    "is_done": false,
    "is_impossible": false
}}"""


def build_react_prompt(
    schema: Any,
    nodes: list,
    mission: Optional[Any] = None,
    app: Optional[Any] = None
) -> str:
    """Build the ReAct navigator prompt from schema, DOM nodes, and mission state."""
    goal_essence = (getattr(schema, "target_entity", "") or "").lower()
    _noise_words = {'the', 'and', 'for', 'then', 'show', 'find', 'get', 'see', 'with', 'videos', 'pics', 'images'}

    # --- Compress DOM: interactive nodes only, with zone/state context ---
    interactive = [
        n for n in nodes
        if n.interactive and (n.text or n.tag in ('input', 'textarea') or getattr(n, 'role', '') == 'searchbox')
    ]

    def _sort_key(n):
        # Tier 0: search inputs / text areas (most critical for typing)
        if n.tag in ('input', 'textarea') or getattr(n, 'role', '') == 'searchbox':
            return 0
        # Tier 1: images in the main content zone (so TARA can "see" pictures)
        if n.tag == 'img' and getattr(n, 'zone', '') == 'main':
            return 1
        # Tier 2: all other interactive elements
        return 2

    zone_priority = {'nav': 0, 'sidebar': 0, 'header': 0, 'menu': 0, 'main': 1, 'footer': 2}
    interactive.sort(key=lambda n: (_sort_key(n), zone_priority.get(getattr(n, 'zone', ''), 3)))
    interactive = interactive[:80]

    compressed_dom = "\n".join([
        f"[ID: {n.id}] tag={n.tag} zone={getattr(n, 'zone', '')} state={getattr(n, 'state', '')} "
        f"aria_expanded={getattr(n, 'aria_expanded', '')} parent={getattr(n, 'parent_id', '')} "
        f"text='{(n.text or '')[:50]}'"
        for n in interactive
    ]) or "(no interactive elements found)"

    # --- Focus interactables/evidence like last-mile planner ---
    goal_terms = set(goal_essence.split())
    goal_terms = {t for t in goal_terms if len(t) > 2 and t not in _noise_words}

    scored_interactables = []
    for n in interactive:
        blob = " ".join([
            getattr(n, "text", "") or "",
            getattr(n, "id", "") or "",
            getattr(n, "placeholder", "") or "",
            getattr(n, "name", "") or "",
            getattr(n, "role", "") or "",
            getattr(n, "zone", "") or "",
        ]).lower()
        terms = set(blob.split())
        overlap = len(terms & goal_terms)
        if overlap <= 0:
            continue
        zone = (getattr(n, "zone", "") or "").lower()
        zone_boost = 1 if zone in {"main", "content", "nav", "sidebar"} else 0
        scored_interactables.append((overlap * 2 + zone_boost, overlap, n))
    scored_interactables.sort(key=lambda x: (-x[0], -x[1], getattr(x[2], "id", "")))
    focus_interactables = "\n".join([
        f"[ID: {n.id}] tag={n.tag} zone={getattr(n, 'zone', '')} text='{(n.text or '')[:60]}' overlap={ov}"
        for _, ov, n in scored_interactables[:25]
    ]) or "None"

    readable_nodes = [
        n for n in nodes
        if getattr(n, "text", None) and len((getattr(n, "text", "") or "").strip()) >= 8 and getattr(n, "zone", "") in {"main", "content"}
    ][:120]
    scored_evidence = []
    for n in readable_nodes:
        txt = (getattr(n, "text", "") or "").strip()
        if not txt:
            continue
        terms = set(txt.lower().split())
        overlap = len(terms & goal_terms)
        if overlap <= 0:
            continue
        scored_evidence.append((overlap, txt))
    scored_evidence.sort(key=lambda x: (-x[0], len(x[1])))
    focus_evidence = "\n".join([txt[:200] for _, txt in scored_evidence[:12]]) or "None"
    # --- Content summary: show visible text that matches the goal ---
    # This lets ReAct see if the goal is already on screen
    goal_kws = [w for w in goal_essence.split() if len(w) > 2 and w not in _noise_words]
    if goal_kws:
        content_matches = [
            n for n in nodes
            if n.text and len(n.text) > 10
            and n.zone == 'main'
            and any(kw in n.text.lower() for kw in goal_kws)
        ]
        if content_matches:
            content_summary = "\n".join([
                f"[ID: {n.id}] '{n.text[:50]}'"
                for n in content_matches[:10]
            ])
            compressed_dom += f"\n\n=== CONTENT ALREADY ON SCREEN (matching '{goal_essence}') ===\n{content_summary}"

    # --- Action history: human-readable format with typed text surfaced ---
    history_lines: List[str] = []
    if mission and hasattr(mission, 'action_history') and mission.action_history:
        for entry in mission.action_history[-5:]:
            if isinstance(entry, str) and ":" in entry:
                parts = entry.split(":", 2)  # Split into up to 3 parts: action, target, typed_text
                action = parts[0].upper()
                target = parts[1].strip() if len(parts) > 1 else ""
                typed_text = parts[2].strip() if len(parts) > 2 else ""

                if action == "TYPE" and typed_text:
                    history_lines.append(f"- TYPED '{typed_text}' into [ID: {target}] (search was submitted, results loading)")
                elif action == "TYPE":
                    history_lines.append(f"- TYPED into [ID: {target}] (search was submitted)")
                else:
                    history_lines.append(f"- {action} on element [ID: {target}]")
            else:
                history_lines.append(f"- {entry}")
    elif hasattr(schema, 'action_history') and schema.action_history:
        for entry in schema.action_history[-5:]:
            history_lines.append(f"- {entry}")

    action_history = "\n".join(history_lines) if history_lines else "None yet (this is the first step)"

    # --- Strategy/subgoal context ---
    current_subgoal = ""
    previous_subgoal = "None"
    remaining_subgoals = "None"
    if mission and hasattr(mission, 'subgoals') and mission.subgoals:
        idx = int(getattr(mission, 'current_subgoal_index', 0) or 0)
        if idx < len(mission.subgoals):
            current_subgoal = mission.subgoals[idx]
        if idx - 1 >= 0 and idx - 1 < len(mission.subgoals):
            previous_subgoal = mission.subgoals[idx - 1]
        tail = mission.subgoals[idx + 1:] if idx + 1 < len(mission.subgoals) else []
        if tail:
            remaining_subgoals = "\n".join([f"- {s}" for s in tail[:5]])
    if not current_subgoal:
        current_subgoal = (getattr(schema, 'first_subgoal', None) or schema.target_entity or "").strip()

    # --- Dropdown context extraction ---
    dropdown_nodes = []
    for n in nodes:
        if not getattr(n, 'interactive', False):
            continue
        zone = (getattr(n, 'zone', '') or '').lower()
        state = (getattr(n, 'state', '') or '').lower()
        aria_expanded = str(getattr(n, 'aria_expanded', '') or '').lower()
        txt = (getattr(n, 'text', '') or '').strip()
        if zone not in {'nav', 'sidebar', 'header', 'menu'}:
            continue
        if ('open' in state or 'expanded' in state or aria_expanded == 'true' or txt):
            dropdown_nodes.append(n)
    dropdown_context = "\n".join([
        f"[ID: {n.id}] zone={getattr(n, 'zone', '')} state={getattr(n, 'state', '')} "
        f"parent={getattr(n, 'parent_id', '')} text='{(getattr(n, 'text', '') or '')[:50]}'"
        for n in dropdown_nodes[:20]
    ]) or "None detected"

    # --- Goal fields ---
    main_goal = getattr(schema, 'raw_utterance', None) or schema.target_entity
    goal_essence = schema.target_entity
    step_number = 1
    if mission and hasattr(mission, 'current_subgoal_index'):
        step_number = mission.current_subgoal_index + 1
    domain = getattr(schema, 'domain', 'unknown')
    action_type = schema.action.value if hasattr(schema.action, 'value') else str(schema.action)

    # --- Navigation hint from Mind Reader ---
    nav_hint = getattr(schema, 'navigation_hint', '') or ''
    navigation_hint_section = f"Navigation Hint: Look for '{nav_hint}' links/sections in the DOM" if nav_hint else ""

    prompt = REACT_NAVIGATOR_PROMPT.format(
        main_goal=main_goal,
        goal_essence=goal_essence,
        action_type=action_type,
        domain=domain,
        step_number=step_number,
        navigation_hint_section=navigation_hint_section,
        action_history=action_history,
        current_subgoal=current_subgoal,
        previous_subgoal=previous_subgoal,
        remaining_subgoals=remaining_subgoals,
        compressed_dom=compressed_dom,
        focus_interactables=focus_interactables,
        focus_evidence=focus_evidence,
        dropdown_context=dropdown_context,
    )

    logger.debug(f"ReAct prompt built: {len(prompt)} chars, {len(interactive)} DOM nodes")
    return prompt

# TARA v5.1 — The Definitive Architecture
## Research-Backed, Obstacle-Aware, Hybrid Model

---

## WHY EVERY PREVIOUS APPROACH WAS INCOMPLETE

### v4 (3-call OODA chain): 
Too slow. 3 LLM calls × 4 steps = 12 calls per mission. 
The 20B reasoning model burned 4096 tokens on CoT and produced empty JSON.

### v5 (Single 120B REASON call): 
Better, but still 1.5-3s per step. And "medium" reasoning effort 
may miss obstacles on complex sites.

### v5.1-8B (Pure Python + 8B shortlist): 
Fast but FRAGILE. Python pre-computation can't handle:
- Hidden dropdown options (not in DOM until clicked)
- Dynamic forms (fields appear after selections)
- Modal obstacles blocking the real target
- Multi-step interactions (open menu → select → confirm)
- Ambiguous elements requiring contextual understanding

### What ACTUALLY works (from research):

Agent-E (73.2% SOTA): Planner LLM + Navigator LLM + DOM distillation
WebRL/WebAgent-R1 (44.8%): RL-fine-tuned 8B > prompted GPT-4
Reflexion: Verbal self-correction with episodic memory (max 3)
AgentOccam: Pruned action/observation space → better than complex agents

**The universal finding: Separation of PLANNING from EXECUTION, 
with distilled observations, beats everything else.**

---

## THE ARCHITECTURE: THREE LAYERS, TWO MODELS, ONE LOOP

```
┌─────────────────────────────────────────────────────────────┐
│                    USER: "Book the cheapest flight"          │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  LAYER 1: PLANNER (120B, called ONCE per mission)    │   │
│  │                                                      │   │
│  │  "I need to: 1) Find the flight search form          │   │
│  │   2) Fill in origin/destination/date                  │   │
│  │   3) Click search  4) Sort by price  5) Select first" │   │
│  │                                                      │   │
│  │  Output: GoalPlan with 5 sub-goals + success signals │   │
│  └──────────────────────────┬───────────────────────────┘   │
│                             │                               │
│                    ┌────────▼────────┐                      │
│                    │   STEP LOOP     │                      │
│                    └────────┬────────┘                      │
│                             │                               │
│  ┌──────────────────────────▼───────────────────────────┐   │
│  │  LAYER 2: PYTHON PRE-COMPUTE (0ms per step)          │   │
│  │                                                      │   │
│  │  • Score elements against current sub-goal           │   │
│  │  • Detect obstacles (modals, overlays, loading)      │   │
│  │  • Check if answer is visible in DOM                 │   │
│  │  • Block already-tried elements                      │   │
│  │  • Classify page state (form? nav? data? error?)     │   │
│  │  • Pre-decide action type (click/type/scroll/answer) │   │
│  │                                                      │   │
│  │  Output: DetectiveReport with ranked shortlist        │   │
│  └──────────────────────────┬───────────────────────────┘   │
│                             │                               │
│              ┌──────────────▼──────────────┐                │
│              │  DECISION ROUTER            │                │
│              │                             │                │
│              │  Clear winner (score gap    │                │
│              │  > 10, no obstacles)?       │                │
│              │  ├─ YES → SHORTCUT (0ms)    │                │
│              │  │                          │                │
│              │  Obstacle detected?         │                │
│              │  ├─ YES → 120B (needs       │                │
│              │  │        reasoning)         │                │
│              │  │                          │                │
│              │  Ambiguous (top 2 within    │                │
│              │  5 points)?                 │                │
│              │  ├─ YES → 120B (tie-break)  │                │
│              │  │                          │                │
│              │  Simple selection?          │                │
│              │  └─ YES → 8B (fast pick)    │                │
│              └────────────┬───────────────┘                 │
│                           │                                 │
│  ┌────────────────────────▼─────────────────────────────┐   │
│  │  LAYER 3: NAVIGATOR (8B or 120B, per decision above) │   │
│  │                                                      │   │
│  │  Receives: Sub-goal + 5-8 ranked candidates +        │   │
│  │           obstacle info + reflexion corrections      │   │
│  │  Returns:  {action, target_id, confidence, speech}   │   │
│  └──────────────────────────┬───────────────────────────┘   │
│                             │                               │
│  ┌──────────────────────────▼───────────────────────────┐   │
│  │  VALIDATION (Python, deterministic)                  │   │
│  │                                                      │   │
│  │  URL changed? Nav changed? New elements? Modal gone? │   │
│  │  ├─ YES → Advance sub-goal, loop                    │   │
│  │  └─ NO  → Reflexion entry + retry (max 3)           │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## WHEN EACH MODEL IS CALLED (THE KEY INSIGHT)

The critical question you're asking is: when does the 8B handle it,
and when do you NEED the 120B? Here's the decision matrix:

### 8B-INSTANT (200-400ms) — The Fast Navigator
Used when the step is SIMPLE SELECTION:
- "Click the Dashboard link" → 5 nav links, one says "Dashboard"
- "Type 'NYC' in the search box" → 1 input field visible
- "Click the first search result" → results list visible
- Format answer from pre-found data → data already extracted

Characteristics: clear target, no ambiguity, no obstacles.
Expected: ~60-70% of all steps in a typical mission.

### 120B-REASONING (1-2s, medium effort) — The Problem Solver  
Used when the step requires CONTEXTUAL UNDERSTANDING:
- Obstacle blocking: modal, cookie banner, CAPTCHA prompt
- Dropdown interaction: need to open menu, then pick option
- Form logic: "which field do I fill next? what value?"
- Ambiguous elements: top 2 candidates within 5 score points
- After failure: reflexion says "try a different approach"
- Dynamic content: page loaded new elements, need to re-assess

Characteristics: ambiguity, obstacles, multi-step interactions.
Expected: ~20-30% of steps.

### PYTHON SHORTCUT (0ms) — No LLM At All
Used when the answer is deterministic:
- Clear winner: score gap > 10, no obstacles
- Scroll action: no matching elements visible
- Answer visible: data found in DOM tables
- Already on target page: URL matches sub-goal

Expected: ~10-20% of steps.

### 120B-PLANNER (called ONCE, 1-3s) — The Strategist
Called at mission start only. Decomposes goal into sub-goals.
This is your existing GOAL_DECOMPOSITION — it stays.

---

## LAYER 1: THE PLANNER (Stays Almost Identical to v4)

Your existing GOAL_DECOMPOSITION_PROMPT is actually good.
The sub-goal system is ESSENTIAL — don't remove it.

### Why sub-goals matter for complex sites:

"Book a flight from NYC to London" requires:
1. Find the booking form (navigate)
2. Fill origin field with "NYC" (type_text)  
3. Fill destination field with "London" (type_text)
4. Select departure date (interact — may involve date picker)
5. Click "Search Flights" (click)
6. Sort results by price (click dropdown → select option)
7. Select the cheapest flight (read data → click)

Without sub-goals, the agent tries to do everything at once and
the LLM gets confused by the massive action space. With sub-goals,
each step is a focused task with a clear success signal.

### One improvement: OBSTACLE-AWARE sub-goals

```python
PLANNER_PROMPT_V51 = """Break the user's goal into sequential steps.

GOAL: "{goal}"

PAGE STATE:
{page_slice}

SITE MAP (from previous visits):
{map_hints}

RULES:
1. Each step = ONE interaction (click, type, select, read)
2. Include OBSTACLE steps when needed:
   - "Dismiss cookie/privacy banner if present"
   - "Close any popup or modal blocking the page"
   - "Accept terms if prompted"
3. For FORMS, make each field a separate step
4. For DROPDOWNS, split into: "Open the X dropdown" + "Select Y option"
5. success_signal MUST be verifiable: URL contains X, element Y visible,
   field Z has value, dropdown shows selected option
6. Maximum 7 steps. If more needed, group related actions.

OUTPUT:
{{
  "steps": [
    {{
      "description": "exact action (name the element)",
      "type": "navigate|type_text|select|read|dismiss_obstacle",
      "requires_reasoning": true/false,
      "success_signal": "URL contains X / element Y visible / field Z filled"
    }}
  ]
}}

IMPORTANT: Set requires_reasoning=true for:
- Dropdown/select interactions
- Form fields with validation
- Steps that depend on dynamic content
- Steps where the target may be behind an obstacle
"""
```

---

## LAYER 2: PYTHON PRE-COMPUTE (The Detective)

This is the `investigate()` function from before, BUT with
critical additions for messy websites:

```python
@dataclass
class DetectiveReport:
    """Pre-computed analysis of current page state."""
    
    # === Situation ===
    page_identity: str
    on_target: bool
    
    # === Obstacles (NEW — critical for messy sites) ===
    has_obstacle: bool           # Modal, banner, overlay detected
    obstacle_type: str           # "modal" | "cookie_banner" | "overlay" | "none"
    obstacle_dismiss_id: str     # Element ID to dismiss it (if found)
    
    # === Page Classification (NEW) ===  
    page_type: str               # "form" | "nav" | "data" | "search_results" | "error"
    form_fields: list            # [{id, type, label, required, filled}] if page_type=="form"
    next_empty_field: dict       # The next unfilled required field
    
    # === Evidence ===
    answer_found: bool
    evidence: str
    
    # === Candidates ===
    candidates: list             # Top 5-8 scored elements
    
    # === History ===
    tried_and_failed: list
    blocked_ids: set
    
    # === Routing Decision (NEW — tells us WHICH model to use) ===
    complexity: str              # "trivial" | "simple" | "complex"
    recommended_model: str       # "shortcut" | "8b" | "120b"
    recommended_action: str      # "click" | "type" | "select" | "answer" | "dismiss" | "scroll"
    reasoning: str


def investigate(goal, subgoal, dom_elements, action_history, 
               reflexion_entries, page_graph, **kwargs):
    """The detective — now with obstacle detection and complexity routing."""
    
    report = DetectiveReport(...)  # basic fields same as before
    
    # === NEW: Obstacle Detection ===
    # Check for modals, cookie banners, overlays FIRST
    # These BLOCK all other interactions
    for el in dom_elements:
        role = el.get("role", "")
        zone = el.get("zone", "")
        tag = el.get("tag", el.get("type", ""))
        text = (el.get("text", "") or "").lower()
        classes = (el.get("className", "") or "").lower()
        
        is_modal = (
            role == "dialog" or 
            zone == "modal" or
            "modal" in classes or
            "overlay" in classes or
            "popup" in classes
        )
        is_cookie = (
            "cookie" in text or 
            "consent" in text or 
            "privacy" in text or
            "accept all" in text
        )
        
        if is_modal or is_cookie:
            report.has_obstacle = True
            report.obstacle_type = "cookie_banner" if is_cookie else "modal"
            
            # Find the dismiss button WITHIN the modal
            # Look for: "Accept", "Close", "Dismiss", "Got it", "X"
            dismiss_keywords = ["accept", "close", "dismiss", "got it", 
                              "agree", "ok", "x", "✕", "×"]
            for candidate in dom_elements:
                if not candidate.get("interactive"):
                    continue
                ctext = (candidate.get("text", "") or "").lower()
                if any(kw in ctext for kw in dismiss_keywords):
                    # Check if this button is INSIDE the modal
                    # (simplified: check if it's near the modal in DOM order)
                    report.obstacle_dismiss_id = candidate.get("id", "")
                    break
            break  # Only process first obstacle
    
    # === NEW: Page Type Classification ===
    input_count = sum(1 for el in dom_elements 
                     if el.get("tag", el.get("type", "")) in ("input", "textarea", "select"))
    if input_count >= 3:
        report.page_type = "form"
        report.form_fields = _extract_form_fields(dom_elements)
        report.next_empty_field = _find_next_empty(report.form_fields)
    elif any("error" in (el.get("text", "") or "").lower() for el in dom_elements[:20]):
        report.page_type = "error"
    else:
        report.page_type = "nav"  # default
    
    # === NEW: Complexity Routing ===
    # This determines WHETHER to use 8B, 120B, or skip LLM entirely
    
    if report.has_obstacle:
        # Obstacle present — need 120B to reason about dismissing it
        # UNLESS we found an obvious dismiss button
        if report.obstacle_dismiss_id:
            report.complexity = "simple"
            report.recommended_model = "shortcut"
            report.recommended_action = "dismiss"
        else:
            report.complexity = "complex"
            report.recommended_model = "120b"
            report.recommended_action = "dismiss"
    
    elif report.answer_found and report.evidence:
        report.complexity = "trivial"
        report.recommended_model = "8b"  # just format the answer
        report.recommended_action = "answer"
    
    elif report.page_type == "form" and report.next_empty_field:
        # Form filling — depends on field complexity
        field = report.next_empty_field
        if field.get("type") in ("text", "email", "tel", "number"):
            # Simple text input — 8B can handle
            report.complexity = "simple"
            report.recommended_model = "8b"
            report.recommended_action = "type"
        elif field.get("type") in ("select", "dropdown", "date"):
            # Dropdown or date picker — need 120B reasoning
            report.complexity = "complex"
            report.recommended_model = "120b"
            report.recommended_action = "select"
        else:
            report.complexity = "simple"
            report.recommended_model = "8b"
            report.recommended_action = "type"
    
    elif report.candidates and report.candidates[0]["score"] >= 25:
        gap = report.candidates[0]["score"] - (
            report.candidates[1]["score"] if len(report.candidates) > 1 else 0
        )
        if gap > 10:
            # Clear winner — skip LLM
            report.complexity = "trivial"
            report.recommended_model = "shortcut"
            report.recommended_action = "click"
        else:
            # Close scores — 8B tie-breaks
            report.complexity = "simple"
            report.recommended_model = "8b"
            report.recommended_action = "click"
    
    elif len(report.tried_and_failed) >= 2:
        # Multiple failures — need 120B to reason about alternatives
        report.complexity = "complex"
        report.recommended_model = "120b"
        report.recommended_action = "click"
    
    elif not report.candidates or all(c["score"] <= 0 for c in report.candidates):
        # Nothing matches — need 120B to figure out what to do
        report.complexity = "complex"
        report.recommended_model = "120b"
        report.recommended_action = "scroll"
    
    else:
        # Default: simple selection
        report.complexity = "simple"
        report.recommended_model = "8b"
        report.recommended_action = "click"
    
    # Override: if sub-goal says requires_reasoning=true, use 120B
    if kwargs.get("requires_reasoning"):
        report.recommended_model = "120b"
        report.complexity = "complex"
    
    return report


def _extract_form_fields(dom_elements):
    """Extract form field metadata for intelligent form filling."""
    fields = []
    for el in dom_elements:
        tag = el.get("tag", el.get("type", ""))
        if tag not in ("input", "textarea", "select"):
            continue
        
        # Find associated label
        label = el.get("ariaLabel", "") or el.get("placeholder", "") or ""
        if not label:
            # Look for nearby label element (simplified)
            el_id = el.get("id", "")
            # In practice, traverse DOM for <label for="el_id">
        
        fields.append({
            "id": el.get("id", ""),
            "type": el.get("inputType", tag),  # text, email, date, select
            "label": label,
            "required": el.get("required", False) or el.get("ariaRequired") == "true",
            "filled": bool(el.get("value", "")),
            "value": el.get("value", ""),
        })
    return fields


def _find_next_empty(form_fields):
    """Find the next unfilled required field (or first unfilled field)."""
    # Priority: required empty fields first
    for f in form_fields:
        if f["required"] and not f["filled"]:
            return f
    # Then any empty field
    for f in form_fields:
        if not f["filled"]:
            return f
    return None
```

---

## LAYER 3: THE NAVIGATOR (TWO PROMPTS — 8B and 120B)

### 8B Prompt: For Simple Selection (~200ms)
Same as before — minimal, decision-ready, picks from shortlist.

```python
NAVIGATOR_8B_PROMPT = """You are TARA, a web navigation assistant.
PAGE: {page_identity}
GOAL: {goal}
STEP: {subgoal}

{action_section}

Respond with JSON only:
{{"action":{{"type":"{action_type}","target_id":"<id>","text":"<if needed>"}},"confidence":"high|medium","speech":"<brief narration>"}}"""

# action_section is filled dynamically:
# For clicks: "ELEMENTS:\n  1. [a] Dashboard (id: t-abc)\n  2. ..."
# For answers: "DATA FOUND:\n  MODEL | REQUESTS\n  llama-70b | 1247..."
# For typing: "FIELD: Search box (id: t-xyz)\nTYPE: 'New York'"
```

### 120B Prompt: For Complex Situations (~1-2s)
This is where the "thinking like a pro" happens — but only when needed.

```python
NAVIGATOR_120B_PROMPT = """You are TARA, navigating a website for the user.

MISSION: "{goal}"
CURRENT STEP: {subgoal}

PAGE STATE:
{page_slice}

OBSTACLE: {obstacle_info}
FORM STATE: {form_info}

HISTORY (do NOT repeat):
{compressed_history}

SELF-CORRECTIONS:
{reflexion_memory}

SITUATION ANALYSIS:
{detective_reasoning}

You must handle this situation. Consider:
1. Is there an obstacle (modal, banner) I must dismiss first?
2. Is this a dropdown/select that needs TWO actions (open + pick)?
3. Is this a form field — what value should I type?
4. Have my previous attempts failed? What's a DIFFERENT approach?
5. Is the target hidden behind a tab, accordion, or collapsed section?

Pick ONE action. If the element needs scrolling first, say so.
target_id MUST exist in PAGE STATE above.

JSON only:
{{"action":{{"type":"click|type_text|scroll_to_view|answer|select|dismiss","target_id":"<id>","text":"<value if typing or selecting>"}},"confidence":"high|medium|low","speech":"<brief narration>"}}"""
```

---

## THE DECISION ROUTER: The Core Innovation

This replaces a single `reason()` call with intelligent routing:

```python
async def navigate_step(self, session, dom_elements, page_graph, 
                        goal, url, stagnation_action):
    """
    The unified step handler. Routes to the right model 
    based on pre-computed complexity.
    """
    
    # ── Phase 1: Detective Investigation (Python, ~0ms) ──
    subgoal = session.current_subgoal()
    report = investigate(
        goal=session.goal_raw or goal,
        subgoal=subgoal.description if subgoal else goal,
        dom_elements=dom_elements,
        action_history=session.action_ledger[-8:],
        reflexion_entries=session.reflexion_memory.entries if session.reflexion_memory else [],
        page_graph=page_graph,
        stagnation_action=stagnation_action,
        requires_reasoning=getattr(subgoal, 'requires_reasoning', False),
    )
    
    log.info(f"Detective: {report.complexity} | model={report.recommended_model} | "
             f"action={report.recommended_action} | obstacle={report.has_obstacle} | "
             f"candidates={len(report.candidates)} | {report.reasoning}")
    
    # ── Phase 2: Route to the right handler ──
    
    if report.recommended_model == "shortcut":
        result = self._shortcut(report)
        result["_routing"] = "shortcut"
        
    elif report.recommended_model == "8b":
        result = await self._navigate_8b(report, session, goal)
        result["_routing"] = "8b"
        
    elif report.recommended_model == "120b":
        result = await self._navigate_120b(report, session, goal, page_graph)
        result["_routing"] = "120b"
    
    else:
        # Fallback: 8B
        result = await self._navigate_8b(report, session, goal)
        result["_routing"] = "8b_fallback"
    
    # ── Phase 3: Post-process ──
    # Validate target_id exists
    action = result.get("action", {})
    if action.get("type") == "click" and action.get("target_id"):
        valid_ids = {c["id"] for c in report.candidates}
        valid_ids.add(report.obstacle_dismiss_id or "")
        if action["target_id"] not in valid_ids:
            if report.candidates:
                action["target_id"] = report.candidates[0]["id"]
                result["confidence"] = "medium"
    
    return result


def _shortcut(self, report):
    """Deterministic action — no LLM call (0ms)."""
    
    if report.recommended_action == "dismiss" and report.obstacle_dismiss_id:
        return {
            "action": {"type": "click", "target_id": report.obstacle_dismiss_id, "text": ""},
            "confidence": "high",
            "speech": f"Dismissing {report.obstacle_type}.",
            "_ms": 0,
        }
    
    if report.recommended_action == "answer" and report.evidence:
        return {
            "action": {"type": "answer", "target_id": "", "text": report.evidence[:300]},
            "confidence": "high", 
            "speech": "Here's what I found.",
            "_ms": 0,
        }
    
    if report.recommended_action == "click" and report.candidates:
        best = report.candidates[0]
        return {
            "action": {"type": "click", "target_id": best["id"], "text": ""},
            "confidence": "high",
            "speech": f"Clicking {best['text'][:25]}.",
            "_ms": 0,
        }
    
    return {
        "action": {"type": "scroll_to_view", "target_id": "", "text": "down"},
        "confidence": "medium",
        "speech": "Scrolling down.",
        "_ms": 0,
    }


async def _navigate_8b(self, report, session, goal):
    """Fast navigator for simple selections (~200-400ms)."""
    prompt = _build_8b_prompt(report, goal, 
                              session.current_subgoal().description)
    
    response = await self.groq.generate(
        prompt,
        model="llama-3.1-8b-instant",
        temperature=0.1,
        max_tokens=256,
        response_format={"type": "json_object"},
    )
    return _safe_parse(response)


async def _navigate_120b(self, report, session, goal, page_graph):
    """Deep navigator for complex situations (~1-2s)."""
    
    # Build richer context for 120B
    page_slice = page_graph.query_slice(goal, max_elements=40) if page_graph else ""
    
    # Obstacle info
    obstacle_info = "None"
    if report.has_obstacle:
        obstacle_info = (
            f"TYPE: {report.obstacle_type}\n"
            f"DISMISS BUTTON: {report.obstacle_dismiss_id or 'NOT FOUND'}\n"
            f"You MUST dismiss this before doing anything else."
        )
    
    # Form info
    form_info = "Not a form page"
    if report.page_type == "form":
        form_info = "FORM FIELDS:\n"
        for f in report.form_fields[:6]:
            status = f"filled={f['value']}" if f['filled'] else "EMPTY"
            form_info += f"  [{f['type']}] {f['label']} (id: {f['id']}) [{status}]\n"
        if report.next_empty_field:
            form_info += f"\nNEXT FIELD TO FILL: {report.next_empty_field['label']} "
            form_info += f"(id: {report.next_empty_field['id']})"
    
    # Reflexion
    reflexion = "(No prior failures)"
    if session.reflexion_memory and session.reflexion_memory.entries:
        reflexion = session.reflexion_memory.format_for_prompt()
    
    # History
    history = "\n".join([
        f"  {h.action_type} '{h.target_text}' → {h.actual_outcome[:40]}"
        for h in session.action_ledger[-5:]
    ]) or "(First step)"
    
    prompt = NAVIGATOR_120B_PROMPT.format(
        goal=session.goal_raw or goal,
        subgoal=session.current_subgoal().description,
        page_slice=page_slice,
        obstacle_info=obstacle_info,
        form_info=form_info,
        compressed_history=history,
        reflexion_memory=reflexion,
        detective_reasoning=report.reasoning,
    )
    
    response = await self.groq.generate_with_reasoning(
        prompt,
        model="openai/gpt-oss-120b",
        reasoning_effort="medium",
        include_reasoning=True,
        max_completion_tokens=1024,  # NOT 4096 — cap it
        response_format={"type": "json_object"},
    )
    return _safe_parse(response)
```

---

## REFLEXION MEMORY: Self-Correction That Actually Works

The template-based approach from before was too rigid. 
For complex sites, we need SLIGHTLY smarter critique.

Here's the fix: use the 8B model for reflection (cheap + fast)
but ONLY after failures.

```python
class ReflexionMemory:
    """Episodic self-correction memory. Max 3 entries (per Reflexion paper)."""
    
    def __init__(self, max_entries=3):
        self.entries = []
        self.max_entries = max_entries
    
    def add_failure(self, step, action_type, target_text, outcome):
        """Template-based critique — zero LLM cost."""
        
        # Pattern-match common failure modes
        if "no effect" in outcome or "unchanged" in outcome:
            critique = f"Clicking '{target_text}' did nothing — it may be decorative or disabled."
            strategy = "Look for a DIFFERENT element: sidebar link instead of content button, or check if a modal is blocking."
        
        elif "element not found" in outcome:
            critique = f"'{target_text}' doesn't exist on this page anymore."
            strategy = "The page may have updated. Look at what's actually visible now."
        
        elif "modal" in outcome or "overlay" in outcome:
            critique = f"Tried to click '{target_text}' but a modal/overlay is blocking."
            strategy = "Dismiss the overlay FIRST (look for Close/Accept/X button), THEN retry."
        
        elif "already active" in outcome:
            critique = f"'{target_text}' is already selected/active."
            strategy = "Look for content WITHIN the active section, not the nav link itself."
        
        else:
            critique = f"'{target_text}' failed: {outcome[:60]}"
            strategy = "Try a completely different path."
        
        self.entries.append({
            "step": step,
            "action": f"{action_type} '{target_text}'",
            "critique": critique,
            "strategy": strategy,
        })
        
        # Keep only last N
        if len(self.entries) > self.max_entries:
            self.entries.pop(0)
    
    def format_for_prompt(self):
        if not self.entries:
            return "(No prior failures)"
        lines = []
        for e in self.entries:
            lines.append(f"Step {e['step']}: {e['action']} → FAILED")
            lines.append(f"  Why: {e['critique']}")
            lines.append(f"  Instead: {e['strategy']}")
        return "\n".join(lines)
```

---

## THE COMPLETE STEP FLOW: A Complex Example

### Scenario: "Book the cheapest flight from NYC to London"
### Website: Messy travel site with cookie banner + dynamic form

```
User: "Book the cheapest flight from NYC to London"

═══ PLANNER (120B, once, ~2s) ═══
Goal decomposed into:
  Step 1: dismiss_obstacle — "Dismiss cookie consent if present"
  Step 2: type_text — "Type 'New York' in origin field" (requires_reasoning=false)
  Step 3: type_text — "Type 'London' in destination field" (requires_reasoning=false)
  Step 4: select — "Select departure date" (requires_reasoning=true)
  Step 5: click — "Click Search Flights button"
  Step 6: select — "Sort by price: lowest first" (requires_reasoning=true)
  Step 7: read — "Identify cheapest flight and tell the user"

═══ STEP 1: Dismiss cookie banner ═══
Detective: has_obstacle=true, type=cookie_banner, dismiss_id="t-accept-btn"
Route: SHORTCUT (0ms)
Action: click "t-accept-btn" → cookie banner dismissed
Validate: modal disappeared → SUCCESS → advance

═══ STEP 2: Type origin ═══
Detective: page_type=form, next_empty_field={id:"t-origin", label:"From", type:"text"}
           candidates=[{id:"t-origin", text:"From", score:30}]
           complexity=simple, model=8b
Route: 8B (200ms)
Prompt: "FIELD: From (id: t-origin)\nTYPE: 'New York'\nPick the field and type the value."
Action: type_text "t-origin" "New York"
Validate: field value changed → SUCCESS → advance

═══ STEP 3: Type destination ═══
(Same pattern as step 2, 8B, ~200ms)
Action: type_text "t-dest" "London"

═══ STEP 4: Select date (COMPLEX) ═══
Detective: page_type=form, next_field={type:"date", label:"Departure"}
           complexity=complex (date picker), model=120b
Route: 120B (1.5s)
Context: 120B sees the date picker widget, calendar UI, available dates
Action: click "t-date-next-month" (first open the calendar, navigate to right month)
Validate: new elements appeared (calendar grid) → SUCCESS but sub-goal not complete
Next iteration: 120B picks the specific date → click "t-date-15" 
Validate: field shows selected date → SUCCESS → advance

═══ STEP 5: Click Search ═══
Detective: candidates=[{id:"t-search-btn", text:"Search Flights", score:35}]
           complexity=trivial (clear winner), model=shortcut
Route: SHORTCUT (0ms)
Action: click "t-search-btn"
Validate: URL changed to /results → SUCCESS → advance

═══ STEP 6: Sort by price (COMPLEX) ═══
Detective: sees "Sort by" dropdown, complexity=complex (dropdown), model=120b
Route: 120B (1.5s)
120B reasons: "I need to first OPEN the dropdown, then select 'Price: Low to High'"
Action: click "t-sort-dropdown" (opens the dropdown)
Validate: new elements appeared (dropdown options) → SUCCESS but not complete
Next iteration: 120B sees options, picks "Price: Low to High"
Action: click "t-sort-price-low"
Validate: results reordered → SUCCESS → advance

═══ STEP 7: Read cheapest flight ═══
Detective: answer_found=true (table data shows flight prices)
           evidence="Air France | NYC→LDN | $412 | 7h 30m\nBA | $456 | 6h 45m"
Route: 8B (250ms) — format the answer
Action: answer "The cheapest flight is Air France at $412, 7.5 hour flight."
Validate: answer delivered → MISSION COMPLETE

═══ TOTAL TIMING ═══
Planner:     ~2s    (once)
Shortcuts:   ~0ms   (steps 1, 5)
8B calls:    ~650ms (steps 2, 3, 7)
120B calls:  ~4.5s  (steps 4, 6 — two iterations each)
Validation:  ~100ms (per step)
─────────────────────
TOTAL:       ~7-8 seconds for a 7-step complex booking task
```

---

## COMPARISON: All Approaches Head-to-Head

| Approach | Simple Task (4 steps) | Complex Task (7 steps) | Failure Handling |
|----------|----------------------|------------------------|-----------------|
| v4 (OODA 3-call) | 23s, works | 60s+, likely fails | Auto-advance (broken) |
| v5 (120B single) | 6-10s, works | 15-20s, works | Reflexion (good) |
| v5.1 8B-only | 1-2s, works | FAILS on dropdowns/forms | Python fallback (fragile) |
| **v5.1 Hybrid** | **1-3s** | **7-10s** | **Reflexion + 120B escalation** |

The hybrid is:
- As FAST as 8B-only on simple tasks (shortcuts + 8B)
- As SMART as 120B on complex tasks (escalates when needed)
- NEVER wastes 120B on trivial selections
- NEVER asks 8B to handle dropdowns or modals

---

## IMPLEMENTATION: What to Change in Your Codebase

### File 1: visual_orchestrator.py
- Replace `_reason()` with `navigate_step()` (the router above)
- Keep `_decompose_goal()` but update prompt to include `requires_reasoning`
- Keep `validate_action_outcome()` — it's already good
- Remove `_observe()`, `_orient()`, `_decide()` — collapsed into router

### File 2: tara-widget.js  
- Remove viewport filter at line 2070 (keep inViewport as metadata)
- Add `extractVisibleTables()` and `detectActiveStates()`
- Add obstacle detection: `detectModals()` returns modal info

### File 3: NEW — detective.py
- The `investigate()` function with obstacle detection + complexity routing
- The `ReflexionMemory` class

### File 4: NEW — prompts_v51.py
- `PLANNER_PROMPT_V51` (obstacle-aware sub-goals)
- `NAVIGATOR_8B_PROMPT` (minimal shortlist selection)
- `NAVIGATOR_120B_PROMPT` (context-rich problem solving)

### What stays from v5:
- Redis PageGraph (Semantic Page Graph) — essential
- query_slice() for DOM distillation — essential
- Deterministic validation — essential
- Sub-goal system — essential (you were right to keep it)
- Reflexion memory — essential

### Total implementation: ~6-8 hours
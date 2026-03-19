# TARA Visual Copilot: Detailed Pipeline Execution Journal

> Based on log analysis from 2026-03-16 12:11:34 execution trace
> Goal: "What is the usage for Groq Whisper for last 7 days in groq console"

---

## Executive Summary

This journal traces the complete execution flow from user input through the TARA Visual Copilot pipeline, focusing on the **Last-Mile Compound Protocol** with **Vision Bootstrap → One-Call Reasoning → Tool Execution**. The pipeline demonstrates the dual-model architecture: Groq llama-4-scout for vision analysis, GPT-OSS 120b for reasoning and action decisions.

---

## Phase 0: Pipeline Entry Point

### File: `visual_copilot/orchestration/pipeline.py`

**Purpose**: Main entry point for all planning requests. Creates the execution context and delegates to the ultimate planner.

**Key Function**: `run_pipeline()` (line 15)

**Execution Flow**:
```python
# 1. Build planning context with trace ID
trace_id = str(uuid.uuid4())
ctx = build_context(app, session_id, goal, ...)

# 2. Emit pipeline start event
emit_event("VC_PIPELINE_START", trace_id, ...)

# 3. Check for terminal shortcut (already completed mission)
existing = await mission_brain._load_session_mission(session_id)
terminal = terminal_completion_response(existing)

# 4. Delegate to ultimate planner
result = await ultimate_plan_next_step_impl(...)
```

**What Happens**:
- Creates a unique `trace_id` for observability
- Builds `PlanningContext` with all session state
- Checks if mission already completed (short-circuit)
- Calls `ultimate_plan_next_step_impl` with full context including `screenshot_b64`

---

## Phase 1: Ultimate Plan Next Step Orchestration

### File: `visual_copilot/orchestration/plan_next_step_flow.py`

**Purpose**: 800+ line orchestration file that coordinates all pipeline stages: pre-decision gate, intent detection, hive query, cross-domain routing, lexical routing, semantic detective, and finally LAST MILE execution.

**Key Function**: `ultimate_plan_next_step_impl()` (line 132)

**Execution Flow**:
```python
async def ultimate_plan_next_step_impl(
    app, session_id, goal, current_url,
    step_number, action_history, screenshot_b64, pre_decision, ...
):
    # Stage 1: Pre-Decision Gate (if enabled)
    if ENABLE_PRE_DECISION_GATE and pre_decision:
        gate = _evaluate_pre_decision_gate(pre_decision, ...)
        if gate.blocked:
            return _build_blocked_response(gate)

    # Stage 2: Intent Classification
    intent_result = _classify_intent(goal)

    # Stage 3: Hive Service (semantic memory)
    hive_hints = await hive_service.suggest_nodes(session_id, goal, ...)

    # Stage 4: Cross-Domain Router (if URL mismatch)
    if not is_correct_domain:
        return await _handle_cross_domain_nav(...)

    # Stage 5: Lexical Router (fast path)
    lexical_match = _find_hard_keyword_match(goal, nodes)
    if lexical_match.score >= LEXICAL_DIRECT_ACCEPT_CLICK:
        return _build_lexical_action(lexical_match)

    # Stage 6: Semantic Detective (hybrid scoring)
    detective_report = await semantic_detective.investigate(...)

    # Stage 7: Router V2 (decision making)
    router_decision = await router.decide(...)

    # Stage 8: LAST MILE COMPOUND EXECUTION
    if router_decision.action == "last_mile":
        return await _handle_last_mile_execution(
            schema=schema,
            mission=mission,
            nodes=nodes,
            app=app,
            session_id=session_id,
            screenshot_b64=screenshot_b64,  # <-- Passed through
            force_vision_bootstrap=True,    # <-- VISION TRIGGER
        )
```

**What Happens**:
- **Pre-Decision Gate**: Validates pre-routing decisions for confidence thresholds
- **Intent Classification**: Determines if this is navigation, extraction, form-fill, etc.
- **Hive Service**: Queries semantic memory for previously successful paths
- **Cross-Domain Router**: Handles site navigation if URL doesn't match goal domain
- **Lexical Router**: Fast string-matching for direct keyword hits
- **Semantic Detective**: Hybrid text + structural similarity scoring for element matching
- **Router V2**: Final decision on which action path to take
- **Last Mile Trigger**: Delegates to compound execution when complex reasoning needed

---

## Phase 2: Last-Mile Stage Handler

### File: `visual_copilot/orchestration/stages/last_mile_stage.py`

**Purpose**: Stage-specific handler that wraps the compound last-mile execution with runtime state management (wait gates, deduplication, loading detection).

**Key Function**: `handle_last_mile_stage()` (line 24)

**Execution Flow**:
```python
async def handle_last_mile_stage(ctx: StageContext):
    # 1. Extract pre-decision data
    pre_decision = ctx.payload.get("pre_decision", {})
    force_vision = pre_decision.get("force_vision_bootstrap", False)

    # 2. Build runtime arguments
    runtime_args = {
        "schema": schema,
        "mission": mission,
        "nodes": nodes,
        "app": ctx.app,
        "session_id": ctx.session_id,
        "screenshot_b64": screenshot_b64,  # From payload
        "force_vision_bootstrap": force_vision,
        "excluded_ids": set(already_clicked_ids),
    }

    # 3. Execute compound last-mile (DELEGATES TO last_mile.py)
    result = await execute_compound_last_mile(**runtime_args)

    # 4. Apply wait grace period if configured
    if result and result.get("action"):
        await _apply_wait_grace_period()

    return StageResult.success(result)
```

**What Happens**:
- Extracts configuration from pre-decision payload
- Builds runtime arguments for compound execution
- **Delegates to `execute_compound_last_mile()` in `last_mile.py`**
- Applies post-action wait gates
- Handles deduplication to prevent duplicate actions

---

## Phase 3: Last-Mile Compound Execution (THE CORE)

### File: `visual_copilot/mission/last_mile.py` (~1200 lines)

**Purpose**: The brain of last-mile execution. Implements the **Last-Mile Compound Protocol** with vision bootstrap, one-call reasoning, and tool execution loops.

**Key Function**: `execute_compound_last_mile()` (line ~2390)

**Execution Flow**:

```python
async def execute_compound_last_mile(
    schema, mission, nodes, app, session_id,
    screenshot_b64, force_vision_bootstrap, ...
):
    # ═══════════════════════════════════════════════════════════
    # PHASE A: INITIALIZATION
    # ═══════════════════════════════════════════════════════════

    # 1. Build page context (site map validation)
    target_node = _build_last_mile_page_context(current_url)
    target_node_ctx = _render_page_context_for_onecall(target_node)

    # 2. Score initial evidence relevance
    initial_evidence_hits, initial_best_excerpt, has_entity_evidence = \
        _score_evidence_relevance(main_goal, nodes)

    # 3. Initialize LastMileState (state machine)
    state = LastMileState()
    state.evidence_hits = initial_evidence_hits
    state.progress_score = float(initial_evidence_hits)
    state.best_progress_score = state.progress_score

    # 4. Build page state snapshot for semantic stagnation detection
    if LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED:
        initial_snapshot = build_page_state_snapshot(nodes, current_url, dom_hash)
        state.last_page_snapshot = initial_snapshot

    # 5. Initialize visit graph for rabbit-hole detection
    if LAST_MILE_VISIT_GRAPH_V1_ENABLED:
        state.visit_graph = VisitGraphV1(mission_id, redis_client)
        initial_visit = state.visit_graph.record_visit_from_state(...)

    # ═══════════════════════════════════════════════════════════
    # PHASE B: FORCED VISION BOOTSTRAP (if requested)
    # ═══════════════════════════════════════════════════════════
    # THIS IS WHERE THE LOG SHOWS:
    # "2026-03-16 12:11:34 - 👁️ VISION REQUESTED: Bootstrap last-mile visual grounding..."
    # ═══════════════════════════════════════════════════════════

    if force_vision_bootstrap:
        vision_tool_result = await execute_internal_tool(
            tool_name="request_vision",
            args={"reason": "Bootstrap visual grounding", ...},
            nodes=nodes,
            screenshot_b64=screenshot_b64,
            app=app,
            session_id=session_id,
        )
        state.vision_used = True
        messages.append({
            "role": "user",
            "content": "**VISION BOOTSTRAP (MANDATORY)**\n" + vision_tool_result
        })

    # ═══════════════════════════════════════════════════════════
    # PHASE C: COMPOUND ITERATION LOOP
    # ═══════════════════════════════════════════════════════════

    for iteration in range(1, COMPOUND_MAX_INTERNAL_ITERATIONS + 1):
        state.iteration = iteration

        # 1. Check for escalation conditions
        if LAST_MILE_ESCALATION_CHECKPOINT_ENABLED:
            escalation = should_escalate(mission, state, nodes, ...)
            if escalation:
                return {"action": {"type": "escalate", ...}}

        # 2. Semantic stagnation detection
        if LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED:
            page_state_delta = compare_page_state(last_snapshot, current_snapshot)
            if should_count_as_stagnant(page_state_delta):
                state.stall_count += 1

        # ═══════════════════════════════════════════════════════════
        # PHASE D: ONE-CALL REASONING-ACTION (Primary Mode)
        # ═══════════════════════════════════════════════════════════
        # THIS IS WHERE THE LOG SHOWS:
        # "2026-03-16 12:11:34 - LAST_MILE_ONECALL_REQUEST iter=1 mode=onecall_reasoning_action"
        # ═══════════════════════════════════════════════════════════

        if LAST_MILE_ONECALL_REASONING_ACTION_ENABLED:
            # Build the one-call prompt with full context
            full_prompt = _build_onecall_runtime_prompt(
                main_goal=main_goal,
                target_entity=target_entity,
                dom_text=onecall_dom,          # Compacted DOM (150 nodes max)
                readable_text=onecall_readable, # Compacted readable content
                vision_hints=vision_hints,      # From bootstrap
                ...
            )

            # Call LLM with reasoning capability (GPT-OSS 120b)
            raw_json = await _generate_last_mile_onecall_decision(llm, full_prompt)
            # LOG: "2026-03-16 12:11:36 - GPT-OSS REASONING: input_tokens=4092 output_tokens=550..."

            # Parse JSON response
            decision = _parse_last_mile_onecall_json(raw_json)

            # Validate the decision
            val_result = _validate_onecall_decision(
                decision, nodes, mission, schema, excluded_ids, page_ctx
            )

            if not val_result.is_valid:
                # Handle validation failure with retry
                retry_prompt = full_prompt + "\n[RETRY REQUEST]\n" + val_result.message
                raw_json_retry = await _generate_last_mile_onecall_decision(llm, retry_prompt)
                decision = _parse_last_mile_onecall_json(raw_json_retry)

            # ═══════════════════════════════════════════════════════════
            # PHASE E: VISION GATE (Enforces vision recommendations)
            # ═══════════════════════════════════════════════════════════

            if force_vision_bootstrap and iteration == 1 and vision_hints:
                # Block complete_mission if vision says answer not visible
                if "answer_visible: false" in vision_hints.lower():
                    if decision.action.type == "complete_mission":
                        logger.warning("VISION_GATE blocked complete_mission")
                        messages.append({
                            "role": "user",
                            "content": "BLOCKED: Vision bootstrap indicates answer NOT visible"
                        })
                        continue  # Retry with vision reminder

            # ═══════════════════════════════════════════════════════════
            # PHASE F: TOOL EXECUTION
            # ═══════════════════════════════════════════════════════════
            # THIS IS WHERE THE LOG SHOWS:
            # "2026-03-16 12:11:36 - Tool executor executing complete_mission..."
            # ═══════════════════════════════════════════════════════════

            if decision and val_result.is_valid:
                frontend_actions = []
                for act_dict in val_result.normalized_actions:
                    is_terminal, tool_result, f_act = await execute_internal_tool(
                        tool_name=act_dict["type"],
                        args=act_dict,
                        nodes=nodes,
                        screenshot_b64=screenshot_b64,
                        app=app,
                        session_id=session_id,
                        excluded_ids=excluded_ids,
                    )

                    if f_act:
                        frontend_actions.append(f_act)

                    if is_terminal:
                        break  # Terminal action ends iteration

                if frontend_actions:
                    return {
                        "action": frontend_actions[0],
                        "thought": raw_json,
                        "iterations": iteration,
                        "status": "complete" if terminal else "action",
                    }
```

**What Happens**:

1. **Initialization Phase**:
   - Builds page context from site map validator
   - Scores initial evidence relevance against goal
   - Initializes `LastMileState` (state machine tracking)
   - Creates semantic page snapshot for stagnation detection
   - Initializes visit graph for rabbit-hole detection

2. **Vision Bootstrap Phase** (from logs: 12:11:34):
   - Calls `execute_internal_tool` with `request_vision`
   - **This triggers Groq llama-4-scout-17b-16e-instruct** (3701→109 tokens)
   - Vision result injected into message context for all future reasoning

3. **One-Call Reasoning Phase** (from logs: 12:11:34):
   - Builds compacted DOM representation (150 nodes max)
   - Builds compacted readable content (50 nodes max)
   - Constructs full prompt with vision hints, mission context, DOM, and rules
   - Calls LLM with `generate_with_reasoning` capability
   - **This triggers GPT-OSS 120b** (4092→550 tokens, reasoning_effort=low)
   - Parses JSON response into `LastMileDecision`
   - Validates decision against DOM (no hallucinated IDs)

4. **Vision Gate Phase**:
   - If vision bootstrap said "answer_visible: false", blocks premature completion
   - Enforces vision-recommended actions on first iteration
   - Prevents LLM from ignoring visual evidence

5. **Tool Execution Phase** (from logs: 12:11:36):
   - Calls `execute_internal_tool` for the chosen action
   - If terminal (complete_mission), returns result to frontend
   - If non-terminal (click, type), adds observation to messages and continues loop

---

## Phase 4: Tool Execution Engine

### File: `visual_copilot/mission/tool_executor.py` (~1600 lines)

**Purpose**: Bridges LLM tool calls to TARA's DOM validation + Groq Vision pipeline. Implements guardrails, intent resolution, and multimodal vision analysis.

**Key Function**: `execute_internal_tool()` (line 863)

**Execution Flow by Tool Type**:

### Tool: `request_vision`

```python
async def execute_internal_tool(
    tool_name="request_vision",
    args={"reason": "Bootstrap visual grounding..."},
    nodes=nodes,
    screenshot_b64=screenshot_b64,
    app=app,
    session_id=session_id,
):
    # 1. Build enriched context for vision
    enriched_reason = build_vision_reason(...)

    # 2. Get fresh screenshot via WebSocket broker (preferred)
    image_b64 = await request_screenshot(app, session_id, reason)

    # 3. Fallback to pre-captured screenshot
    if not image_b64:
        image_b64 = screenshot_b64

    # 4. Call Groq Vision API
    vision_result = await _call_groq_vision(
        image_b64=image_b64,
        reason=enriched_reason,
        nodes=nodes,
        app=app,
    )
    # LOG: "2026-03-16 12:11:34 - Vision request to Groq llama-4-scout..."

    # 5. Parse structured hints from vision response
    hints = parse_vision_response(vision_result)
    hints = _filter_vision_hints(hints, nodes, excluded_ids)

    # 6. Render reasoning brief for LLM context
    brief_text = _render_vision_reasoning_brief(hints, nodes)

    return False, brief_text, None  # Non-terminal, feeds back to LLM
```

**Vision Response Processing**:

The vision model returns natural language like:
```
Answer visible now: no
Visible page mode: documentation
Strongest visible control: Activity (t-123)
Safest next probe: click_element
Action Plan: click t-123 (Activity tab)
Confidence: high
```

This is parsed into structured hints:
- `answer_visible`: bool
- `best_target_id`: "t-123"
- `recommended_tool`: "click_element"
- `evidence_summary`: "Activity tab visible in left sidebar"

### Tool: `click_element`

```python
if tool_name == "click_element":
    # PHASE 4: Intent-Based Action Resolution
    if "intent" in args:
        # Intent-based: LLM describes what to click, not ID
        resolved_id, resolve_reason = _resolve_intent_to_target_id(
            intent=args["intent"],  # {text_label, zone, element_type, context}
            nodes=nodes,
            excluded_ids=excluded_ids,
        )
        target_id = resolved_id
    else:
        # Legacy: direct target_id
        target_id = args.get("target_id")

    # Guardrail: Validate ID exists in DOM
    if not _is_valid_id(target_id, nodes):
        return False, "Error: ID does not exist", None

    # Guardrail: Block re-clicks
    if target_id in excluded_ids:
        return False, "Error: Already clicked", None

    # Guardrail: Clickability check
    if not _is_clickable_interactive_node(target_node):
        return False, "REJECTED: Not a clickable control", None

    # Guardrail: Section re-click detection
    if _is_same_section_reclick_blocked(nodes, target_node):
        return False, "REJECTED: Already in this section", None

    # Auto-enable force_click for Radix UI patterns
    force_click = _should_force_click(text_label, context, target_node)

    return False, "Action queued", {
        "type": "click",
        "target_id": target_id,
        "speech": args.get("why", "Clicking element"),
        "force_click": force_click,
    }
```

**Intent Resolution Scoring** (lines 95-204):

```python
def _resolve_intent_to_target_id(intent, nodes, excluded_ids):
    text_label = intent.get("text_label", "")
    zone = intent.get("zone", "")
    element_type = intent.get("element_type", "")
    context = intent.get("context", "")

    for n in nodes:
        score = 0.0

        # Text match: +0.5 (most important)
        if text_label.lower() in node_text:
            score += 0.5

        # Zone match: +0.3
        if zone.lower() in node_zone:
            score += 0.3

        # Type match: +0.2
        if element_type.lower() in node_tag:
            score += 0.2

        # Context match: +0.1 (bonus)
        if context.lower() in node_aria:
            score += 0.1

        # Exact text match: +0.2 bonus
        if node_text.strip() == text_label.lower().strip():
            score += 0.2

    return best_id if best_score >= 0.5 else None
```

### Tool: `complete_mission`

```python
if tool_name == "complete_mission":
    status = args.get("status", "success")
    response = args.get("response", "")
    evidence_refs = args.get("evidence_refs", "")
    answer_confidence = args.get("answer_confidence", "medium")

    # ═══════════════════════════════════════════════════════════
    # COMPLETION GATES (Multi-layer validation)
    # ═══════════════════════════════════════════════════════════

    # Gate 1: URL Evidence Check
    url_evidence = _extract_url_evidence(current_url, main_goal)
    if url_evidence.get("matches_goal"):
        # Allow completion based on URL parameters
        pass

    # Gate 2: Interaction Required Check
    if _requires_interaction_before_completion(main_goal, schema_action):
        if session_clicks == 0 and not url_evidence.get("matches_goal"):
            return False, "REJECTED: Must CLICK before completion", None

    # Gate 3: Entity Anchor Verification (LogicCritic)
    entity_terms, qualifier_terms = _goal_entity_and_qualifier_terms(main_goal)
    anchor_satisfied = _verify_entity_anchor(evidence_blob, entity_terms)
    if not anchor_satisfied:
        return False, "REJECTED: Entity not in evidence", None

    # Gate 4: Low Confidence Interception
    if answer_confidence == "low":
        # Try evidence rescue from readable nodes
        comprehensive_answer = _extract_comprehensive_answer(nodes, main_goal)
        if comprehensive_answer:
            return True, "", {
                "type": "answer",
                "text": comprehensive_answer,
                "status": "success",
                "answer_confidence": "medium",
            }
        return False, "REJECTED: Low confidence, re-examine page", None

    # Gate 5: Metric Mismatch Detection
    if has_money_refs and not has_token_refs:
        return False, "WARNING: Wrong metric (cost vs tokens)", None

    # Gate 6: Entity Anchor (Strict)
    if target_entity and _requires_interaction_before_completion(...):
        entity_tokens = extract_significant_tokens(target_entity)
        found_count = sum(1 for et in entity_tokens if et in evidence_lower)
        if found_count < len(entity_tokens) / 2:
            return False, "REJECTED: Entity not adequately found", None

    # ═══════════════════════════════════════════════════════════
    # COMPLETION ACCEPTED
    # ═══════════════════════════════════════════════════════════
    # LOG: "2026-03-16 12:11:36 - LAST_MILE success, iterations=1, status=complete"

    return True, "", {
        "type": "answer",
        "speech": response,
        "text": response,
        "status": "success",
        "evidence_refs": evidence_refs,
        "answer_confidence": answer_confidence,
    }
```

---

## Phase 5: LLM Provider Integration

### File: `llm_providers/groq_provider.py`

**Purpose**: Provider wrapper for Groq API with support for chat completions, tool use, vision, and reasoning.

**Key Functions**:

```python
class GroqProvider:
    async def generate_messages(self, messages, model, max_tokens, temperature, response_format=None):
        """Standard chat completion with optional JSON mode"""
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        # ... API call

    async def generate_with_reasoning(self, messages, model, reasoning_effort="low", ...):
        """GPT-OSS specific with reasoning tokens"""
        # Handles gpt-oss-120b with reasoning_effort parameter
        # Returns content with reasoning traces stripped

    async def generate_vision(self, text_prompt, image_b64, model, ...):
        """Multimodal vision analysis"""
        payload = {
            "model": model,  # meta-llama/llama-4-scout-17b-16e-instruct
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": text_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ]},
            ],
        }
```

---

## Log Trace Analysis: Complete Execution Flow

Based on your logs from 2026-03-16 12:11:34:

```
12:11:34 - Request screenshot from browser (WebSocket)
         ↓
12:11:34 - Screenshot captured (jpeg size)
         ↓
12:11:34 - VISION REQUESTED to Groq llama-4-scout-17b-16e-instruct
         ↓
12:11:34 - Vision result: "Activity tab is the strongest control..."
         ↓
12:11:34 - LAST_MILE_ONECALL_REQUEST iter=1 mode=onecall_reasoning_action
         ↓
12:11:36 - GPT-OSS REASONING: input_tokens=4092 output_tokens=550 reasoning_effort=low
         ↓
12:11:36 - One-Call decision parsed: complete_mission
         ↓
12:11:36 - Tool executor executing complete_mission
         ↓
12:11:36 - Completion gates passed (URL evidence: dateRange matches 7 days)
         ↓
12:11:36 - LAST_MILE success, iterations=1, status=complete
         ↓
12:11:36 - Pipeline returning success response to frontend
```

**Token Usage Summary**:
- Vision: 3701 → 109 tokens (Groq llama-4-scout)
- Reasoning: 4092 → 550 tokens (GPT-OSS 120b, low reasoning effort)
- **Total**: ~7.8k input, ~660 output

---

## Architecture Patterns Summary

### 1. Dual-Stream Vision + Reasoning
- **Vision Stream**: Groq llama-4-scout for visual page analysis
- **Reasoning Stream**: GPT-OSS 120b for decision making with tool use

### 2. Intent-Based Architecture
- Phase 1: LLM describes what to click (text_label, zone, type, context)
- Phase 2: `_resolve_intent_to_target_id()` scores DOM nodes
- Phase 3: Best match >= 0.5 score wins, mapped to actual DOM ID

### 3. Multi-Layer Completion Gates
- URL Evidence Check
- Interaction Required Check
- Entity Anchor Verification (LogicCritic)
- Low Confidence Interception
- Metric Mismatch Detection

### 4. Vision Bootstrap Pattern
- Force vision analysis before any click/type decisions
- Vision result injected into LLM context
- Vision Gate blocks actions that contradict visual evidence

### 5. State Machine Tracking
- `LastMileState` tracks iterations, evidence hits, stall count
- `VisitGraphV1` prevents rabbit-hole navigation patterns
- `PageStateSnapshot` detects semantic stagnation

---

## Files Reference Map

| File | Lines | Purpose |
|------|-------|---------|
| `orchestration/pipeline.py` | 149 | Main entry, context building |
| `orchestration/plan_next_step_flow.py` | 800+ | Stage orchestration, router coordination |
| `orchestration/stages/last_mile_stage.py` | ~150 | Stage wrapper, wait gates |
| `mission/last_mile.py` | ~1200 | **Core**: Compound protocol, one-call reasoning |
| `mission/tool_executor.py` | ~1600 | **Core**: Tool execution, vision, guardrails |
| `mission/screenshot_broker.py` | ~200 | WebSocket screenshot capture |
| `mission/page_state.py` | ~300 | Semantic fingerprinting |
| `mission/visit_graph.py` | ~250 | Rabbit-hole detection |
| `mission/escalation_checkpoint.py` | ~150 | Escalation logic |
| `llm_providers/groq_provider.py` | ~400 | LLM API integration |

---

## Key Configuration Environment Variables

```bash
# One-Call Mode
LAST_MILE_ONECALL_REASONING_ACTION_ENABLED=true
LAST_MILE_ONECALL_JSON_SCHEMA_STRICT=true
LAST_MILE_ONECALL_INCLUDE_REASONING=false

# Vision
LAST_MILE_VISION_POLICY_TRIGGER=true
LAST_MILE_FORCE_VISION_BOOTSTRAP=true  # In pre_decision

# Semantic Tracking
LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED=true
LAST_MILE_LOADING_STATE_GUARD_ENABLED=true
LAST_MILE_VISIT_GRAPH_V1_ENABLED=true

# Timeouts
COMPOUND_MAX_INTERNAL_ITERATIONS=6
LAST_MILE_WAIT_GRACE_MS=1800
LAST_MILE_DEDUP_WINDOW_MS=6000
```

---

---

## Phase 6: Contract-First Mapped Mode (2026-03-16+)

> **NEW ARCHITECTURE**: Contract-first mapped mode for deterministic known-site browsing.
> **PRIMARY FILE**: `visual_copilot/mission/last_mile_tools.py`, `visual_copilot/mission/last_mile.py`

### 6.1 Architecture Overview

**Problem Solved**: Vision-first priority caused failures when vision missed controls (e.g., date pickers, filter tabs). One-size-fits-all FSM couldn't handle different task types (extraction vs. action vs. form-fill).

**Solution**: Site map as SOURCE OF TRUTH with vision as advisory optimizer. Task-type-specific FSMs for deterministic execution.

**Priority Order (CONTRACT-FIRST)**:
```
1. Site map control_groups (CONTRACT)    → e.g., control_groups["date_filters"]
2. Vision hints (ADVISORY)              → Vision saw it, but site map is truth
3. DOM-grounded fallback                → When site map and vision both miss
4. Wait/retry                           → Let UI settle and retry
```

### 6.2 Per-Turn Node Resolution

**File**: `visual_copilot/orchestration/plan_next_step_flow.py` (lines 455-475)

**Before**: Node resolved once at entry, stale contract throughout execution.

**After**: `resolve_current_node()` called every turn to synchronize with live page state.

```python
# In plan_next_step_flow.py, inside last-mile execution loop
current_node = resolve_current_node(url, dom_summary=nodes)
if current_node:
    logger.info(f"CONTRACT_FIRST: node={current_node.get('node_id')} controls={current_node.get('expected_controls')}")

# Build mapped context with fresh contract
context = MappedTerminalContext.from_site_map(
    url=url,
    nodes=nodes,
    current_node=current_node,  # Fresh every turn
)
```

**Site Map Node Schema (Extended)**:
```json
{
  "node_id": "usage_section",
  "title": "Usage and Spend",
  "url": "https://console.groq.com/dashboard/usage",

  "control_groups": {
    "date_filters": ["Date Picker", "Period Select"],
    "model_filters": ["Model Filter", "Show All Models"],
    "metric_toggles": ["Activity Tab", "Cost Tab"]
  },

  "task_modes": ["read_extract", "filter_view"],

  "completion_contract": {
    "read_extract": ["entity_visible", "metric_visible", "value_extracted"],
    "filter_view": ["filter_applied", "view_updated"]
  },

  "primary_cta": "Create API Key",  // For action tasks
  "required_fields": ["name"],      // For form-fill tasks
  "expected_controls": ["Date Picker", "Model Filter"],
  "terminal_capabilities": ["read_token_usage"]
}
```

### 6.3 Task-Type Classification

**File**: `visual_copilot/mission/last_mile.py`

**Function**: `classify_task_type(user_goal, current_node)`

**Task Types**:

| Type | FSM Class | Example Goals |
|------|-----------|---------------|
| `READ_EXTRACT` | `MappedExtractionStateMachine` | "Show me Whisper token usage", "What was my cost last week" |
| `CREATE_ACTION` | `MappedActionStateMachine` | "Create a new API key", "Add a new project" |
| `FORM_FILL` | `MappedFormFillStateMachine` | "Update my profile", "Change the team name" |
| `CONFIRM_ACTION` | TBD | "Yes, delete this project", "Confirm the action" |

**Classification Logic**:
```python
def classify_task_type(user_goal: str, current_node: Dict) -> MappedTaskType:
    goal_lower = user_goal.lower()

    if any(v in goal_lower for v in ["create", "add new", "make a", "generate"]):
        if "api key" in goal_lower:
            return MappedTaskType.CREATE_ACTION
        return MappedTaskType.FORM_FILL

    if any(v in goal_lower for v in ["update", "edit", "change", "set my"]):
        return MappedTaskType.FORM_FILL

    if any(v in goal_lower for v in ["show me", "what", "how many", "usage", "cost"]):
        return MappedTaskType.READ_EXTRACT

    return MappedTaskType.READ_EXTRACT
```

### 6.4 Control Group Resolution (Node-Local Search)

**File**: `visual_copilot/mission/last_mile_tools.py`

**Method**: `MappedTerminalContext.resolve_control_from_group()`

**Problem**: Searching entire DOM is slow and error-prone. Controls organized by function (date filters, model filters, metric toggles).

**Solution**: Search only within node-local control group first.

```python
def resolve_control_from_group(
    self,
    group_name: str,          # e.g., "date_filters"
    nodes: List[Any],
    clicked_ids: Set[str],
) -> Optional[Dict[str, Any]]:
    """Resolve control from node-local control_group, not whole DOM."""

    # 1. Get controls defined in site map for this group
    group_controls = self.context.control_groups.get(group_name, [])
    if not group_controls:
        return None

    # 2. Search DOM for each control name
    for control_name in group_controls:
        for node in nodes:
            node_id = getattr(node, "id", "")
            if node_id in clicked_ids:
                continue

            text = str(getattr(node, "text", "") or "").lower()
            if control_name.lower() in text:
                if getattr(node, "interactive", False):
                    return {
                        "target_id": node_id,
                        "intent": {
                            "text_label": control_name,
                            "zone": "main",
                            "element_type": "button",
                            "context": f"From {group_name} group"
                        }
                    }
    return None
```

**Usage in SET_FILTERS State**:
```python
# PRIORITY 1: Site map control_groups (CONTRACT)
date_filter = self.resolve_control_from_group(
    "date_filters", nodes, clicked_ids
)
if date_filter:
    logger.info(f"SET_FILTERS: Using control_group date_filters → {date_filter['target_id']}")
    return MappedAction(
        tool="click_element",
        target_id=date_filter["target_id"],
        why="Apply date filter from site map contract",
    ), False

# PRIORITY 2: Vision hints (ADVISORY)
if isinstance(vision_hints, dict):
    identified = vision_hints.get("identified_controls", [])
    for ctrl in identified:
        if "date" in ctrl.get("label", "").lower():
            if ctrl["target_id"] not in clicked_ids:
                return MappedAction(
                    tool="click_element",
                    target_id=ctrl["target_id"],
                    why="Apply date filter from vision hint",
                ), False

# PRIORITY 3: Expected controls, generic DOM search, wait/retry
```

### 6.5 Vision Advisory Pattern

**File**: `visual_copilot/mission/tool_executor.py`

**Function**: `process_vision_result(raw_vision, site_map_node)`

**Before**: Vision returned raw string, treated as authoritative.

**After**: Vision returns structured dict with confidence scores and `advisory_only` flag.

```python
def process_vision_result(raw_vision: str, site_map_node: Dict) -> Dict:
    """Process vision bootstrap as ADVISORY (not source of truth).

    Returns:
        {
            "raw_recommendations": raw_vision,  # Intact - not rewritten
            "identified_controls": [           # Parsed controls with IDs
                {"label": "Date Picker", "target_id": "t-123", "confidence": 0.9},
                {"label": "Activity Tab", "target_id": "t-456", "confidence": 0.8},
            ],
            "confidence_scores": {             # Per-control confidence 0-1
                "date_picker": 0.9,
                "activity_tab": 0.8,
            },
            "advisory_only": True,             # Flag for last_mile
            "missing_controls": [              # Site map controls vision didn't see
                "Model Filter",                # Vision missed this
            ]
        }
    """
```

**Vision Gate Enhancement**:
```python
# In last_mile.py, one-call reasoning loop
if force_vision_bootstrap and iteration == 1 and vision_hints:
    vision_dict = process_vision_result(vision_hints, site_map_node)

    # Block complete_mission if vision says answer not visible
    if vision_dict.get("answer_visible") is False:
        if decision.action.type == "complete_mission":
            logger.warning("VISION_GATE blocked complete_mission: vision says answer not visible")

            # Remind LLM of vision findings
            missing = vision_dict.get("missing_controls", [])
            messages.append({
                "role": "user",
                "content": f"BLOCKED: Vision indicates answer not yet visible. Missing controls: {missing}"
            })
            continue  # Retry with vision reminder
```

### 6.6 Task-Type FSMs

#### 6.6.1 MappedActionStateMachine (CREATE_ACTION)

**File**: `visual_copilot/mission/last_mile_tools.py`

**State Flow**:
```
VALIDATE_NODE → FIND_PRIMARY_CTA → CLICK_PRIMARY_CTA →
VERIFY_MODAL_OR_FORM → FILL_REQUIRED_FIELDS → CLICK_CONFIRM →
VERIFY_SUCCESS → COMPLETE
```

**Example: "Create a new API key"**:
```python
context = MappedTerminalContext(
    url="https://console.groq.com/dashboard/keys",
    current_node_id="api_keys_section",
    is_mapped=True,
)

machine = MappedActionStateMachine(context, max_attempts=15)

# State transitions driven by observation
for iteration in range(15):
    observation = {
        "nodes": dom_nodes,
        "readable_content": page_content,
        "url_params": {},
        "clicked_ids": clicked_ids,
    }

    action, is_terminal = machine.transition(observation)

    # Expected action sequence:
    # 1. wait_for_ui (validate node)
    # 2. click_element (primary CTA: "Create API Key")
    # 3. type_text (fill "name" field)
    # 4. click_element (confirm button)
    # 5. complete_mission (success verified)
```

#### 6.6.2 MappedFormFillStateMachine (FORM_FILL)

**State Flow**:
```
VALIDATE_NODE → LOCATE_FORM → FIND_REQUIRED_FIELDS →
FILL_FIELD → VALIDATE_FIELD → SUBMIT_FORM → VERIFY_SUCCESS → COMPLETE
```

#### 6.6.3 MappedExtractionStateMachine (READ_EXTRACT)

**State Flow** (enhanced with contract-first):
```
VALIDATE_NODE → VALIDATE_SCOPE → SET_FILTERS →
LOCATE_ENTITY → LOCATE_METRIC → EXTRACT_VALUE →
VALIDATE_EVIDENCE → COMPLETE
```

### 6.7 Completion Contract Validation

**File**: `visual_copilot/mission/last_mile_tools.py`

**Function**: `validate_completion_contract(context, evidence, task_mode)`

**Purpose**: Ensure task completion meets site map-defined requirements before allowing success.

```python
def validate_completion_contract(
    context: MappedTerminalContext,
    evidence: Dict[str, Any],
    task_mode: str = "read_extract",
) -> Tuple[bool, List[str]]:
    """Validate evidence against site map completion contract."""

    contract = context.site_map_node.get("completion_contract", {})
    requirements = contract.get(task_mode, [])

    missing = []
    for req in requirements:
        if req == "entity_visible" and not context.entity_anchor_found:
            missing.append("entity_visible")
        elif req == "metric_visible" and not context.metric_anchor_found:
            missing.append("metric_visible")
        elif req == "value_extracted" and not context.numeric_value_found:
            missing.append("value_extracted")
        elif req == "filter_applied" and not context.filter_applied:
            missing.append("filter_applied")

    return len(missing) == 0, missing
```

**Usage**:
```python
# In MappedExtractionStateMachine.EXTRACT_VALUE state
contract_met, missing = validate_completion_contract(
    self.context, evidence, task_mode="read_extract"
)

if not contract_met:
    logger.warning(f"Completion contract not met: missing {missing}")
    # Continue loop to gather missing evidence
else:
    logger.info("Completion contract satisfied - ready to complete")
    self.state = MappedExtractionState.COMPLETE
```

---

## Architecture Patterns Summary (Updated)

### 1. Dual-Stream Vision + Reasoning (Unchanged)
- **Vision Stream**: Groq llama-4-scout for visual page analysis
- **Reasoning Stream**: GPT-OSS 120b for decision making with tool use

### 2. Intent-Based Architecture (Unchanged)
- Phase 1: LLM describes what to click (text_label, zone, type, context)
- Phase 2: `_resolve_intent_to_target_id()` scores DOM nodes
- Phase 3: Best match >= 0.5 score wins, mapped to actual DOM ID

### 3. Multi-Layer Completion Gates (Enhanced)
- URL Evidence Check
- Interaction Required Check
- Entity Anchor Verification (LogicCritic)
- Low Confidence Interception
- Metric Mismatch Detection
- **NEW**: Completion Contract Validation (site map-defined requirements)

### 4. Vision Bootstrap Pattern (Enhanced)
- Force vision analysis before any click/type decisions
- Vision result injected into LLM context
- Vision Gate blocks actions that contradict visual evidence
- **NEW**: Vision is ADVISORY, site map is CONTRACT

### 5. State Machine Tracking (Enhanced)
- `LastMileState` tracks iterations, evidence hits, stall count
- `VisitGraphV1` prevents rabbit-hole navigation patterns
- `PageStateSnapshot` detects semantic stagnation
- **NEW**: `MappedExtractionStateMachine` for READ_EXTRACT tasks
- **NEW**: `MappedActionStateMachine` for CREATE_ACTION tasks
- **NEW**: `MappedFormFillStateMachine` for FORM_FILL tasks

### 6. NEW: Contract-First Pattern
- Site map node as SOURCE OF TRUTH
- Per-turn `resolve_current_node()` for live synchronization
- Control groups for node-local search
- Task-type-specific FSMs
- Completion contracts for validation

---

## Key Configuration Environment Variables (Updated)

```bash
# One-Call Mode
LAST_MILE_ONECALL_REASONING_ACTION_ENABLED=true
LAST_MILE_ONECALL_JSON_SCHEMA_STRICT=true
LAST_MILE_ONECALL_INCLUDE_REASONING=false

# Vision
LAST_MILE_VISION_POLICY_TRIGGER=true
LAST_MILE_FORCE_VISION_BOOTSTRAP=true  # In pre_decision

# Semantic Tracking
LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED=true
LAST_MILE_LOADING_STATE_GUARD_ENABLED=true
LAST_MILE_VISIT_GRAPH_V1_ENABLED=true

# Timeouts
COMPOUND_MAX_INTERNAL_ITERATIONS=6
LAST_MILE_WAIT_GRACE_MS=1800
LAST_MILE_DEDUP_WINDOW_MS=6000

# NEW: Contract-First Mapped Mode (2026-03-16)
MAPPED_CONTRACT_FIRST_ENABLED=true
MAPPED_TASK_TYPE_FSM_ENABLED=true
MAPPED_VISION_ADVISORY_ENABLED=true
MAPPED_COMPLETION_CONTRACT_ENABLED=true
```

---

## Test Coverage (New)

| Test File | Purpose | Status |
|-----------|---------|--------|
| `tests/test_contract_first_mapped_mode.py` | Node resolution, control_groups extraction, contract-first handoff | ✓ |
| `visual_copilot/mission/tests/test_vision_advisory_not_authoritative.py` | Vision normalization tests | ✓ |
| `visual_copilot/mission/tests/test_node_local_control_resolution.py` | Control group resolution tests | ✓ |
| `visual_copilot/mission/tests/test_action_fsm_api_key.py` | 19 test cases for action FSM | ✓ |
| `test_mapped_mode_filters.py` | Filter clicking with date picker, metric tabs | ✓ |
| `test_mapped_mode_validation.py` | Node validation, contract validation | ✓ |

---

## Migration Guide

See `MIGRATION_NOTES.md` for:
- Site map schema changes
- Code changes by file
- Step-by-step migration instructions
- Acceptance criteria
- Rollback procedures

---

*End of Journal (Updated 2026-03-16: Contract-First Mapped Mode v2.0)*

---

## Addendum: Balance Restoration - Determinism + Adaptability (2026-03-16)

### Overview

The contract-first mapped mode implementation was tipping too far into determinism, behaving like a rigid workflow engine instead of an adaptive agent. This update restores balance:

- **Pre-route**: Stays deterministic (railway tracks)
- **Last-mile**: Contract-guided but adaptive (walking inside the station)

### Key Problems Fixed

| Problem | Root Cause | Solution |
|---------|------------|----------|
| Last-mile executing like blind script | FSMs too rigid, following transition_rules exactly | Adaptive state transitions with live DOM checks |
| Contract forcing exact micro-steps | Over-specified site map contract | Contract defines success criteria, not step sequence |
| Vision demoted too far | Vision confidence threshold too low | Vision as "DOM helper" with 0.8+ confidence |
| System robotic/brittle on UI variations | No escape hatch when contract assumptions wrong | Contract breach detection with fallback |

### New Components

#### 1. Contract Breach Detection (Escape Hatch)

**File**: `visual_copilot/mission/last_mile_tools.py`

**Function**: `contract_breach_detected(observation, context) -> Tuple[bool, str]`

**Breach Conditions**:
1. **Control group mismatch** - Less than 30% of expected controls found in DOM
2. **Primary CTA mismatch** - Contract defines CTA but not found in DOM
3. **URL pattern mismatch** - URL doesn't match expected patterns for node
4. **Post-action state mismatch** - Expected post-click elements not present
5. **FSM stall** - Stuck in same state for more than 8 attempts

**Feature Flag**:
```bash
MAPPED_CONTRACT_ESCAPE_HATCH_ENABLED=true  # Default: enabled
```

**Fallback Behavior**:
When breach detected, falls back to `_run_exploratory_last_mile()` - the original adaptive last-mile path.

#### 2. Vision-Assisted Control Resolution

**File**: `visual_copilot/mission/last_mile_tools.py`

**Enhanced Function**: `resolve_control_from_group(group_name, nodes, clicked_ids, vision_hints)`

**Priority Order**:
```
1. Site map control_groups (CONTRACT) - First priority, site map is truth
2. Vision hints with confidence > 0.8 (ADVISORY) - Vision saw something contract doesn't define
3. Generic DOM search (FALLBACK) - Keyword matching when contract and vision both miss
```

**Return Format** (tracks resolution source):
```python
{
    "target_id": str,
    "intent": {...},
    "from": "contract"|"vision"|"dom"
}
```

#### 3. Adaptive FSM State Transitions

**File**: `visual_copilot/mission/last_mile_tools.py`

**Modified**: `MappedActionStateMachine` transition methods

| Transition | Adaptive Skip Condition | New Behavior |
|------------|------------------------|--------------|
| `_transition_validate_node` | Modal/form already visible | Skip to `FILL_REQUIRED_FIELDS` |
| `_transition_find_cta` | CTA already clicked | Skip to `VERIFY_MODAL_OR_FORM` |
| `_transition_verify_modal` | Success already visible | Skip to `COMPLETE` |
| `_transition_verify_modal` | All fields already filled | Skip to `CLICK_CONFIRM` |
| `_transition_fill_fields` | Fields appear already filled | Skip to `CLICK_CONFIRM` |

**Example** (from `_transition_validate_node`):
```python
# ADAPTIVE CHECK: Is modal/form already open?
modal_indicators = ["modal", "dialog", "form", "create", "new", "enter name", "key name"]
modal_already_present = any(
    m in node_text or m in zone.lower()
    for node in nodes
    for node_text in [str(getattr(node, "text", "") or "").lower()]
    for zone in [getattr(node, "zone", "")]
)

if modal_already_present:
    logger.info("ADAPTIVE_SKIP: Modal/form already present, skipping CTA click")
    self.state = MappedActionState.FILL_REQUIRED_FIELDS
    return MappedAction(tool="read_page_content", why="Modal already present"), False
```

#### 4. Contract-Language Structured Logging

**File**: `visual_copilot/mission/last_mile.py`

**Log Events**:
```python
logger.info(f"CURRENT_NODE_RESOLVED=node:{node_id}")
logger.info(f"TASK_MODE_SELECTED={task_type.value}")
logger.info(f"GOAL_ENTITY={context.goal_entity}")
logger.info(f"GOAL_METRIC={context.goal_metric}")
logger.info(f"CONTROL_GROUPS_AVAILABLE={list(control_groups.keys())}")
logger.info(f"CONTROL_GROUP_RESOLVED={group} -> {target_id} (from=contract|vision|dom)")
logger.info(f"COMPLETION_CONTRACT_PASS={True|False}")
logger.warning(f"MAPPED_CONTRACT_ESCAPE_HATCH_TRIGGERED reason={fallback_reason}")
```

### Updated Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                    PRE-ROUTE (Deterministic)                        │
│  PageIndex tree → Known site confidence → Mapped handoff           │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
                    MappedTerminalContext built
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│              LAST-MILE (Contract-Guided + Adaptive)                 │
│                                                                     │
│  ┌─────────────────┐    ┌──────────────────┐    ┌────────────────┐ │
│  │ Contract-First  │ →  │ Adaptive FSM     │ →  │ Escape Hatch   │ │
│  │ control_groups  │    │ State Skips      │    │ (if breach)    │ │
│  └─────────────────┘    └──────────────────┘    └────────────────┘ │
│         ↓                       ↓                        ↓         │
│  Site map truth          Live DOM checks          Fallback to      │
│  (priority 1)            (skip if already)        exploratory      │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              Vision Advisory (confidence > 0.8)             │   │
│  │         Suggests controls, doesn't authorize completion     │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Design Principles

| Layer | Should Be | Why |
|-------|-----------|-----|
| **Pre-route** | Deterministic railway tracks | Decides which line, which station, which transfer. No creativity needed. |
| **Last-mile** | Contract-guided adaptation | Walking inside station - which gate is open, elevator vs stairs. Must adapt. |

**Contract Defines (Not Prescribes)**:
- ✅ Task mode - what kind of work this is
- ✅ Control families - what categories of controls exist
- ✅ Success evidence - what proves completion
- ✅ Verification signals - what to look for after actions
- ❌ Exact step sequence - this makes it brittle

**Live DOM Determines**:
- Which specific control instance matches the contract family
- Whether expected post-control state actually appeared
- If the contract's assumed flow matches reality
- When to abandon contract and fall back to exploration

### Updated Feature Flags

```bash
# Contract-First Mapped Mode
MAPPED_CONTRACT_FIRST_ENABLED=true
MAPPED_TASK_TYPE_FSM_ENABLED=true
MAPPED_VISION_ADVISORY_ENABLED=true
MAPPED_COMPLETION_CONTRACT_ENABLED=true

# NEW: Escape Hatch (2026-03-16)
MAPPED_CONTRACT_ESCAPE_HATCH_ENABLED=true  # Fall back when contract breaches
```

### Test Coverage (Updated)

| Test File | Purpose | Status |
|-----------|---------|--------|
| `tests/test_contract_coverage.py` | Per-layer eval: node resolution, task-mode routing, control resolution, transition, completion | ✓ NEW |
| `tests/test_contract_first_mapped_mode.py` | Node resolution, control_groups extraction, contract-first handoff | ✓ |
| `visual_copilot/mission/tests/test_vision_advisory_not_authoritative.py` | Vision normalization tests | ✓ |
| `visual_copilot/mission/tests/test_node_local_control_resolution.py` | Control group resolution tests | ✓ |
| `visual_copilot/mission/tests/test_action_fsm_api_key.py` | 19 test cases for action FSM | ✓ |
| `test_mapped_mode_filters.py` | Filter clicking with date picker, metric tabs | ✓ |
| `test_mapped_mode_validation.py` | Node validation, contract validation | ✓ |

### Success Metrics

Track per-task-mode success rate:
- `api_keys.create_api_key` success rate
- `api_keys.copy_api_key` success rate
- `api_keys.delete_api_key` success rate
- `usage_section.read_token_usage` success rate
- `playground.run_model_inference` success rate

**Target**: >90% success rate on golden missions with <10% escape hatch triggers

### Rollout Plan

1. **Phase 1**: Enable for `api_keys`, `usage_section`, `playground` nodes only
2. **Phase 2**: Run golden missions, collect logs
3. **Phase 3**: Tune breach detection based on false positives
4. **Phase 4**: Gradually expand to more nodes once stable

### Files Modified

| File | Changes |
|------|---------|
| `visual_copilot/mission/last_mile_tools.py` | Added `contract_breach_detected()`, `should_fallback_to_exploratory()`, enhanced `resolve_control_from_group()`, adaptive FSM transitions |
| `visual_copilot/mission/last_mile.py` | Added structured logging, escape hatch integration |
| `tests/test_contract_coverage.py` | NEW: Per-layer eval harness |

---

*End of Journal (Updated 2026-03-16: Balance Restoration v2.1)*

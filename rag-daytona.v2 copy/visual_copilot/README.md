# Visual CoPilot — Modular Pipeline Architecture

TARA's Visual CoPilot plans the next browser action for every user request. It takes a user goal, the current DOM, and action history — then returns a grounded action (click, type, answer, wait) with narrator speech.

```
POST /api/v1/plan_next_step
  → { session_id, goal, current_url, step_number, action_history }
  ← { success, action: {type, target_id, text}, speech, mission_id, confidence }
```

---

## Directory Layout

```
visual_copilot/
├── __init__.py                      # Re-exports: ultimate_plan_next_step, ultimate_update_constraint
├── constants.py                     # Feature flags, score thresholds, tag/zone sets, canary domains
│
├── api/
│   ├── plan_next_step.py            # API entry → delegates to pipeline.run_pipeline()
│   └── update_constraint.py         # Mission constraint updates
│
├── orchestration/
│   ├── pipeline.py                  # Top-level wrapper: context build, error handling, event emission
│   ├── plan_next_step_flow.py       # Main orchestrator: calls stages 0→9 in sequence
│   ├── bootstrap.py                 # build_context(), require_runtime_modules()
│   ├── completion.py                # terminal_completion_response(), check_if_arrived()
│   ├── decision_router.py           # High-level route_step(): explicit_id → lexical → semantic
│   ├── fallback_controller.py       # fallback_response() for stage failures
│   ├── state_helpers.py             # Reclick-safe detection, exclusion relaxation
│   ├── legacy_core.py               # Backward-compat shim (deprecated)
│   └── stages/
│       ├── intent_stage.py          # Stage 0: Mind Reader → TacticalSchema
│       ├── hive_stage.py            # Stage 1: Hive retrieval → strategy + visual hints
│       ├── session_stage.py         # Stage 2: Amnesia guard, excluded IDs
│       ├── cross_domain_stage.py    # Stage 3: Cross-domain navigation gate
│       ├── mission_stage.py         # Stage 4: Mission create/resume, subgoal lifecycle
│       ├── router_stage.py          # Stage 5: Build router context (labels, mode, strategy lock)
│       ├── router_pre_detective_stage.py  # Stage 6: Arrival check, read-only, gallery, keyword direct
│       ├── router_execution_stage.py     # Stage 7: Lexical grounding with V2 guards
│       ├── detective_stage.py       # Stage 8: Semantic detective + LLM reranker + Tier 3
│       ├── last_mile_stage.py       # Stage 9: Fine-grained extraction/presentation phase
│       └── terminal_stage.py        # Terminal mission state handling
│
├── routing/
│   ├── lexical_router.py            # Token-based keyword matching & scoring
│   ├── semantic_router.py           # Semantic detective wrapper
│   ├── tier3_router.py              # LLM full-DOM fallback (Groq Llama 8b)
│   ├── action_guard.py              # Node classification, tag compat, target validation
│   ├── id_authoritative.py          # Direct [ID: xxx] routing
│   ├── read_only_router.py          # Cognitive read-only extraction path
│   └── gallery_router.py            # Image/gallery click handling
│
├── detection/
│   ├── semantic_detective_service.py # Detective investigate() wrapper
│   ├── candidate_scoring.py         # Lexical overlap scoring
│   ├── candidate_prefilter.py       # Pre-filter before detailed scoring
│   └── reranker.py                  # Post-detective reranking
│
├── mission/
│   ├── mission_service.py           # Mission brain load/create wrapper
│   ├── subgoal_planner.py           # Current subgoal access, hint queries
│   ├── verified_advance.py          # DOM change verification, action recording
│   ├── constraints.py               # Constraint updates on active missions
│   └── last_mile.py                 # Last-mile step planning
│
├── intent/
│   ├── mind_reader_service.py       # Mind Reader LLM wrapper
│   └── schema_normalizer.py         # Domain normalization, flag computation
│
├── memory/
│   ├── cache_service.py             # Schema + hive response caching
│   ├── hive_service.py              # Hive retrieval with step-aware caching
│   └── live_graph_service.py        # DOM node fetching wrapper
│
├── text/
│   ├── tokenization.py              # Tokenize, canonicalize, zone extraction, mode classification
│   ├── label_extraction.py          # Label candidates from query/schema
│   └── normalization.py             # Domain-specific synonym expansion
│
├── models/
│   ├── contracts.py                 # PlanningContext, RoutingDecision, PipelineResult
│   └── events.py                    # Event schemas
│
└── logging/
    ├── config.py                    # emit_event(), get_logger()
    └── context.py                   # Trace context management
```

---

## Pipeline Flow

Every request flows through **10 sequential stages**. Each stage can return an early response (short-circuit) or pass control to the next stage.

```
┌─────────────────────────────────────────────────────────────────┐
│                     api/plan_next_step.py                       │
│                              │                                  │
│                    pipeline.run_pipeline()                       │
│                              │                                  │
│              plan_next_step_flow.ultimate_plan_next_step_impl() │
└──────────────────────────────┬──────────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                     │
          ▼                    ▼                     ▼
   Stage 0: Intent      Stage 1: Hive       Stage 2: Session
   Mind Reader           Strategy +           Amnesia Guard
   → TacticalSchema      Visual Hints         Excluded IDs
          │                    │                     │
          └────────────────────┼─────────────────────┘
                               │
                               ▼
                    Stage 3: Cross-Domain Gate
                    (early exit if domain jump)
                               │
                               ▼
                    Stage 4: Mission
                    Create/Resume, Subgoals,
                    Verified Advance, Last-Mile Handoff
                               │
                    ┌──────────┴──────────┐
                    │                     │
                    ▼                     ▼
             Stage 5: Router        Stage 9: Last Mile
             Context Build          (if enter_last_mile)
                    │                     │
                    ▼                     ▼
             Stage 6: Pre-Detective  Extract/Present
             Arrival│Read-Only│       → complete
             Gallery│Keyword
                    │
                    ▼
             Stage 7: Lexical Router
             (keyword match with V2 guards)
                    │
              ┌─────┴─────┐
              │ hit?       │ miss
              ▼            ▼
           RETURN    Stage 8: Detective
           action    Semantic + LLM Rerank
                           │
                     ┌─────┴─────┐
                     │ confident? │ low
                     ▼            ▼
                  RETURN    Tier 3 Fallback
                  action    (Groq Llama 8b)
                                  │
                            ┌─────┴─────┐
                            │ grounded?  │ no
                            ▼            ▼
                         RETURN       BLOCKED
                         action       "All tiers failed"
```

---

## Stage Details

### Stage 0 — Intent (`intent_stage.py`)

Calls **Mind Reader** to parse the user goal into a `TacticalSchema`:

| Field | Example |
|-------|---------|
| `action` | `ActionIntent.CLICK`, `SEARCH`, `EXTRACTION` |
| `target_entity` | `"iPhone 15 Pro"` |
| `domain` | `"amazon.de"` |
| `first_subgoal` | `"Click Search bar"` |

Cached per `(session_id, goal)` — steps 1+ reuse the same schema with 0 LLM calls.

Feature flags computed here: `keyword_direct_v3`, `subgoal_hints`, `verified_advance`, `last_mile_enabled`.

---

### Stage 1 — Hive Interface (`hive_stage.py`)

Retrieves domain-specific knowledge from **Qdrant Hive Mind**:

- **Strategy**: Ordered subgoal sequence (e.g., `["Click Search", "Type query", "Click first result"]`)
- **Visual Hints**: DOM element annotations (selectors, zones, expected labels)

On step 0: query Hive and cache. Steps 1+: reuse cached response. Unknown domains get an empty response.

---

### Stage 2 — Session State (`session_stage.py`)

**Amnesia Guard**: Frontend sometimes sends `step_number=0` on page reload while the backend has an active mission at step 5. This stage detects the mismatch and trusts the backend state.

**Excluded IDs**: Builds the set of already-clicked element IDs from `action_history` to prevent loops.

**Reclick Relaxation**: Nav links, tabs, and menu items are removed from the exclusion set — safe to re-click.

---

### Stage 3 — Cross-Domain Gate (`cross_domain_stage.py`)

Detects when the user's goal requires a different website. If the Hive strategy includes a `cross_domain_target` (e.g., user is on google.com but goal needs amazon.de), returns a `navigate` action to bridge domains.

---

### Stage 4 — Mission (`mission_stage.py`)

The **mission** persists across multiple requests for the same goal. This stage:

1. **Get or Create** — Load existing mission from Redis, or create a new one with Hive subgoals
2. **Zero-Shot Replan** — If subgoals are exhausted, call ReAct to generate new ones from the live DOM
3. **Verified Advance** — Check if the previous action had its expected effect (DOM changed, subgoal focus visible). If verified, advance to next subgoal. If not, retry or drop.
4. **Last-Mile Handoff** — When strategy subgoals are done but the goal needs extraction/presentation, transition to `phase: last_mile`

Early exits: ReAct fails (blocked), mission already completed (terminal response).

---

### Stage 5 — Router Context (`router_stage.py`)

Builds the routing context consumed by stages 6-8:

- **`subgoal_mode`**: `literal_click`, `literal_type`, `cognitive_read`, `cognitive_navigate`, `ambiguous`
- **`label_policy`**: Expected label candidates extracted from the subgoal (e.g., `["Dashboard", "dashboard"]`)
- **`strategy_authoritative`**: Whether the Hive strategy is locked (prevents drift)
- **`effective_hive_hints`**: Merged visual hints from Hive + subgoal hint expansion

---

### Stage 6 — Pre-Detective (`router_pre_detective_stage.py`)

A set of fast checks before the expensive semantic detective:

| Check | Condition | Result |
|-------|-----------|--------|
| **Explicit ID** | Subgoal contains `[ID: t-abc123]` | Direct click/type on that ID |
| **Arrival Check** | Goal visible in 5+ content nodes | `check_if_arrived()` → complete |
| **Read-Only Terminal** | `cognitive_read` mode, answer visible | Extract answer from DOM text |
| **Gallery Router** | Image grid detected | Click matching image |
| **Keyword Direct** | V3 feature flag, exact label match | Direct routing |

---

### Stage 7 — Lexical Router (`router_execution_stage.py`)

Token-based keyword matching against all interactive DOM nodes.

**Scoring** (`_lexical_ground_candidate()`):
- Explicit token overlap (query terms ∩ node text)
- Target entity token overlap
- Zone boost (nav/sidebar for clicks, main for type)
- Affordance boost (tag compatibility with action mode)
- Text penalty (avoid text-heavy nodes for clicks)

**Acceptance Criteria** (all must pass):
- Score ≥ `LEXICAL_DIRECT_ACCEPT` threshold (0.70)
- At least 1 explicit term overlap
- Zone compatible with action
- If strategy is locked: node matches strategy focus terms

**V2 Router Guards**: Additional rejection checks on canary domains — score threshold, explicit match, label policy, strategy mismatch.

Returns the action directly on hit. Passes to Detective on miss.

---

### Stage 8 — Semantic Detective (`detective_stage.py`)

The main element-finding engine. Three sub-phases:

#### Phase A: Semantic Investigation

Calls `semantic_detective.investigate()` with the query, hive hints, and excluded IDs. Returns candidates sorted by `hybrid_score` (weighted semantic + hive scores).

#### Phase B: LLM Reranking

If no strong accept (score < 0.70) and multiple candidates exist, an **LLM reranker** (Llama 8b) picks the best candidate from the top 10:

```
Prompt: "Choose the single best candidate for the current subgoal..."
Returns: integer index [0-9] or -1 (reject all)
```

Validation after reranking:
- Tag mismatch (wants type but candidate is a button) → force Tier 3
- Label mismatch (candidate doesn't match expected labels) → revert to pre-rerank best
- Strategy drift (candidate doesn't match locked strategy) → tank score

#### Phase C: Confidence & Tier 3 Decision

```
needs_tier3 = (confidence == "low" AND NOT label_locked) OR score < DETECTIVE_MIN_SCORE
```

Guards that degrade confidence to "low" (triggering Tier 3):
- **Ambiguity band**: Top two candidates within 0.12 of each other
- **Loop guard**: Same candidate signature rejected ≥ N times
- **Tag incompatibility**: Action mode doesn't match node type
- **Strategy mismatch**: Strategy-locked but candidate off-focus

If Tier 3 is needed, it runs `run_tier3_fallback()`.

---

### Tier 3 Fallback (`tier3_router.py`)

Full-DOM LLM agent using **Groq Llama 8b** with tool calling:

**Tools provided to LLM**:
- `click_element(target_id, draft_thought)`
- `type_text(target_id, text_to_type, draft_thought)`
- `answer_user(final_answer, draft_thought)`

**Input**: Compressed DOM (top 100 interactive nodes), goal, current subgoal.

**Validation**: Every LLM tool call is validated against the live DOM:
- Target ID must exist in nodes
- Target must not be in excluded IDs
- Target must be interactive
- If label policy exists, target must match expected labels
- Click targets walk up the parent chain to find the nearest clickable ancestor

Returns `None` if validation fails → pipeline returns "All 3 tiers failed, blocked."

---

### Stage 9 — Last Mile (`last_mile_stage.py`)

Activated when `enter_last_mile=True` (strategy subgoals exhausted, goal needs extraction).

Pops steps from `mission.last_mile_queue`, validates each against the live DOM, and executes. When the queue produces an `answer` action, marks the mission as `completed` with `phase=done`.

---

## Fallback Chain Summary

```
                    ┌─────────────────┐
                    │  Explicit ID    │  ← Fastest (0 LLM calls)
                    │  [ID: t-xxx]    │
                    └────────┬────────┘
                             │ miss
                    ┌────────▼────────┐
                    │  Lexical Router │  ← Fast (0 LLM calls, token matching)
                    │  keyword match  │
                    └────────┬────────┘
                             │ miss
                    ┌────────▼────────┐
                    │  Detective      │  ← Medium (embedding similarity)
                    │  + LLM Rerank   │     + 1 LLM call for reranking
                    └────────┬────────┘
                             │ low confidence
                    ┌────────▼────────┐
                    │  Tier 3         │  ← Slow (full DOM + Groq tool call)
                    │  LLM Agent      │
                    └────────┬────────┘
                             │ validation fail
                    ┌────────▼────────┐
                    │  BLOCKED        │  ← "Cannot find element"
                    │  no_legacy_fallback │
                    └─────────────────┘
```

Each tier is progressively more expensive but more capable. The pipeline exits at the first tier that produces a confident, grounded action.

---

## Loop Prevention

Multiple mechanisms prevent the agent from clicking the same element forever:

| Mechanism | Where | How |
|-----------|-------|-----|
| **Excluded IDs** | Session Stage | Every click/type target added to exclusion set |
| **Rejection Signatures** | Detective Stage | Per-session hash of `(text, tag, zone)` — blocked after N rejections |
| **Verified Advance** | Mission Stage | If action had no DOM effect after 2 attempts, drop target and try next |
| **Reclick Relaxation** | State Helpers | Nav/tab/menu elements exempt from exclusion (safe to re-click) |
| **Mission Audit** | Detective Stage | `mission_brain.audit_action()` blocks forbidden patterns |

---

## Key Response Fields

```json
{
  "success": true,
  "action": {
    "type": "click | type_text | answer | wait | clarify",
    "target_id": "t-abc123",
    "text": "Dashboard"
  },
  "speech": "Clicking on Dashboard...",
  "mission_id": "uuid",
  "subgoal_index": 2,
  "confidence": "high | medium | low",
  "timing_ms": 245,
  "pipeline": "ultimate_tara | ultimate_tara_tier3",
  "fallback_tier": "id_authoritative | lexical_direct | detective | tier3_after_detective",
  "detective_used": true,
  "detective_score": 0.72,
  "router_mode": "literal_click | literal_type | cognitive_read",
  "complete": false,
  "pending_verification": true,
  "no_legacy_fallback": false
}
```

---

## Configuration (`constants.py`)

| Constant | Default | Purpose |
|----------|---------|---------|
| `DETECTIVE_MIN_SCORE` | 0.45 | Minimum hybrid score to accept detective result |
| `DETECTIVE_AMBIGUOUS_BAND` | 0.12 | Score gap between #1 and #2 — below this → "low" confidence |
| `LEXICAL_DIRECT_ACCEPT` | 0.70 | Minimum lexical score for direct routing |
| `TARA_ROUTER_V2_ENABLED` | env | Enable V2 router guards globally |
| `ENABLE_LAST_MILE_REASONING` | env | Enable last-mile extraction phase |
| `ENABLE_KEYWORD_DIRECT_V3` | env | Enable V3 keyword direct routing (per-domain canary) |
| `ENABLE_VERIFIED_ADVANCE` | env | Enable DOM-change verification before advancing subgoals |
| `ENABLE_SUBGOAL_HINT_QUERY` | env | Enable hint expansion for upcoming subgoals |
| `MAX_DETECTIVE_RETRIES_PER_SUBGOAL` | 2 | Loop guard threshold |

---

## External Dependencies

| Service | Used By | Purpose |
|---------|---------|---------|
| **Mind Reader** (`app.state.mind_reader`) | Intent Stage | Goal → TacticalSchema (Groq Llama 8b) |
| **Hive Interface** (`app.state.hive_interface`) | Hive Stage | Qdrant vector search for domain strategies |
| **Mission Brain** (`app.state.mission_brain`) | Mission Stage | Redis-backed mission state persistence |
| **Semantic Detective** (`app.state.semantic_detective`) | Detective Stage | Hybrid embedding + hive scoring |
| **Live Graph** (`app.state.live_graph`) | All stages | Redis DOM mirror — visible/interactive nodes |
| **Groq API** | Tier 3, Reranker | LLM tool calling (Llama 8b) |

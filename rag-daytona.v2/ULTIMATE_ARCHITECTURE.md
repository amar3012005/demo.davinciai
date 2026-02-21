# TARA Ultimate Architecture

## Overview

The TARA (Tactical Autonomous Reasoning Agent) Ultimate Architecture is a **modular, high-precision visual co-pilot system** designed to handle complex tasks across any website - from dashboards to e-commerce to SaaS applications.

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER INPUT                                │
│                     (Voice/Text/Command)                         │
└────────────────────────────┬────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  mind_reader.py │ ◄── Translates to Schema
                    └────────┬────────┘
                             │ Tactical Schema
                    ┌────────▼─────────┐
                    │ hive_interface.py│ ◄── Retrieves Strategy + Hints
                    └────────┬─────────┘
                             │ Strategy Plan + Visual Hints
                    ┌────────▼──────────┐
                    │  mission_brain.py │ ◄── Creates Mission + Sub-goals
                    └─────────┬──────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
    ┌─────────▼─────────┐          ┌─────────▼──────────┐
    │ BROWSER           │          │  REASONING LOOP    │
    │  tara_sensor.js   │          │  (mission_brain)   │
    └─────────┬─────────┘          └─────────┬──────────┘
              │ Deltas                        │
    ┌─────────▼─────────┐                    │
    │  live_graph.py    │◄───────────────────┤
    │  (Redis Mirror)   │   Queries Graph    │
    └─────────┬─────────┘                    │
              │                              │
    ┌─────────▼───────────┐                  │
    │ semantic_detective.py│◄─────────────────┘
    │ (Hybrid Scoring)     │   Requests Target
    └─────────┬────────────┘
              │ Target Node ID
    ┌─────────▼────────────┐
    │  mission_brain.py    │ ◄── Constraint Audit
    │  (Logic Guard)       │
    └─────────┬────────────┘
              │ Approved Action
    ┌─────────▼────────────┐
    │   EXECUTOR           │
    │  (Browser Widget)    │
    └──────────────────────┘
```

## Module Architecture

### Phase 1: Foundation

#### `tara_models.py` - Data Structures
**Purpose:** Centralized type-safe data models for the entire system.

**Key Classes:**
- `ActionIntent` - Enum: NAVIGATION, EXTRACTION, INTERACTION, PURCHASE, SEARCH
- `TacticalSchema` - User intent with constraints
- `StrategyHint` - High-level navigation sequence from Qdrant
- `VisualHint` - Low-level element selectors from Qdrant
- `GraphNode` - DOM element mirror in Redis
- `MissionState` - Persistent mission tracking
- `Constraint` / `ConstraintStatus` - Constraint enforcement
- `ScoredCandidate` / `DetectiveReport` - Element scoring output
- `DomDelta` - Incremental DOM updates

**Example:**
```python
from tara_models import TacticalSchema, ActionIntent

schema = TacticalSchema(
    action=ActionIntent.EXTRACTION,
    target_entity="API usage data",
    domain="groq.com",
    constraints={"date_range": "last_30_days"}
)
print(schema.missing_constraints())  # []
print(schema.to_query_string())  # "action:extraction domain:groq.com date_range:last_30_days"
```

---

### Phase 2: Perception Layer

#### `tara_sensor.js` - Client-Side Delta Streamer
**Purpose:** Real-time DOM change detection and streaming to server.

**Features:**
- MutationObserver for DOM changes
- Debounced transmission (150ms default)
- Stable ID generation (DJB2 hash)
- Client-side filtering (SVG, decorative elements)
- Zone classification (nav, main, modal, sidebar)

**Usage:**
```javascript
// In tara-widget.js
this.sensor = new TaraSensor(this.ws, {
    sendFullScanOnInit: true,
    debounceMs: 150,
    maxBatchSize: 50
});
this.sensor.start();

// Server receives:
// {type: 'dom_delta', delta_type: 'update', changes: [...]}
```

#### `live_graph.py` - Redis DOM Mirror
**Purpose:** Server-side DOM mirror backed by Redis for instant querying.

**Redis Key Schema:**
- Graph index: `graph:{session_id}` (Set of node IDs)
- Node data: `graph:{session_id}:node:{node_id}` (JSON)
- TTL: 3600s

**API:**
```python
from live_graph import LiveGraph

live_graph = LiveGraph(redis_client)

# Ingest delta from browser
await live_graph.ingest_delta(session_id, delta_dict)

# Query visible nodes
nodes = await live_graph.get_visible_nodes(session_id)

# Find by ID
node = await live_graph.find_by_id(session_id, "tara-abc123")

# Get buttons
buttons = await live_graph.get_buttons(session_id)
```

---

### Phase 3: Intelligence Layer

#### `mind_reader.py` - Input Translator
**Purpose:** Convert raw user input into structured TacticalSchema.

**Features:**
- LLM-based intent parsing (Llama-3.1-8B)
- Heuristic fallback when LLM unavailable
- Input sanitization (filler word removal)
- Domain extraction from URL

**Example:**
```python
from mind_reader import MindReader

mind_reader = MindReader(groq_provider)

schema = await mind_reader.translate(
    user_input="Find my API usage for last month",
    current_url="https://groq.com/dashboard"
)
# schema.action = ActionIntent.EXTRACTION
# schema.constraints = {"date_range": "last_month"}
```

#### `hive_interface.py` - Qdrant Dual-Retrieval
**Purpose:** Retrieve strategy sequences and visual hints from Qdrant.

**Document Types:**
1. `Strategy_Sequence` - High-level navigation plans
2. `Visual_Hint` - Element selectors and patterns
3. `Website_Map` - Legacy site maps

**Example:**
```python
from hive_interface import HiveInterface

hive = HiveInterface(qdrant_client, embeddings, redis_client)

response = await hive.retrieve(schema)

# Strategy for planning
print(response.strategy.sequence)  
# ["Navigate to Dashboard", "Click Usage", "Export data"]

# Visual hints for detection
for hint in response.visual_hints:
    print(hint.selector)  # "#export-btn"
```

---

### Phase 4: Decision Layer

#### `semantic_detective.py` - Hybrid Scoring Engine
**Purpose:** Score and rank candidate elements using semantic + hive hints.

**Scoring Algorithm:**
```
hybrid_score = (semantic_score × 0.6) + (hive_score × 0.4)
```

**Example:**
```python
from semantic_detective import SemanticDetective

detective = SemanticDetective(live_graph)

report = await detective.investigate(
    session_id="session-123",
    query="export button",
    hive_hints=[visual_hint]
)

print(report.best_match.node_id)  # "tara-abc123"
print(report.best_match.hybrid_score)  # 0.87
print(report.confidence)  # "high"
```

#### `mission_brain.py` - Constraint Enforcer
**Purpose:** Create missions, enforce blocking rules, audit actions.

**Constraint Enforcement:**
Before any action executes, `audit_action()` checks:
1. Are all blocking constraints filled?
2. Has this action been tried and failed?
3. Is the target element still valid?

**Example:**
```python
from mission_brain import MissionBrain

brain = MissionBrain(redis_client, hive_interface)

# Create mission
mission = await brain.create_mission(session_id, schema)

# Audit action before execution
approved, reason = await brain.audit_action(
    mission_id=mission.mission_id,
    action_type="click",
    target_id="add-to-cart-btn",
    target_text="Add to Cart"
)

if not approved:
    print(reason)  # "Cannot Add to Cart until size is selected"
```

---

## Integration Flow

### Complete User Request Flow

```python
# 1. User speaks/types
user_input = "Export my API usage for last month"

# 2. Mind Reader translates to schema
schema = await mind_reader.translate(
    user_input=user_input,
    current_url="https://groq.com"
)

# 3. Hive retrieves strategy + hints
hive_response = await hive.retrieve(schema)
strategy = hive_response.strategy  # Navigation plan
hints = hive_response.visual_hints  # Element selectors

# 4. Mission Brain creates mission
mission = await mission_brain.create_mission(
    session_id=session_id,
    schema=schema,
    strategy=strategy
)

# 5. Tara Sensor streams DOM deltas
# (Happens in browser, automatic)

# 6. Live Graph maintains DOM mirror
await live_graph.ingest_delta(session_id, delta)

# 7. Reasoning Loop (per sub-goal)
for subgoal in mission.subgoals:
    # 7a. Detective investigates
    report = await semantic_detective.investigate(
        session_id=session_id,
        query=subgoal,
        hive_hints=hints
    )
    
    # 7b. Mission Brain audits action
    approved, reason = await mission_brain.audit_action(
        mission_id=mission.mission_id,
        action_type=report.recommended_action,
        target_id=report.best_match.node_id
    )
    
    # 7c. Execute if approved
    if approved:
        await execute_action(report.best_match)
        await mission_brain.advance_subgoal(mission.mission_id)
```

---

## Error Handling & Degradation

### Redis Unavailable
```python
# live_graph.py returns empty list
# mission_brain.py falls back to in-memory state
# System continues with degraded persistence
```

### Qdrant Unavailable
```python
# hive_interface.py returns None/empty
# semantic_detective.py uses pure semantic scoring
# mission_brain.py uses heuristic sub-goals
```

### LLM Timeout
```python
# mind_reader.py uses heuristic fallback
# Keyword-based intent detection
# Constraint extraction from input
```

### WebSocket Disconnect
```python
# tara_sensor.js buffers deltas locally
# Reconnects automatically
# Resends buffered deltas on reconnect
```

---

## Redis Key Schema

```
# Graph (DOM Mirror)
graph:{session_id}                    # Set of node IDs
graph:{session_id}:node:{node_id}     # JSON node data

# Mission State
mission:{mission_id}                  # JSON mission data

# Cache
hive:cache:{hash}                     # Cached HiveResponse

# TTL: 3600s (1 hour) for all keys
```

---

## Qdrant Document Schema

### Strategy_Sequence
```json
{
  "doc_type": "Strategy_Sequence",
  "domain": "groq.com",
  "action": "extraction",
  "sequence": ["Navigate to Dashboard", "Click Usage", "Export data"],
  "constraints_order": ["date_range"],
  "blocking_rules": {"Export": ["date_range"]},
  "example_url": "https://groq.com/dashboard",
  "text": "Strategy for extraction on groq.com: Navigate to Dashboard → Click Usage → Export"
}
```

### Visual_Hint
```json
{
  "doc_type": "Visual_Hint",
  "domain": "groq.com",
  "entity": "export button",
  "selector": "#export-btn",
  "element_type": "button",
  "zone": "toolbar",
  "text_pattern": "Export.*",
  "text": "export button selector on groq.com: #export-btn"
}
```

---

## Files Created

| Module | Location | Purpose |
|--------|----------|---------|
| `tara_models.py` | `rag-daytona.v2/` | Data structures |
| `live_graph.py` | `rag-daytona.v2/` | Redis DOM mirror |
| `tara_sensor.js` | `orchestra_daytona.v2/static/` | Client delta streamer |
| `mind_reader.py` | `rag-daytona.v2/` | Input translator |
| `hive_interface.py` | `rag-daytona.v2/` | Qdrant retrieval |
| `semantic_detective.py` | `rag-daytona.v2/` | Hybrid scoring |
| `mission_brain.py` | `rag-daytona.v2/` | Constraint enforcer |

---

## Next Steps (Phase 5: Integration)

1. **Update `visual_orchestrator.py`** to use new modules
2. **Update `tara-widget.js`** to initialize TaraSensor
3. **Add WebSocket handlers** for `dom_delta` messages
4. **Test end-to-end flow** with real user requests

---

## Design Principles

1. **Modularity** - Each module has single responsibility
2. **Graceful Degradation** - System works when components fail
3. **Type Safety** - All data flows through TaraModels
4. **Constraint Enforcement** - Never execute invalid actions
5. **Incremental Updates** - Delta streaming, not snapshots
6. **Hybrid Scoring** - Semantic + learned hints
7. **State Persistence** - Redis-backed mission tracking

---

## Generic Website Handling

This architecture is **not e-commerce specific**. It handles:

- **Dashboards** (SaaS apps, analytics)
- **Documentation sites** (navigation, search)
- **E-commerce** (products, filters, checkout)
- **Forms** (multi-step, validation)
- **Data tables** (sorting, filtering, export)
- **Settings pages** (configuration, toggles)

The key is the **generic constraint system** and **semantic scoring** that adapts to any domain.

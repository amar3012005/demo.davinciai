# Visual CoPilot — Modular Architecture Map
> `rag-daytona.v2/visual_copilot/` — Full file index with roles

---

## Overview

The `visual_copilot` package is a **layered, modular pipeline** for TARA's web navigation intelligence. Every concern is separated into a named module — no monolith. The entry point is `orchestration/pipeline.py` → `plan_next_step_flow.py`, which fans out to dedicated stage files.

---

## Layer Map

```
visual_copilot/
├── api/                        ← HTTP boundary (thin wrappers)
├── orchestration/              ← Core reasoning pipeline
│   ├── pipeline.py             ← TOP-LEVEL ENTRY POINT
│   ├── plan_next_step_flow.py  ← Main orchestration brain
│   ├── stages/                 ← One file per pipeline stage
│   ├── bootstrap.py
│   ├── completion.py
│   ├── decision_router.py
│   ├── fallback_controller.py
│   ├── legacy_core.py
│   └── state_helpers.py
├── mission/                    ← Last-mile compound agent
├── routing/                    ← Action routing / guard rails
├── detection/                  ← Candidate scoring & reranking
├── intent/                     ← Mind Reader (schema parsing)
├── memory/                     ← Hive, LiveGraph, Cache
├── models/                     ← Shared data contracts
├── logging/                    ← Structured event emission
├── text/                       ← Text utilities
├── prompts/                    ← LLM prompt templates
└── constants.py                ← Shared configuration values
```

---

## 📂 `api/` — HTTP Boundary

| File | Role |
|------|------|
| `api/__init__.py` | Re-exports |
| `api/plan_next_step.py` | Thin FastAPI route handler → delegates to pipeline |
| `api/update_constraint.py` | Mission constraint update endpoint |

---

## 📂 `orchestration/` — Core Pipeline

| File | Role |
|------|------|
| **`pipeline.py`** | ⭐ **TOP ENTRY POINT.** Called by `ultimate_api.py`. Builds context, calls `ultimate_plan_next_step_impl`, handles mission terminal shortcuts |
| **`plan_next_step_flow.py`** | ⭐ **MAIN BRAIN.** Runs all pipeline stages in order: session → intent → hive → mission → last_mile → detective → router → fallback |
| `bootstrap.py` | Builds `PlanningContext` from `app.state` modules; validates runtime module availability |
| `completion.py` | Terminal detection — checks if a mission is already done (shortcut before re-planning) |
| `decision_router.py` | Routes between legacy planner and Ultimate TARA based on feature flags |
| `fallback_controller.py` | Owns the legacy fallback logic when Ultimate TARA returns nothing |
| `legacy_core.py` | Thin adapter that calls the legacy `visual_copilot_v1.py` |
| `state_helpers.py` | Shared state utilities (DOM signature, loading signals, stagnation detection) |

### 📂 `orchestration/stages/` — One Stage Per Concern

| File | Role |
|------|------|
| **`session_stage.py`** | Validates session, sets up mission context |
| **`intent_stage.py`** | Calls Mind Reader → produces `TacticalSchema` |
| **`hive_stage.py`** | Retrieves strategy + visual hints from Qdrant HiveMind |
| **`mission_stage.py`** | Creates/loads Mission object, runs subgoal planning |
| **`last_mile_stage.py`** | ⭐ **LAST MILE COORDINATOR.** Checks last-mile triggers, runs queue, fires `run_compound_last_mile` |
| **`detective_stage.py`** | Semantic Detective — scores + ranks DOM candidates |
| **`router_pre_detective_stage.py`** | Pre-detective routing (zero-shot, Tier 1/2 shortcuts) |
| **`router_execution_stage.py`** | Final action selection + confidence guard |
| **`router_stage.py`** | Routes to Tier 3 router if detective fails |
| **`cross_domain_stage.py`** | Handles cross-domain navigation triggers |
| **`terminal_stage.py`** | Handles `answer` / `complete` action types |

---

## 📂 `mission/` — Compound Last-Mile Agent

> This is the **inner loop** where Groq's compound tool system powers TARA's final steps.

| File | Role |
|------|------|
| **`last_mile.py`** | ⭐ **`run_compound_last_mile()`** — The agentic LLM loop. Calls Groq with tools, iterates until terminal action or max iterations |
| **`last_mile_tools.py`** | Tool schema definitions (`click_element`, `type_text`, `scroll_page`, `wait_for_ui`, `request_vision`, `complete_mission`) |
| **`tool_executor.py`** | ⭐ Executes tool calls: ID validation guardrail, vision pipeline, DOM actions |
| **`screenshot_broker.py`** | ⭐ **Vision broker.** Session WS registry + async Future-based screenshot request/resolve |
| `constraints.py` | `update_constraint()` — updates mission constraints (dates, amounts, etc.) |
| `mission_service.py` | Thin service wrapper around MissionBrain CRUD |
| `subgoal_planner.py` | Breaks a goal into ordered subgoals |
| `verified_advance.py` | Validates that a subgoal was actually completed before advancing |

---

## 📂 `routing/` — Action Routing & Guard Rails

| File | Role |
|------|------|
| **`action_guard.py`** | Validates proposed actions: ID existence, excluded IDs, loop detection, hallucination guard |
| `lexical_router.py` | Keyword/regex-based action routing (fast path, no LLM) |
| `semantic_router.py` | Semantic similarity routing for candidate selection |
| `read_only_router.py` | Routes read-only/extraction goals (answer without clicking) |
| `gallery_router.py` | Specialized routing for gallery/grid/search results pages |
| `id_authoritative.py` | Authoritative ID lookup — checks LiveGraph for exact ID match |
| `tier3_router.py` | Deep reasoning router (Tier 3 — last resort before failure) |

---

## 📂 `detection/` — Candidate Scoring & Reranking

| File | Role |
|------|------|
| `semantic_detective_service.py` | Wrapper around `SemanticDetective` — scores candidates vs. goal |
| `candidate_prefilter.py` | Fast pre-filter: limits candidate pool before expensive scoring |
| `candidate_scoring.py` | Scores individual DOM nodes (text match, zone, tag, etc.) |
| `reranker.py` | Re-ranks pre-filtered candidates using semantic scores |

---

## 📂 `intent/` — Mind Reader (Intent → Schema)

| File | Role |
|------|------|
| `mind_reader_service.py` | Calls `MindReader.translate()` → produces `TacticalSchema` from raw user input |
| `schema_normalizer.py` | Normalizes and validates `TacticalSchema` fields |

---

## 📂 `memory/` — State & Retrieval Services

| File | Role |
|------|------|
| `hive_service.py` | Retrieves strategy hints + visual hints from Qdrant HiveMind via `HiveInterface` |
| `cache_service.py` | In-memory + Redis caching layer for expensive LLM/DB calls |
| `live_graph_service.py` | Wraps `LiveGraph` — reads current DOM nodes from Redis mirror |

---

## 📂 `models/` — Shared Data Contracts

| File | Role |
|------|------|
| `contracts.py` | `PlanningContext` dataclass — the shared context object passed through all stages |
| `events.py` | Structured log event schema (used by `logging/config.py`) |

---

## 📂 `logging/` — Structured Event Emission

| File | Role |
|------|------|
| `config.py` | `emit_event()` and `get_logger()` — structured JSON events for every pipeline decision |
| `context.py` | Log correlation context (trace_id, session_id, mission_id) |

---

## 📂 `prompts/` — LLM Prompt Templates

| File | Role |
|------|------|
| `react/navigator_prompt.py` | ⭐ **Main compound agent system prompt** — instructions for the Last Mile agent's tool loop |
| `mind_reader/generic.py` | Generic mind reader system prompt (default for all domains) |
| `mind_reader/groq_com.py` | Domain-specific mind reader prompt for groq.com |
| `mind_reader/engelvoelkers_com.py` | Domain-specific prompt for real estate domain |
| `mind_reader/pornpics_de.py` | Domain-specific prompt override |
| `mission_end/end_dialogue_prompt.py` | Prompt for mission completion dialogue |
| `mission_end/validate_prompt.py` | Prompt for validating that a goal is truly complete |

---

## 📂 `text/` — Text Utilities

| File | Role |
|------|------|
| `tokenization.py` | Tokenization helpers (token counting, chunking) |
| `label_extraction.py` | Extracts visible labels from DOM elements |
| `normalization.py` | Text normalization (lowercasing, unicode, etc.) |

---

## Root-Level Files in `visual_copilot/`

| File | Role |
|------|------|
| `constants.py` | Shared constants (max attempts, confidence thresholds, zone weights, etc.) |
| `orchestrator.py` | Legacy thin wrapper — kept for backward compat |
| `arrival_detector.py` | Re-export of the arrival detection logic |
| `last_mile.py` | Thin re-export → `mission/last_mile.py` |
| `hive_interface.py` | Re-export → HiveInterface |
| `live_graph.py` | Re-export → LiveGraph |
| `mind_reader.py` | Re-export → MindReader |
| `mission_brain.py` | Re-export → MissionBrain |
| `mission_planner.py` | Re-export → SubgoalPlanner |
| `mission_validator.py` | Re-export → validated advance logic |
| `semantic_detective.py` | Re-export → SemanticDetective |
| `semantic_detector.py` | Re-export alias |
| `node_classifier.py` | Node zone/type classifier (nav, main, modal, etc.) |
| `tara_models.py` | Re-export → `tara_models.py` (root level) |
| `text_utils.py` | Re-export → text utilities |
| `tier3_fallback.py` | Re-export → Tier 3 router fallback |
| `keyword_matcher.py` | Fast keyword match utility |
| `read_only_terminal.py` | Terminal detection for read-only/answer responses |

---

## 🔗 Vision Pipeline Integration (v6)

The files added/modified for Groq Vision multimodal integration:

```
mission/
├── screenshot_broker.py    ← NEW: async Future broker for WS-based screenshot requests
├── tool_executor.py        ← UPDATED: request_vision calls Groq Vision API
└── last_mile.py            ← UPDATED: passes session_id + screenshot_b64 through

orchestration/stages/
└── last_mile_stage.py      ← UPDATED: passes screenshot_b64 → run_compound_last_mile

orchestration/
└── plan_next_step_flow.py  ← UPDATED: screenshot_b64 in handle_last_mile_stage call

pipeline.py                 ← UPDATED: screenshot_b64 param through to impl

llm_providers/
└── groq_provider.py        ← UPDATED: generate_vision() for llama-4-scout-17b
```

---

## Call Flow (Happy Path)

```
HTTP POST /api/v1/plan_next_step
  → ultimate_api.py::ultimate_plan_next_step()
    → pipeline.py::run_pipeline()
      → plan_next_step_flow.py::ultimate_plan_next_step_impl()
        │
        ├─ session_stage.py       (validate session)
        ├─ intent_stage.py        (MindReader → TacticalSchema)
        ├─ hive_stage.py          (Qdrant → strategy + hints)
        ├─ mission_stage.py       (create/load Mission + subgoals)
        ├─ last_mile_stage.py     (last mile check)
        │   └─ mission/last_mile.py::run_compound_last_mile()
        │       └─ Groq compound tool loop
        │           ├─ request_vision → tool_executor.py → Groq Vision
        │           ├─ click_element → guardrail → frontend action
        │           └─ complete_mission → answer
        │
        ├─ router_pre_detective_stage.py  (zero-shot / Tier 1/2)
        ├─ detective_stage.py             (SemanticDetective scoring)
        ├─ router_execution_stage.py      (final action + confidence)
        └─ tier3_router.py                (last resort)
```

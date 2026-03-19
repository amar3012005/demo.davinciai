# Mapped Mode Contract-First Migration

## Overview

This migration transforms mapped mode from **vision-driven** to **contract-first** architecture. The site map becomes the SOURCE OF TRUTH, with vision as an advisory optimizer.

## What Changed

### 1. Contract-First Priority (BREAKING)

**Before (vision-first):**
```
Vision hints → DOM search → Site map fallback
```

**After (contract-first):**
```
Site map contract (control_groups) → Vision advisory → DOM fallback
```

### 2. New Site Map Schema Fields

Add these fields to each node in `site_map.json`:

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

  "required_fields": ["name"],  // For form fill tasks

  "expected_controls": ["Date Picker", "Model Filter"],
  "terminal_capabilities": ["read_token_usage"]
}
```

### 3. New Task Types

`MappedTaskType` enum replaces one-size-fits-all extraction:

| Task Type | FSM | Use Case |
|-----------|-----|----------|
| `READ_EXTRACT` | `MappedExtractionStateMachine` | "Show me Whisper token usage" |
| `CREATE_ACTION` | `MappedActionStateMachine` | "Create a new API key" |
| `FORM_FILL` | `MappedFormFillStateMachine` | "Update my profile" |
| `CONFIRM_ACTION` | (TBD) | "Yes, delete this project" |

### 4. Per-Turn Node Resolution

**Before:** Node resolved once at entry
**After:** `resolve_current_node()` called every turn

```python
# In plan_next_step_flow.py
current_node = resolve_current_node(url, dom_summary=nodes)
```

### 5. Vision Advisory Format

**Before:** Raw string
**After:** Structured dict with confidence

```python
vision_hints = {
    "raw_recommendations": "...",  # Intact, not rewritten
    "identified_controls": [...],
    "confidence_scores": {...},
    "advisory_only": True,
    "missing_controls": [...]  # Site map controls vision didn't see
}
```

## Code Changes by File

### `visual_copilot/mission/last_mile_tools.py`

**Added:**
- `MappedTaskType` enum (4 task types)
- `MappedActionState` enum (8 action states)
- `MappedFormFillState` enum (7 form states)
- `MappedTerminalContext.control_groups`, `task_modes`, `completion_contract` fields
- `resolve_control_from_group()` method (node-local search)
- `MappedActionStateMachine` class
- `MappedFormFillStateMachine` class
- `validate_completion_contract()` function

**Modified:**
- `_transition_set_filters()` - Priority: control_groups → vision → expected_controls → DOM
- `MappedTerminalContext.from_site_map()` - Uses `resolve_current_node()`

### `visual_copilot/mission/last_mile.py`

**Added:**
- `classify_task_type()` function
- `_run_action_fsm()` helper
- `_run_form_fill_fsm()` helper
- `_run_read_extract_fsm()` helper

**Modified:**
- `_run_mapped_last_mile()` - Routes to task-type-specific FSM

### `visual_copilot/orchestration/plan_next_step_flow.py`

**Added:**
- Per-turn `resolve_current_node()` call
- Contract-first handoff to last_mile

### `visual_copilot/navigation/site_map_validator.py`

**Added:**
- `resolve_current_node(url, dom_summary, nodes)` method

### `visual_copilot/mission/tool_executor.py`

**Added:**
- `process_vision_result()` - Preserves raw vision, adds confidence

**Modified:**
- `_is_valid_id()` - Accepts radix-..., aria-... when DOM-grounded

### `site_map.json`

**Added to usage_section:**
- `control_groups`
- `task_modes`
- `completion_contract`

**Added api_keys node:**
- Full node for API key creation testing

## Migration Steps

### Step 1: Update Site Map

Add `control_groups`, `task_modes`, `completion_contract` to all mapped nodes:

```bash
# Check current nodes
python3 -c "import json; print(json.load(open('site_map.json')))"

# Edit site_map.json to add new fields
```

### Step 2: Update Feature Flags

Enable new contract-first mode:

```python
# In your config or .env
MAPPED_CONTRACT_FIRST_ENABLED=true
MAPPED_TASK_TYPE_FSM_ENABLED=true
MAPPED_VISION_ADVISORY_ENABLED=true
```

### Step 3: Test Contract-First

```bash
# Run contract-first tests
python3 tests/test_contract_first_mapped_mode.py

# Run vision advisory tests
python3 visual_copilot/mission/tests/test_vision_advisory_not_authoritative.py

# Run node-local resolution tests
python3 visual_copilot/mission/tests/test_node_local_control_resolution.py
```

### Step 4: Test Task-Type FSMs

```bash
# Action FSM (API key creation)
python3 visual_copilot/mission/tests/test_action_fsm_api_key.py

# Form fill FSM
python3 visual_copilot/mission/tests/test_form_fill_profile_update.py  # TBD
```

## Acceptance Criteria

| Criterion | Status |
|-----------|--------|
| Node resolved every turn | ✓ |
| `control_groups` in site map | ✓ |
| Vision is advisory (not truth) | ✓ |
| Vision misses control → site map still finds it | ✓ |
| "Create API key" → CREATE_ACTION FSM | ✓ |
| Action FSM has CTA workflow | ✓ |
| Extraction FSM still works | ✓ |
| Non-t-... IDs accepted (radix, aria) | ✓ |

## Regression Tests

### Test 1: Usage Extraction (READ_EXTRACT)

```python
goal = "Show me Whisper token usage in last 7 days"
# Expected flow:
# 1. resolve_current_node() → usage_section
# 2. classify_task_type() → READ_EXTRACT
# 3. MappedExtractionStateMachine:
#    - VALIDATE_NODE → SET_FILTERS (control_groups["date_filters"])
#    - SET_FILTERS → click date picker
#    - LOCATE_ENTITY → find "Whisper"
#    - LOCATE_METRIC → click "Activity" tab
#    - EXTRACT_VALUE → read tokens
#    - VALIDATE_EVIDENCE → contract met
#    - COMPLETE
```

### Test 2: API Key Creation (CREATE_ACTION)

```python
goal = "Create a new API key for production"
# Expected flow:
# 1. resolve_current_node() → api_keys_section
# 2. classify_task_type() → CREATE_ACTION
# 3. MappedActionStateMachine:
#    - VALIDATE_NODE → primary_cta = "Create API Key"
#    - FIND_PRIMARY_CTA → find button with "Create API Key"
#    - CLICK_PRIMARY_CTA → click button
#    - VERIFY_MODAL_OR_FORM → modal appeared
#    - FILL_REQUIRED_FIELDS → fill "name" field
#    - CLICK_CONFIRM → click "Create"
#    - VERIFY_SUCCESS → "API key created"
#    - COMPLETE
```

## Rollback

If issues occur, disable new features:

```python
# In last_mile_tools.py or config
MAPPED_CONTRACT_FIRST_ENABLED = False
MAPPED_TASK_TYPE_FSM_ENABLED = False
MAPPED_VISION_ADVISORY_ENABLED = False
```

This reverts to legacy extraction-only FSM with vision-first priority.

## Known Issues

1. **Site map incompleteness**: Some nodes may lack `control_groups`. Fallback to `expected_controls` → DOM search.

2. **Vision format transition**: Legacy vision (string) still supported. New vision (dict) preferred.

3. **Non-t-... IDs**: Now accepted when DOM-grounded. Guardrail still logs warnings for unknown formats.

## Next Steps

1. Add `control_groups` to all remaining site map nodes
2. Implement `CONFIRM_ACTION` FSM for confirmation dialogs
3. Add telemetry for contract-first vs legacy success rates
4. Create site map editor UI for adding `control_groups`

---

Generated: 2026-03-16
Migration: Contract-First Mapped Mode v2.0

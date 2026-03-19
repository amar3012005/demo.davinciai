# Implementation Summary: Restore Balance Between Determinism and Adaptability

**Date**: 2026-03-16
**Status**: COMPLETED

## Overview

This implementation restores balance between determinism (pre-route) and adaptability (last-mile) in the contract-first mapped mode system. The system now behaves less like a rigid workflow engine and more like a contract-guided adaptive agent.

## Changes Made

### 1. Contract Breach Detection Escape Hatch (`last_mile_tools.py`)

**Location**: Lines 1755-2020 (approximate)

**New Functions**:
- `contract_breach_detected(observation, context)` - Detects when contract assumptions don't match reality
- `should_fallback_to_exploratory(observation, context, fsm_state, attempts)` - Combines breach detection with FSM stall detection

**Breach Conditions Detected**:
1. Control group mismatch - Less than 30% of expected controls found in DOM
2. Primary CTA mismatch - Contract defines CTA but not found in DOM
3. URL pattern mismatch - URL doesn't match expected patterns for node
4. Post-action state mismatch - Expected post-click elements not present
5. FSM stall - Stuck in same state for more than 8 attempts

**Feature Flag**:
```bash
MAPPED_CONTRACT_ESCAPE_HATCH_ENABLED=true  # Default: enabled
```

### 2. Vision-Assisted Control Resolution (`last_mile_tools.py`)

**Location**: `resolve_control_from_group()` method (lines 310-420 approx)

**Priority Order**:
1. **Site map control_groups (CONTRACT)** - First priority, site map is truth
2. **Vision hints with high confidence (ADVISORY)** - Only if confidence > 0.8
3. **Generic DOM search (FALLBACK)** - Last resort keyword matching

**Enhanced Signature**:
```python
def resolve_control_from_group(
    self,
    group_name: str,
    nodes: List[Any],
    clicked_ids: Set[str],
    vision_hints: Optional[Dict[str, Any]] = None,  # NEW
) -> Optional[Dict[str, Any]]:
```

**Returns**:
```python
{
    "target_id": str,
    "intent": {...},
    "from": "contract"|"vision"|"dom"  # Tracks resolution source
}
```

### 3. Adaptive FSM State Transitions (`last_mile_tools.py`)

**Modified State Machine**: `MappedActionStateMachine`

**Key Adaptations**:

#### `_transition_validate_node()` - Adaptive Skip
- Checks if modal/form already visible
- If yes, skips directly to `FILL_REQUIRED_FIELDS`
- Logs: `ADAPTIVE_SKIP: Modal/form already present`

#### `_transition_find_cta()` - Adaptive Skip
- Checks if CTA already clicked (in clicked_ids)
- If yes, skips directly to `VERIFY_MODAL_OR_FORM`
- Logs: `ADAPTIVE_SKIP: CTA already clicked`

#### `_transition_verify_modal()` - Adaptive Skip
- Checks if success already visible
- Checks if all fields already filled
- Skips to `COMPLETE` or `CLICK_CONFIRM` accordingly
- Logs: `ADAPTIVE_SKIP: Success already visible`

#### `_transition_fill_fields()` - Adaptive Skip
- Checks which fields actually need filling based on live DOM
- Skips fields that appear already filled
- If no fields need filling, proceeds to `CLICK_CONFIRM`
- Logs: `ADAPTIVE: Field already filled`

### 4. Contract-Language Structured Logging (`last_mile.py`)

**Location**: `_run_mapped_last_mile()` function

**Structured Log Events**:
```python
logger.info(f"CURRENT_NODE_RESOLVED=node:{node_id}")
logger.info(f"TASK_MODE_SELECTED={task_type.value}")
logger.info(f"GOAL_ENTITY={context.goal_entity}")
logger.info(f"GOAL_METRIC={context.goal_metric}")
logger.info(f"CONTROL_GROUPS_AVAILABLE={list(control_groups.keys())}")
logger.info(f"FSM_TYPE={fsm_type}")
logger.info(f"TRANSITION_STEP={step}")
logger.info(f"CONTROL_GROUP_RESOLVED={group} -> {target_id} (from=contract|vision|dom)")
logger.info(f"VERIFY_SIGNALS_FOUND={signals}")
logger.info(f"COMPLETION_CONTRACT_PASS={True|False}")
```

**Escape Hatch Trigger Log**:
```python
logger.warning(
    f"MAPPED_CONTRACT_ESCAPE_HATCH_TRIGGERED reason={fallback_reason} "
    f"state={machine.state.value} attempts={machine.attempts}"
)
```

### 5. Per-Layer Eval Harness (`tests/test_contract_coverage.py`)

**Location**: New file `tests/test_contract_coverage.py`

**Five Evaluation Layers**:

**Layer A: Node Resolution Eval**
- Tests `SiteMapValidator.resolve_current_node()`
- Verifies correct node found for URL

**Layer B: Task-Mode Routing Eval**
- Tests `classify_task_type()`
- Verifies correct FSM selection

**Layer C: Control Resolution Eval**
- Tests `resolve_control_from_group()`
- Verifies contract-first, vision-fallback behavior

**Layer D: Transition Eval**
- Tests FSM state transitions
- Verifies adaptive skip behavior

**Layer E: Completion Validation Eval**
- Tests `contract_breach_detected()`
- Verifies breach detection logic

**Golden Missions**:
- `api_keys.create_api_key` - Create API key with name
- `api_keys.copy_api_key` - Copy existing key
- `api_keys.delete_api_key` - Delete key with confirmation
- `usage_section.read_token_usage` - Show Whisper token usage (last 7 days)
- `playground.run_model_inference` - Run prompt and verify output

## Design Principles Implemented

### The Right Split

| Layer | Behavior | Why |
|-------|----------|-----|
| **Pre-route** | Deterministic railway tracks | Decides which line, station, transfer. No creativity needed. |
| **Last-mile** | Contract-guided adaptation | Walking inside station - which gate is open, elevator vs stairs. Must adapt. |

### Contract Defines (Not Prescribes)

The site map contract now specifies:
- ✅ Task mode - what kind of work this is
- ✅ Control families - what categories of controls exist
- ✅ Success evidence - what proves completion
- ✅ Verification signals - what to look for after actions
- ❌ ~~Exact step sequence~~ - this makes it brittle

### Live DOM Determines

The live DOM now tells you:
- Which specific control instance matches the contract family
- Whether expected post-control state actually appeared
- If the contract's assumed flow matches reality
- When to abandon contract and fall back to exploration

### Vision as "DOM Helper" Not Dominant

Vision confidence threshold: **0.8+**
- Below 0.8: Vision ignored
- Above 0.8: Vision advisory, contract still truth
- Prevents vision re-domination

## Testing

### Syntax Verification
```bash
python3 -m py_compile visual_copilot/mission/last_mile_tools.py
python3 -m py_compile visual_copilot/mission/last_mile.py
python3 -m py_compile tests/test_contract_coverage.py
```
**Result**: PASSED

### Unit Tests
Run with:
```bash
cd rag-visual-copilot
pytest tests/test_contract_coverage.py -v
```

### Verification Commands
```bash
# Run golden missions and collect logs
cd rag-visual-copilot
python3 -m pytest tests/test_contract_coverage.py::TestGoldenMissions -v -s

# Check logs show contract-language events
docker logs -f orchestrator-<id> | grep -E "CURRENT_NODE_RESOLVED|TASK_MODE_SELECTED|COMPLETION_CONTRACT"
```

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| **Too much escape** - Agent falls back to exploratory too eagerly | Log every escape, tune breach detection thresholds (currently 30% control match, 8 attempts stall) |
| **Vision re-domination** - Vision becomes primary again | Keep vision confidence threshold high (0.8+) |
| **Contract rot** - Site map becomes outdated documentation | Contract breach detection logs staleness, can add staleness detection |

## Rollout Plan

1. **Phase 1**: Enable for `api_keys`, `usage_section`, `playground` nodes only
2. **Phase 2**: Run golden missions, collect logs
3. **Phase 3**: Tune breach detection based on false positives
4. **Phase 4**: Gradually expand to more nodes once stable

## Files Modified

1. `visual_copilot/mission/last_mile_tools.py`
   - Added `contract_breach_detected()` function
   - Added `should_fallback_to_exploratory()` function
   - Enhanced `resolve_control_from_group()` with vision fallback
   - Modified `MappedActionStateMachine` transitions for adaptability

2. `visual_copilot/mission/last_mile.py`
   - Added structured logging for contract-language events
   - Integrated escape hatch fallback in FSM loop
   - Added import for `should_fallback_to_exploratory`

3. `tests/test_contract_coverage.py` (NEW)
   - Per-layer eval harness
   - Golden mission tests

## Success Metrics

Track per-task-mode success rate:
- `api_keys.create_api_key` success rate
- `api_keys.copy_api_key` success rate
- `api_keys.delete_api_key` success rate
- `usage_section.read_token_usage` success rate
- `playground.run_model_inference` success rate

**Target**: >90% success rate on golden missions with <10% escape hatch triggers

## One-Line Summary

Pre-route stays deterministic (railway tracks); last-mile becomes contract-guided but adaptive (walking inside the station), with escape hatches when contract assumptions breach reality.

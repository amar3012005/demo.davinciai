# Mapped Last-Mile Extraction Design Document

## Overview

This document describes the split architecture for last-mile extraction, which divides execution into two explicit paths:

1. **Mapped Last-Mile** (`run_mapped_last_mile`): Deterministic extraction for known sites with site map definitions
2. **Exploratory Last-Mile** (`run_exploratory_last_mile`): Flexible behavior for unknown sites (preserves existing implementation)

## Problem Statement

The original last-mile extraction suffered from **premature completion**:
- Completing missions based on URL parameters alone (e.g., `?model=whisper&date=7days`)
- Treating vision hints as authorization for completion
- Not requiring actual page-grounded evidence

## Solution: Split Architecture

### Feature Flag

```python
MAPPED_LAST_MILE_ENABLED = os.getenv("MAPPED_LAST_MILE_ENABLED", "false")
```

When `false` (default), the system uses the exploratory path preserving all existing behavior.

When `true`, the system checks if the current URL matches a site map pattern. If yes, it uses the mapped path; otherwise, it falls back to exploratory.

## Mapped Extraction State Machine

### States

```
VALIDATE_NODE
    └── Check if expected controls are present
    └── Verify we're on the correct site map node
    └── Transition to: VALIDATE_SCOPE

VALIDATE_SCOPE
    └── Check if current filters match goal requirements
    └── Identify mismatched filters (date range, model, etc.)
    └── Transition to: SET_FILTERS or LOCATE_ENTITY

SET_FILTERS
    └── Apply missing date/model filters
    └── Click date pickers, model selectors
    └── Transition to: LOCATE_ENTITY

LOCATE_ENTITY
    └── Find target entity (Whisper, GPT-4, etc.) in readable content
    └── Scroll if needed
    └── Transition to: LOCATE_METRIC

LOCATE_METRIC
    └── Find metric column/field (tokens, cost, usage)
    └── Click metric tabs/toggles if needed
    └── Transition to: EXTRACT_VALUE

EXTRACT_VALUE
    └── Extract actual numeric value from readable content
    └── Parse numbers with units (1.2M, $0.04, etc.)
    └── Transition to: VALIDATE_EVIDENCE

VALIDATE_EVIDENCE
    └── Verify all completion invariants are satisfied
    └── Block if URL-only evidence detected
    └── Transition to: COMPLETE

COMPLETE
    └── Terminal state - return extraction result
```

### State Transition Rules

1. **No skipping**: Must pass through each state in order
2. **Validation gates**: Each state validates progress before advancing
3. **Escalation**: Can escalate to human if stuck in any state
4. **Max attempts**: Hard limit (default 15) prevents infinite loops

## Completion Invariants

For a mapped extraction to complete, ALL FOUR invariants must be satisfied:

### 1. Entity Anchor Found

```python
entity_anchor_found = True
entity_anchor_text = "Whisper"  # Must appear in readable content
```

The target entity (e.g., "Whisper", "GPT-4") must be explicitly found in the readable page content, not just inferred from URL parameters.

### 2. Metric Anchor Found

```python
metric_anchor_found = True
metric_anchor_text = "tokens"  # Must appear in readable content
```

The metric (e.g., "tokens", "cost", "usage") must be explicitly found in the readable page content.

### 3. Numeric Value Extracted

```python
numeric_value_found = True
numeric_value = "1.2M"  # Must be extracted from readable content
```

An actual numeric value must be extracted from the readable content. Examples:
- `1.2M`
- `$0.004`
- `1,234,567`
- `42`

### 4. Evidence From Page Content (Not URL)

```python
evidence_text = "Whisper usage: 1.2M tokens this month"  # From readable content
# NOT: evidence derived from URL params alone
```

Evidence must come from the actual page content, not just URL parameters. The `_is_url_only_evidence()` function detects when evidence is just echoing URL params.

## Evidence Validation Rules

### URL Evidence May Confirm Filter Scope

URL parameters can be used to:
- Confirm expected date range is set
- Verify model filter is active
- Guide navigation decisions

**But URL evidence NEVER authorizes completion.**

### Vision May Suggest Controls

Vision bootstrap can:
- Identify visible data tables
- Suggest which filters to click
- Guide scroll direction

**But vision hints NEVER authorize completion.**

### Completion Requires Page-Grounded Evidence

Only evidence from `readable_content` (actual DOM text) can satisfy completion invariants.

## Implementation Details

### Files Modified

1. **`visual_copilot/mission/last_mile.py`**
   - Added `MAPPED_LAST_MILE_ENABLED` feature flag
   - Added `_run_mapped_last_mile()` function
   - Modified `run_compound_last_mile()` to split paths
   - Added `_extract_url_params()` helper

2. **`visual_copilot/mission/last_mile_tools.py`**
   - Added `MappedExtractionState` enum
   - Added `MappedTerminalContext` dataclass
   - Added `MappedExtractionStateMachine` class
   - Added `MappedAction` dataclass
   - Added `is_mapped_site()` and `should_use_mapped_mode()` functions

3. **`visual_copilot/mission/tests/test_mapped_last_mile.py`** (new)
   - Regression tests for premature completion blocking
   - State machine transition tests
   - Evidence validation tests

### Key Classes

#### MappedTerminalContext

```python
@dataclass
class MappedTerminalContext:
    url: str
    current_node_id: Optional[str]
    goal_entity: str           # e.g., "Whisper"
    goal_metric: str           # e.g., "tokens"
    goal_filters: Dict[str, str]  # e.g., {"date_range": "last_7_days"}
    is_mapped: bool

    # Evidence tracking
    entity_anchor_found: bool = False
    metric_anchor_found: bool = False
    numeric_value_found: bool = False
    evidence_text: str = ""
```

#### MappedExtractionStateMachine

```python
class MappedExtractionStateMachine:
    def __init__(self, context: MappedTerminalContext, max_attempts: int = 15)
    def transition(self, observation: Dict) -> Tuple[MappedAction, bool]
    def _validate_mapped_completion(self, ...) -> Tuple[bool, str]
```

## Rollout Strategy

### Phase 1: Disabled by Default

```bash
MAPPED_LAST_MILE_ENABLED=false  # Default
```

Existing behavior preserved. Monitor logs for any issues.

### Phase 2: Opt-in Testing

```bash
MAPPED_LAST_MILE_ENABLED=true
```

Enable for specific sites/domains. Collect metrics on:
- Completion rates
- Escalation rates
- Extraction accuracy

### Phase 3: Gradual Rollout

Enable for well-tested site maps first:
- OpenAI Platform usage pages
- Groq console
- Other documented sites

### Phase 4: Default Enable

Once confidence is high, make mapped mode the default for mapped sites.

## Testing

### Regression Tests

1. **URL-only evidence blocked**: Test that `?model=whisper` alone doesn't allow completion
2. **Vision hints don't authorize**: Test that vision "answer visible" doesn't auto-complete
3. **All invariants required**: Test missing entity/metric/value blocks completion
4. **Exploratory fallback**: Test unknown sites use flexible path
5. **State progression**: Test states advance in correct order

### Integration Tests

1. End-to-end extraction on mapped sites
2. Fallback to exploratory on unknown sites
3. Feature flag toggle behavior
4. Site map validator integration

## Backwards Compatibility

The exploratory path (`run_compound_last_mile`) retains all existing:
- Semantic stagnation detection
- Visit graph tracking
- Retry scaffolding
- One-call reasoning
- Tool execution loop

No breaking changes to existing code paths.

## Future Enhancements

1. **Adaptive state timeouts**: Adjust max attempts based on page complexity
2. **Entity synonym resolution**: Recognize "GPT-4" = "gpt-4" = "GPT4"
3. **Multi-value extraction**: Extract multiple entities in one pass
4. **Historical evidence vault**: Cache verified extractions for similar queries

## Summary

This architecture provides:
- **Deterministic extraction** for known sites
- **Strict invariants** preventing premature completion
- **Graceful fallback** to exploratory for unknown sites
- **Feature-flagged rollout** for safe deployment
- **Preserved existing behavior** when disabled

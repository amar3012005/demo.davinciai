# Mapped Last-Mile Completion Invariants

## Quick Reference

For a mapped last-mile extraction to complete successfully, **ALL FOUR** invariants must be satisfied:

| # | Invariant | Validation |
|---|-----------|------------|
| 1 | **Entity Anchor** | Target entity (e.g., "Whisper") found in readable page content |
| 2 | **Metric Anchor** | Metric name (e.g., "tokens") found in readable page content |
| 3 | **Numeric Value** | Actual number (e.g., "1.2M") extracted from readable page content |
| 4 | **Page Evidence** | Evidence comes from DOM text, NOT URL parameters |

## Code Reference

```python
def _validate_mapped_completion(
    self,
    context: MappedTerminalContext,
    readable: str,
    url_params: Dict,
) -> Tuple[bool, str]:
    """Validate that completion meets all mapped-mode invariants."""

    # Invariant 1: Entity anchor
    if not context.entity_anchor_found:
        return False, "Missing entity anchor - entity name not found in readable content"

    # Invariant 2: Metric anchor
    if not context.metric_anchor_found:
        return False, "Missing metric anchor - metric name not found in readable content"

    # Invariant 3: Numeric value
    if not context.numeric_value_found:
        return False, "Missing numeric value - no number extracted from readable content"

    # Invariant 4: Evidence from page, not URL
    url_only_evidence = self._is_url_only_evidence(
        context.evidence_text,
        url_params,
    )
    if url_only_evidence:
        return False, "Evidence appears to be URL-only - must extract from readable page content"

    return True, "All invariants satisfied"
```

## Examples

### Valid Completion

```python
# Readable content: "Whisper usage: 1.2M tokens this month"
context.entity_anchor_found = True   # "Whisper" found
context.metric_anchor_found = True   # "tokens" found
context.numeric_value_found = True   # "1.2M" found
evidence_text = "Whisper usage: 1.2M tokens this month"
url_params = {"model": "whisper"}

# Validation: PASSED
# All invariants satisfied, evidence is page-grounded
```

### Invalid: URL-Only Evidence

```python
# URL: /usage?model=whisper&date=7days
# Readable content: "Usage Dashboard"
context.entity_anchor_found = True   # "Whisper" from URL
context.metric_anchor_found = False  # No metric in content
context.numeric_value_found = False  # No value in content
evidence_text = "model=whisper, date=7days"  # Echoes URL
url_params = {"model": "whisper", "date": "7days"}

# Validation: FAILED
# Evidence is URL-only, not from readable content
```

### Invalid: Missing Metric

```python
# Readable content: "Whisper: 1.2M"
context.entity_anchor_found = True   # "Whisper" found
context.metric_anchor_found = False  # "tokens" not found
context.numeric_value_found = True   # "1.2M" found

# Validation: FAILED
# Metric anchor missing
```

## What Counts as Evidence

### Valid Evidence Sources
- Text nodes in the DOM (`<div>Whisper: 1.2M tokens</div>`)
- Table cells (`<td>1.2M</td>`)
- Chart labels (if extracted via OCR/vision)
- Input values (`<input value="1.2M">`)

### Invalid Evidence Sources
- URL query parameters (`?model=whisper`)
- URL path segments (`/usage/whisper`)
- Browser tab titles
- Meta tags only

## Enforcement Points

1. **State Machine**: `VALIDATE_EVIDENCE` state must pass before `COMPLETE`
2. **Tool Execution**: `complete_mission` tool validates invariants before returning
3. **Regression Tests**: Automated tests verify invariant enforcement

## Debugging

When completion is blocked, check logs for:
```
VALIDATION_FAILED: Missing entity anchor
VALIDATION_FAILED: Missing metric anchor
VALIDATION_FAILED: Missing numeric value
VALIDATION_FAILED: Evidence appears to be URL-only
```

## Rollout

During rollout, monitor:
- `MAPPED_LAST_MILE_PATH` log lines (mapped path taken)
- `EXPLORATORY_LAST_MILE_PATH` log lines (fallback to exploratory)
- `mapped_validation_failed` status (invariant blocking)

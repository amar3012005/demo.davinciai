# 04 Add Router Rule

## Preconditions
- Rule intent and safety impact approved.

## Steps
1. Implement deterministic matcher in `routing/*`.
2. Insert in router order before heavier fallback tier.
3. Add action target validation using action guard.
4. Emit `VC_ROUTER_DECISION` with reason code.

## Failure Symptoms and Fixes
- Wrong click target: tighten label/zone constraints.
- Missed actions: lower confidence threshold carefully.

## Verification Checklist
- Rule activates in expected scenario, bypasses in non-target scenarios.

# 05 Debug Playbook

## Preconditions
- Access to application logs.

## Steps
1. Locate `trace_id` for failing session.
2. Follow lifecycle events in order.
3. Identify failing module by `module` field.
4. Reproduce with captured DOM context and goal.

## Common Failures
- Mid-plan completion: inspect completion guard conditions.
- Stuck mission: inspect verified-advance pending state.
- Bad semantic pick: inspect reranker and action guard outputs.

## Verification Checklist
- Root cause narrowed to single module and event.

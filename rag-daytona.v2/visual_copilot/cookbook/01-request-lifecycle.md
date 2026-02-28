# 01 Request Lifecycle

## Preconditions
- Session ID and DOM context available from widget.

## Steps
1. API receives mission request.
2. Pipeline emits `VC_PIPELINE_START`.
3. Intent/Hive/Mission/Router run in orchestrator stack.
4. Action is validated, emitted, and returned.
5. Completion path validates terminal state before final answer.

## Failure Symptoms and Fixes
- Premature completion: confirm terminal-only completion guard.
- Looping steps: inspect mission advance logs and pending-action state.

## Verification Checklist
- Same `trace_id` appears across lifecycle events.

## Example Logs
- `VC_ROUTER_DECISION`, `VC_ACTION_DISPATCHED`, `VC_MISSION_COMPLETED`

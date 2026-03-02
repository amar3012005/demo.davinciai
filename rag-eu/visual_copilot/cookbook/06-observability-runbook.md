# 06 Observability Runbook

## Preconditions
- Structured lifecycle events enabled.

## Core Queries
1. Error rate by `event_name=VC_ERROR` and `module`.
2. Router drift by `VC_ROUTER_DECISION` reason distribution.
3. Completion reliability by `VC_MISSION_COMPLETED` ratio.

## Alert Suggestions
- Spike in `VC_FALLBACK_USED`.
- Increased `tier3` path share.
- Session loop rate increase.

## Verification Checklist
- Dashboards include session_id + mission_id breakdowns.

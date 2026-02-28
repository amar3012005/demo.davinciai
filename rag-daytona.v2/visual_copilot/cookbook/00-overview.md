# 00 Overview

## Preconditions
- RAG service boots with Ultimate modules initialized.

## Steps
1. Request enters `visual_copilot.api.plan_next_step`.
2. Pipeline runs through `visual_copilot.orchestration.pipeline`.
3. Runtime owner is modular flow in `visual_copilot.orchestration.plan_next_step_flow` and stage modules under `visual_copilot.orchestration.stages`.
4. Compatibility wrappers preserve old imports.

## Failure Symptoms and Fixes
- Import errors: verify wrapper module targets.
- Missing modular logs: ensure `vc.*` loggers loaded.

## Verification Checklist
- `ultimate_api` exports both async functions.
- `/api/v1/plan_next_step` returns successful action for known scenario.

## Example Logs
- `{"event_name":"VC_PIPELINE_START", ...}`

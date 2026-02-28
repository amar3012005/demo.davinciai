# 02 Module Ownership

## Preconditions
- Team follows single-owner-per-module rule.

## Ownership
- `api/*`: public entrypoints.
- `orchestration/*`: pipeline orchestration.
- `mission/*`: mission lifecycle and constraints.
- `routing/*`: action decision chain.
- `detection/*`: semantic candidate scoring/reranking.
- `text/*`: tokenization/label parsing.
- `prompts/*`: prompt source of truth.

## Anti-patterns
- Business logic in wrappers.
- Prompt logic in router modules.

## Verification Checklist
- Wrappers only re-export.

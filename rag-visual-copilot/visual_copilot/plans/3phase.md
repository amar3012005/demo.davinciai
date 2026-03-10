# Phased Plan for TARA Reliability Upgrades

## Summary

Implement the three upgrades incrementally in this order:

1. `Upgrade 1`: semantic page stability and loading-aware stagnancy
2. `Upgrade 2`: `Visit Graph v1`, not a full session graph
3. `Upgrade 3`: escalation checkpoint, not full HITL conversation orchestration

This order minimizes risk, improves reliability fastest, and avoids coupling later features to unstable page-state detection.

## Guiding Decisions

- `Upgrade 1` becomes the single source of truth for page-state classification.
- `Upgrade 2` starts as append-only mission memory, not multi-tab graph infrastructure.
- `Upgrade 3` starts as a structured pause/escalate action, not a resumable dialogue system.
- Each phase must be shippable behind flags.
- No phase should require rewriting the entire orchestration stack.

---

# Phase 1: Semantic Stability Engine

## Goal

Replace raw DOM-hash stagnancy with deterministic semantic fingerprints and a loading-state classifier so the agent can distinguish:

- stable page
- loading page
- structural navigation
- interactive-surface change
- content change

## Why First

Everything else depends on this. Without reliable page-state classification:
- last-mile loops miscount progress
- visit memory records noisy transitions
- escalation triggers are low quality

## Scope

### Core behavior
Introduce four deterministic page-state artifacts:
- `heading_fingerprint`
- `interactive_fingerprint`
- `content_fingerprint`
- `loading_state`

Add one comparison result object:
- `PageStateDelta`

### Classification outputs
Every turn should classify the page as:
- `loading`
- `stable_no_change`
- `structure_changed`
- `interactives_changed`
- `content_changed`
- `mixed_changed`

### Files to change
Primary:
- [rag-visual-copilot/visual_copilot/mission/last_mile.py](/Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/mission/last_mile.py)
- [rag-eu/visual_copilot/orchestration/stages/last_mile_stage.py](/Users/amar/demo.davinciai/rag-eu/visual_copilot/orchestration/stages/last_mile_stage.py)

Add new helper module:
- `rag-visual-copilot/visual_copilot/mission/page_state.py`
- optionally mirrored or shared in `rag-eu/visual_copilot/orchestration/`

## Implementation details

### 1. Fingerprint builder
Implement deterministic normalized projections:

- `heading_fingerprint`
  - collect `h1/h2/h3` text or heading-like nodes
  - normalize whitespace/case
  - sort and hash

- `interactive_fingerprint`
  - collect visible interactive nodes
  - fields: role/tag/text/id-prefix/zone
  - exclude volatile attrs/classes
  - sort and hash

- `content_fingerprint`
  - collect visible main/content text
  - tokenize
  - dedupe
  - sort tokens
  - hash

- `loading_state`
  - classify using deterministic keyword + role heuristics:
    - loading
    - skeleton
    - spinner
    - pending
    - shimmer
    - progressbar
  - only inspect main/content zone plus known app shell zones

### 2. Delta comparator
Implement:
- `compare_page_state(previous, current) -> PageStateDelta`

Fields:
- `loading_changed`
- `headings_changed`
- `interactives_changed`
- `content_changed`
- `classification`
- `new_headings`
- `added_interactive_labels`
- `content_token_diff_summary`

### 3. Stagnancy rewrite
In last-mile:
- if current page is `loading`, do not increment stagnancy
- if all three fingerprints unchanged, increment stagnancy
- if any semantic fingerprint changed, reset stagnancy according to change class
- do not treat raw DOM churn as progress

### 4. Prompt injection
Add a compact system note to last-mile prompt:
- page change classification
- what changed
- what did not change

Example:
- `SYSTEM: interactives_changed, content_unchanged, headings_unchanged`
- `New controls: Date Picker, Activity`
- `No new answer evidence yet`

### 5. Feature flags
Add:
- `LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED`
- `LAST_MILE_LOADING_STATE_GUARD_ENABLED`

## Tests

### Unit
- identical DOM with changed volatile attrs -> classified stable
- new heading only -> `structure_changed`
- dropdown open -> `interactives_changed`
- new table data -> `content_changed`
- skeleton state -> `loading`

### Integration
- click causing skeleton then content load should not consume 3 stagnancy resets
- repeated no-op rerender should eventually stagnate
- prompt receives compact delta summary

## Acceptance criteria
- stagnancy no longer depends on raw serialized DOM hash
- loading pages do not count as stagnancy
- logs show deterministic page-state classification per turn
- false “progress” from React churn is materially reduced

---

# Phase 2: Visit Graph v1

## Goal

Add mission-scoped cross-page memory using a lightweight append-only visit log, enough to support:
- evidence persistence
- better backtracking
- cross-domain/auth context
- later escalation diagnostics

## Why This Version

A full session graph is too much plumbing for current tab/session reality. `Visit Graph v1` gets most of the value with much lower complexity.

## Scope

### Data model
Append-only mission-scoped visit records with parent links.

### Files to change
Primary:
- [rag-visual-copilot/visual_copilot/mission/last_mile.py](/Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/mission/last_mile.py)
- [rag-visual-copilot/visual_copilot/orchestration/stages/last_mile_stage.py](/Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/orchestration/stages/last_mile_stage.py)
- [rag-visual-copilot/visual_copilot/orchestration/stages/mission_stage.py](/Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/orchestration/stages/mission_stage.py)

Add:
- `rag-visual-copilot/visual_copilot/mission/visit_graph.py`

Optional persistence:
- Redis-backed if mission state already persists there
- otherwise mission-attached in-memory/serialized object first

## Implementation details

### 1. Visit record
Create:
```python
VisitRecord:
- visit_id
- mission_id
- parent_visit_id
- url
- domain
- page_title
- page_node_id
- entered_at
- exit_action
- evidence_vault
- evidence_score
- semantic_summary
```

### 2. Evidence vault
Store compact mission-relevant evidence, not raw DOM:
- excerpts matching goal/entity/metric
- source labels
- confidence/evidence score

### 3. Recording policy
Create or update visit when:
- URL changes
- semantic page classification says `structure_changed`
- domain changes
- last-mile enters a clearly new section

Do not create a new visit for:
- dropdown opens
- loading interstitials
- small content-only refresh unless configured

### 4. Query API
Support these queries only:
- current visit
- parent visit
- highest evidence visit
- latest same-domain visit

### 5. Backtracking policy
When stuck:
- inspect highest-evidence visit
- if current visit evidence is weak and prior visit evidence stronger, suggest backtrack target in reasoning context

### 6. Cross-domain context
If current domain differs from prior and visit parent exists:
inject:
- `You are on a transitive/auth/support page`
- `Mission target remains on parent domain`

### 7. Feature flags
Add:
- `LAST_MILE_VISIT_GRAPH_V1_ENABLED`
- `LAST_MILE_VISIT_EVIDENCE_VAULT_ENABLED`

## Tests

### Unit
- create first visit
- create child visit on URL change
- no duplicate visit on dropdown open
- evidence vault keeps only relevant excerpts
- highest-evidence query returns correct visit

### Integration
- pricing page -> checkout page retains plan evidence
- auth redirect preserves parent-domain context
- stuck state suggests backtrack candidate from prior visit

## Acceptance criteria
- mission retains structured evidence across page transitions
- last-mile can consult prior high-evidence visits
- no full multi-tab plumbing required
- cross-domain steps get parent-domain context injection

---

# Phase 3: Escalation Checkpoint

## Goal

Replace vague `clarify` exits with a structured `escalate` action that tells the user:
- what blocked the agent
- what evidence was found
- exactly what input is needed

## Why This Version

Current clarify/impossible exits collapse too many failure modes. But a full HITL conversation/resume framework is too much for one pass.

## Scope

### New action type
- `escalate`

### New structured payload
- blockage type
- diagnostic package
- single ask
- resume context blob

### Files to change
Primary:
- [rag-visual-copilot/visual_copilot/mission/last_mile.py](/Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/mission/last_mile.py)
- [rag-visual-copilot/visual_copilot/mission/tool_executor.py](/Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/mission/tool_executor.py)
- [rag-visual-copilot/visual_copilot/orchestration/stages/last_mile_stage.py](/Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/orchestration/stages/last_mile_stage.py)

Frontend contract consumers:
- current frontend action rendering path wherever `clarify` is handled

## Implementation details

### 1. Blockage taxonomy v1
Support these initial types:
- `information_wall`
- `ambiguity_wall`
- `verification_wall`
- `captcha_wall`
- `architecture_wall`

### 2. Escalation payload
```json
{
  "type": "escalate",
  "blockage_type": "ambiguity_wall",
  "speech": "I found multiple candidate values and need one detail from you.",
  "ask": "Which model should I report usage for?",
  "diagnostics": {
    "current_url": "...",
    "current_page_node": "...",
    "recent_actions": [...],
    "evidence_summary": "...",
    "best_visit_id": "...",
    "partial_evidence": [...]
  },
  "resume_context": {
    "mission_id": "...",
    "visit_id": "...",
    "needed_input": "model_name"
  }
}
```

### 3. Detection points
Detect escalation at:
- completion gate rejection with partial evidence -> likely `ambiguity_wall`
- stagnancy on login/auth text -> `information_wall`
- irreversible action with low confidence -> `verification_wall`
- captcha/puzzle markers -> `captcha_wall`
- canvas/pdf/unreadable mechanism -> `architecture_wall`

### 4. Frontend compatibility
For v1:
- frontend may initially render `escalate` similarly to `clarify`
- but payload must remain structured for future resume support

### 5. Feature flag
Add:
- `LAST_MILE_ESCALATION_CHECKPOINT_ENABLED`

## Tests

### Unit
- login wall returns `escalate`, not `clarify`
- ambiguity wall includes specific ask
- verification wall includes action summary and confirmation ask

### Integration
- low-confidence irreversible action pauses with `escalate`
- auth wall carries domain + URL in diagnostics
- impossible-with-evidence becomes escalate, not generic failure

## Acceptance criteria
- major failure modes no longer collapse into generic `clarify`
- `escalate` payload includes one specific ask and structured diagnostics
- frontend can display v1 escalation without requiring full resume system

---

# Rollout Strategy

## Recommended order
1. Ship Phase 1 behind flags
2. Enable on a small domain set like `console.groq.com`
3. Once stable, ship Phase 2 with visit recording only
4. Then enable evidence-vault reads
5. Ship Phase 3 last, first as action payload only, then add frontend polish

## Metrics to watch
- stagnancy exits per mission
- average last-mile turns to success
- false completion gate attempts
- number of repeated clicks on same/nearby controls
- backtrack success rate
- escalation rate by blockage type

---

# Important Interface Changes

## New internal types
- `PageStateSnapshot`
- `PageStateDelta`
- `VisitRecord`
- `VisitGraphV1`
- `EscalationPayload`

## New action type
- `escalate`

## New flags
- `LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED`
- `LAST_MILE_LOADING_STATE_GUARD_ENABLED`
- `LAST_MILE_VISIT_GRAPH_V1_ENABLED`
- `LAST_MILE_VISIT_EVIDENCE_VAULT_ENABLED`
- `LAST_MILE_ESCALATION_CHECKPOINT_ENABLED`

---

# Assumptions and Defaults

- Primary implementation target is [rag-visual-copilot/visual_copilot/mission/last_mile.py](/Users/amar/demo.davinciai/rag-visual-copilot/visual_copilot/mission/last_mile.py).
- Phase 1 classification becomes canonical; no duplicate page-state logic should be introduced elsewhere.
- Visit Graph v1 is mission-scoped and append-only.
- No full multi-tab session graph in this plan.
- Escalation v1 is a structured pause event, not a conversational resume system.
- Page-state logic should stay deterministic and cheap, not embedding-heavy or fuzzy.


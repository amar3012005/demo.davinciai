# ⚡ TARA Last-Mile Compound Protocol: Hierarchical Execution & Logical Verification

The **Last-Mile** is the highest-precision phase of the TARA agentic workflow. It transitions from high-level "strategy" to direct, verified browser interaction. This architecture enforces a strict **"System 2" Verification** protocol to eliminate hallucinations and infinite loops.

---

## 🚀 Core Objectives

1.  **Zero-Hallucination Extraction**: Enforces the **Entity Anchor Rule**—completions are blocked unless the target entity is physically verified in the DOM evidence.
2.  **Latency Pipelining**: Executes multiple UI actions (click + type + wait) in a single reasoning turn, reducing round-trip latency by up to 80%.
3.  **Loop Hardening**: Implements deductive guardrails to detect and break concept-neighbor drifts and redundant navigation cycles.
4.  **Visual Grounding**: Mandatory **Vision Bootstrap** provides a multimodal strategic brief before any physical interaction.
5.  **Semantic Stability**: Distinguishes real progress from React DOM churn using deterministic fingerprints.
6.  **Cross-Page Memory**: Retains evidence across page transitions via append-only visit graph.
7.  **Structured Escalation**: Replaces vague failures with specific blockage classification and user guidance.

---

## 👥 The Hierarchical Subagent Team

The Last-Mile is no longer a single LLM call. It is managed by a specialized team of subagents defined in `agent.yaml`:

| Subagent | Mission | Key Tool / Rule |
| :--- | :--- | :--- |
| **ActionPipeliner** | Optimization | Bundles atomic actions (Click+Type) into efficient execution units. |
| **LogicCritic** | Verification | **Entity Anchor Rule**: Rejects "lazy" completions lacking factual evidence. |
| **BugHunter** | Stability | **None-Safety**: Enforces strict DOM validation and prevents `NameError` drift. |
| **SiteIndexer** | Context | Map-driven navigation using `site_map.json` instead of vector-exploration. |
| **StabilityGuard** | Progress | **Semantic Fingerprinting**: Distinguishes loading states from true stagnancy. |
| **MemoryKeeper** | Evidence | **Visit Graph**: Accumulates evidence across page transitions for backtracking. |
| **EscalationRouter** | Handoff | **Blockage Taxonomy**: Classifies blockages and structures human escalation. |

---

## 🔄 The Protocol Lifecycle

### Phase 0: The Trigger
The mission transitions to Last-Mile via `_should_enter_last_mile.py` when:
- The final subgoal is an **extraction** or **question**.
- The strategy has navigated to a "Leaf Node" of the `site_map.json`.

### Phase 1: Vision Bootstrap
Before Step 1, the agent performs a **Visual Scan**.
- Uses **Groq Llama-4-Scout** to map the physical layout.
- Returns a **Vision Strategic Brief** with the best validated `target_id`.

### Phase 2: Semantic State Capture
The agent captures deterministic semantic fingerprints:
- **Heading Fingerprint**: Hash of h1/h2/h3 text
- **Interactive Fingerprint**: Hash of visible controls
- **Content Fingerprint**: Hash of main content tokens
- **Loading State**: Detects loading/skeleton states

### Phase 3: The Read → Verify → Act Loop
The agent performs up to 8 iterations of a strict behavioral cycle:
1.  **READ**: Analyze "Current Readable Page Content" + Visit Graph context.
2.  **VERIFY**: Check if the goal is already answered by visible text.
3.  **COMPARE**: Calculate PageStateDelta to detect real vs. false progress.
4.  **ACT**: Execute ONE targeted action (or a pipeline of actions).
5.  **RECORD**: Update Visit Graph with new evidence.

### Phase 4: Completion or Escalation
- **Success**: Entity Anchor verified, answer extracted.
- **Stagnancy**: Semantic stagnancy detected, attempt backtrack to high-evidence visit.
- **Blockage**: Structured escalation with blockage taxonomy and diagnostics.

---

## 🛡 Verification Guardrails

### 1. The Entity Anchor Rule (LogicCritic)
The system verifies that the evidence blob actually contains the core entity requested.
```python
# _verify_entity_anchor logic
for term in entity_terms:
    if term in evidence_blob.lower():
        return True # Anchor found
return False # BLOCK Mission Completion
```

### 2. Semantic Stability Engine (StabilityGuard)
Replaces raw DOM-hash stagnancy with deterministic semantic fingerprints.

```python
# Page State Classification
- loading: Page is loading/skeleton state
- stable_no_change: All fingerprints identical (true stagnancy)
- structure_changed: Headings changed (real progress)
- interactives_changed: Controls changed (dropdown opened)
- content_changed: Content changed (new data loaded)
- mixed_changed: Multiple aspects changed
```

**Key Behavior**: Loading states do NOT increment stagnancy counters.

### 3. Visit Graph v1 (MemoryKeeper)
Append-only mission-scoped visit log for cross-page evidence retention.

```python
# Visit Record Structure
VisitRecord:
  - visit_id, mission_id, parent_visit_id
  - url, domain, page_title, page_node_id
  - evidence_vault: List[EvidenceEntry]
  - evidence_score: float
  - semantic_summary: str
```

**Key Behaviors**:
- Auto-links new visits to parent
- Accumulates evidence per visit
- Suggests backtrack to high-evidence pages
- Cross-domain context injection

### 4. Escalation Checkpoint (EscalationRouter)
Structured escalation replacing vague `clarify` exits.

**Blockage Taxonomy**:
- `information_wall`: Missing required info (credentials, API keys)
- `ambiguity_wall`: Multiple options, need clarification
- `verification_wall`: Confirmation needed before irreversible action
- `captcha_wall`: Human verification challenge
- `architecture_wall`: Unsupported mechanism (canvas, PDF)

**Escalation Payload**:
```python
{
  "type": "escalate",
  "blockage_type": "ambiguity_wall",
  "speech": "I found multiple candidate values...",
  "ask": "Which model should I report usage for?",
  "diagnostics": { /* context for debugging */ },
  "resume_context": { /* for future resume */ },
  "suggested_resolutions": ["Option 1", "Option 2", "Option 3"]
}
```

### 5. Semantic Repeat Guard
Detects when the agent is clicking "Concept-Neighbors" (e.g., clicking *API Docs* then *API Guide* then *Developer Home*) without progress. After 3 repeats, it forces a **Vision Escalation**.

### 6. Section Reclick Guard
Prevents the agent from re-clicking sidebar links for sections that are already active in the current DOM view.

---

## 🔧 Feature Flags

All new capabilities are behind feature flags for incremental rollout:

```bash
# Phase 1: Semantic Stability Engine
export LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED=true
export LAST_MILE_LOADING_STATE_GUARD_ENABLED=true

# Phase 2: Visit Graph v1
export LAST_MILE_VISIT_GRAPH_V1_ENABLED=true
export LAST_MILE_VISIT_EVIDENCE_VAULT_ENABLED=true

# Phase 3: Escalation Checkpoint
export LAST_MILE_ESCALATION_CHECKPOINT_ENABLED=true
```

---

## 📂 Key Architecture Files

### Core Implementation
- **`visual_copilot/mission/last_mile.py`**: Loop orchestrator, prompts, and Phase triggers.
- **`visual_copilot/mission/tool_executor.py`**: Hardware-level tool execution & LogicCritic gates.
- **`visual_copilot/mission/screenshot_broker.py`**: Real-time WebSocket screenshot management.
- **`visual_copilot/orchestration/stages/pre_decision_stage.py`**: Routing logic & PageIndex traversal.
- **`visual_copilot/text/tokenization.py`**: Semantic drift detection & term extraction.

### New Reliability Modules (Phased Implementation)
- **`visual_copilot/mission/page_state.py`**: Semantic Stability Engine (Phase 1)
  - `PageStateSnapshot`: Deterministic fingerprints
  - `PageStateDelta`: Change classification
  - `compare_page_state()`: Progress detection
  
- **`visual_copilot/mission/visit_graph.py`**: Visit Graph v1 (Phase 2)
  - `VisitRecord`: Append-only visit log
  - `VisitGraphV1`: Mission-scoped memory
  - `suggest_backtrack()`: Recovery guidance
  
- **`visual_copilot/mission/escalation_checkpoint.py`**: Escalation Checkpoint (Phase 3)
  - `BlockageType`: Taxonomy of blockages
  - `EscalationDetector`: Detection logic
  - `EscalationPayload`: Structured handoff

### Supporting Infrastructure
- **`visual_copilot/navigation/site_map_validator.py`**: Ground-truth navigation validation using `site_map.json`
- **`visual_copilot/mission/verified_advance.py`**: URL-based navigation verification

---

## 📊 Metrics to Watch

Monitor these metrics to validate reliability improvements:

| Metric | Target | Description |
|--------|--------|-------------|
| Stagnancy exits per mission | < 0.3 | Fewer false stagnancy triggers |
| Average last-mile turns | < 5 | Faster extraction completion |
| False completion gate attempts | < 0.1 | Reduced hallucination attempts |
| Repeated clicks on same controls | < 2 | Less redundant navigation |
| Backtrack success rate | > 70% | Effective recovery from wrong paths |
| Escalation rate by type | track | Understand blockage patterns |

---

## 🎯 Rollout Strategy

### Phase 1: Semantic Stability (Week 1-2)
1. Enable `LAST_MILE_SEMANTIC_FINGERPRINT_ENABLED`
2. Monitor stagnancy detection accuracy
3. Verify loading states don't count as stagnancy

### Phase 2: Visit Graph (Week 3-4)
1. Enable `LAST_MILE_VISIT_GRAPH_V1_ENABLED`
2. Monitor evidence accumulation across pages
3. Validate backtrack suggestions

### Phase 3: Escalation (Week 5-6)
1. Enable `LAST_MILE_ESCALATION_CHECKPOINT_ENABLED`
2. Monitor escalation classification accuracy
3. Gather user feedback on escalation clarity

---

> [!IMPORTANT]
> **Logic Integrity**: All code changes to the Last-Mile layer must be verified by both the **BugHunter** (for syntax/safety) and the **LogicCritic** (for semantic validity).

> [!NOTE]
> **Deterministic Progress**: The Semantic Stability Engine ensures that only *semantic* changes (new content, new structure) count as progress—not React re-renders or DOM churn.

> [!TIP]
> **Cross-Page Memory**: The Visit Graph enables the agent to remember evidence found on previous pages, supporting better backtracking and cross-domain navigation.

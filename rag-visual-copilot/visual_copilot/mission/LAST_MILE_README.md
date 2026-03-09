# ⚡ TARA Last-Mile Compound Protocol: Hierarchical Execution & Logical Verification

The **Last-Mile** is the highest-precision phase of the TARA agentic workflow. It transitions from high-level "strategy" to direct, verified browser interaction. This architecture enforces a strict **"System 2" Verification** protocol to eliminate hallucinations and infinite loops.

---

## 🚀 Core Objectives

1.  **Zero-Hallucination Extraction**: Enforces the **Entity Anchor Rule**—completions are blocked unless the target entity is physically verified in the DOM evidence.
2.  **Latency Pipelining**: Executes multiple UI actions (click + type + wait) in a single reasoning turn, reducing round-trip latency by up to 80%.
3.  **Loop Hardening**: Implements deductive guardrails to detect and break concept-neighbor drifts and redundant navigation cycles.
4.  **Visual Grounding**: Mandatory **Vision Bootstrap** provides a multimodal strategic brief before any physical interaction.

---

## 👥 The Hierarchical Subagent Team

The Last-Mile is no longer a single LLM call. It is managed by a specialized team of subagents defined in `agent.yaml`:

| Subagent | Mission | Key Tool / Rule |
| :--- | :--- | :--- |
| **ActionPipeliner** | Optimization | Bundles atomic actions (Click+Type) into efficient execution units. |
| **LogicCritic** | Verification | **Entity Anchor Rule**: Rejects "lazy" completions lacking factual evidence. |
| **BugHunter** | Stability | **None-Safety**: Enforces strict DOM validation and prevents `NameError` drift. |
| **SiteIndexer** | Context | Map-driven navigation using `site_map.json` instead of vector-exploration. |

---

## 🔄 The Protocol Lifecycle

### Phase 1: The Trigger
The mission transitions to Last-Mile via `_should_enter_last_mile.py` when:
- The final subgoal is an **extraction** or **question**.
- The strategy has navigated to a "Leaf Node" of the `site_map.json`.

### Phase 2: Vision Bootstrap
Before Step 1, the agent performs a **Visual Scan**.
- Uses **Groq Llama-4-Scout** to map the physical layout.
- Returns a **Vision Strategic Brief** with the best validated `target_id`.

### Phase 3: The Read → Verify → Act Loop
The agent performs up to 8 iterations of a strict behavioral cycle:
1.  **READ**: Analyze "Current Readable Page Content".
2.  **VERIFY**: Check if the goal is already answered by visible text.
3.  **ACT**: Execute ONE targeted action (or a pipeline of actions).

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

### 2. Semantic Repeat Guard
Detects when the agent is clicking "Concept-Neighbors" (e.g., clicking *API Docs* then *API Guide* then *Developer Home*) without progress. After 3 repeats, it forces a **Vision Escalation**.

### 3. Section Reclick Guard
Prevents the agent from re-clicking sidebar links for sections that are already active in the current DOM view.

---

## 📂 Key Architecture Files

- **`visual_copilot/mission/last_mile.py`**: Loop orchestrator, prompts, and Phase triggers.
- **`visual_copilot/mission/tool_executor.py`**: Hardware-level tool execution & LogicCritic gates.
- **`visual_copilot/mission/screenshot_broker.py`**: Real-time WebSocket screenshot management.
- **`visual_copilot/orchestration/stages/pre_decision_stage.py`**: Routing logic & PageIndex traversal.
- **`visual_copilot/text/tokenization.py`**: Semantic drift detection & term extraction.

---

> [!IMPORTANT]
> **Logic Integrity**: All code changes to the Last-Mile layer must be verified by both the **BugHunter** (for syntax/safety) and the **LogicCritic** (for semantic validity).

# ⚡ Last Mile Architecture: Multi-Action Pipelining & Entity Anchoring

The **Last Mile** is the critical phase where the TARA agent transitions from high-level strategy to direct browser interaction. This architecture is designed to solve the "Latency vs. Intelligence" trade-off common in LLM-driven browser agents.

---

## 🚀 Core Objectives

1.  **Latency Reduction**: Eliminate the 4-5s "latency tax" per atomic action by bundling multiple UI commands into a single turn.
2.  **Verification Accuracy**: Prevent "lazy exits" using a strict **Entity Anchor Rule**—the agent cannot finish until the target entity is physically present in the evidence.
3.  **Stateful Awareness**: Minimize "agent amnesia" by retaining deeper DOM history across reasoning cycles.

---

## 🛠 Architectural Components

### 1. Backend Reasoning Loop (`last_mile.py`)
The agent runs an internal loop (up to 10 iterations) using **Groq Compound Tools**. 
- **Queuing Mechanism**: Actions like `click_element`, `type_text`, and `scroll_page` are no longer "terminal." Instead of returning to the user immediately, they are appended to a `queued_actions` list.
- **Pipelining**: The loop continues until the agent calls a terminal tool (`complete_mission`, `clarify`) or explicitly requests a UI sync via `wait_for_ui`.
- **Payload**: The final response to the frontend is a **Bundled Action Array**: `[action_1, action_2, ..., terminal_action]`.

### 2. The Tool Executor (`tool_executor.py`)
The executor manages the transition between reasoning and execution.
- **Non-Terminal Flags**: Key physical tools return `is_terminal=False` to allow the reasoning loop to keep "thinking" and queuing.
- **Entity Anchor Guard**: Intercepts `complete_mission`. It extracts the `target_entity` (e.g., "Purple Socks") and verifies it exists in the current DOM or evidence refs. If missing, it forces the agent to keep searching.

### 3. Frontend Sequential Pipeline (`tara-ws.js` & `tara-executor.js`)
The frontend is now an **Execution Engine** rather than a simple command receiver.
- **Serial Execution**: When it receives an array, it iterates through each step with a **150ms micro-delay** to allow the browser's main thread to settle.
- **Phoenix Guard**: A navigation detector runs between every step. If a step triggers a full page load, the pipeline is immediately aborted to allow the `phoenix` protocol to resume the mission on the new page.
- **Atomic Completions**: Only ONE `execution_complete` signal is sent to the backend after the *entire* bundle is finished, reducing round-trips by up to 80%.

---

## 🔄 Logic Flow: The Multi-Action Turn

1.  **Reasoning**: LLM decides: "I will click the search bar, type 'socks', and wait for results."
2.  **Internal Queue**: 
    - `click_element(id=search)` → Added to `queued_actions` (Backend loop continues).
    - `type_text(text="socks")` → Added to `queued_actions` (Backend loop continues).
    - `wait_for_ui(seconds=2)`   → Terminal signal for current turn.
3.  **Transmission**: Bundle sent to Frontend: `[ {type: "click", ...}, {type: "type_text", ...}, {type: "wait", ...} ]`.
4.  **Frontend Execution**:
    - Cursor moves to search bar → Click.
    - (150ms delay).
    - Text typed.
    - (Wait 2000ms).
5.  **Sync**: Frontend captures a fresh DOM hash and screenshot, then sends **one** `execution_complete` back to the backend.

---

## 🛡 Verification Logic (The Anchor Rule)

To prevent the agent from saying "Done!" prematurely, the following validation is enforced:

```python
# tool_executor.py logic snippet
if tool_name == "complete_mission":
    target = goal_schema.target_entity  # "Purple Socks"
    if target.lower() not in evidence.lower():
        # REJECT: Force agent to stay in the loop
        return "ERROR: You cannot complete until you see the [Target Entity] in evidence."
```

---

## 📉 Latency Comparison

| Turn Type | Old Architecture (Waterfall) | New Architecture (Pipelined) |
| :--- | :--- | :--- |
| Single Click | ~4,500ms | ~4,500ms |
| Click + Type + Wait | ~13,500ms (3 turns) | **~5,200ms (1 turn)** |
| 5-Step Navigation | ~22,500ms | **~6,800ms** |

---

## 📂 Key Files

- `rag-daytona.v2/visual_copilot/mission/last_mile.py`: Reasoning logic & prompts.
- `rag-daytona.v2/visual_copilot/mission/tool_executor.py`: Tool behavior & validation.
- `rag-daytona.v2/visual_copilot/orchestration/stages/last_mile_stage.py`: Routing & bundling.
- `orchestra_daytona.v2/static/tara-ws.js`: Sequential action loop.
- `orchestra_daytona.v2/static/tara-executor.js`: Physical DOM interaction.

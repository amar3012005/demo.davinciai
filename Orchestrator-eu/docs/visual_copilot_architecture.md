# Visual Co-Pilot: Self-Aware Planning Architecture

## Overview
This document describes the autonomous planning system for TARA's Visual Co-Pilot mode, emphasizing the **Expressive Co-Browsing** personality and **Self-Aware Skepticism** layer that prevents hallucination-driven errors.

## Core Philosophy: "Narrated Action"
TARA is not a silent robot executor. She's an expressive co-pilot who:
- **Observes** the DOM
- **Narrates** what she sees and what she's about to do
- **Acts** while explaining her reasoning
- **Repeats** for each step

This creates a collaborative "sitting next to you" experience instead of master-apprentice command-taking.

## Architecture Layers

### 1. **OODA Loop (Observe-Orient-Decide-Act)**
The planner operates in a continuous loop:
- **Observe**: Receive DOM context from browser
- **Orient**: Query Qdrant for Map Hints (static, fetched once per mission)
- **Decide**: Call LLM planner to determine next action
- **Act**: Execute action and wait for DOM update

### 2. **Self-Aware Skepticism Layer** ⚡ NEW
Before executing any action, the planner performs a **confidence assessment**:

#### Map Hint Validation
The planner must validate Map Hints against the Goal before trusting them:
```
VALIDATE MAP HINT FIRST: Does the hint "{map_hints}" actually help achieve Goal "{goal}"?
- If the hint is IRRELEVANT, OUTDATED, or NONSENSICAL → IGNORE IT
- If the hint URL contains random IDs, image paths, or unrelated content → REJECT IT
- Only trust hints that make SEMANTIC SENSE for the goal
```

**Example of Rejection Logic:**
```
Goal: "Find prompt caching documentation"
Map Hint: "To find peace, go to /api/images/random123.jpg"
Decision: REJECT (image URL is irrelevant to documentation search)
```

#### Confidence Scoring
Each plan output includes a confidence level:
- **high**: Action clearly moves toward goal (e.g., clicking "Prompt Caching" link)
- **medium**: Action might help but not certain
- **low**: Unsure which element to click, or map hint seems wrong

**Low Confidence Triggers:**
- Unsure which element to click
- Map hint seems wrong but no alternative found
- Page structure is confusing
- Might be in a loop

#### Backend Handling
```python
confidence = plan.get("confidence", "high")

if confidence == "low":
    # Pause execution, ask user for guidance
    await send_voice(websocket, "I'm not sure where to find that. Could you guide me?")
    await end_mission(session, clarification_msg)
    return
```

### 3. **Recursion Guard**
Prevents infinite loops by detecting duplicate actions:
```python
if current_action_signature == session.last_action and action_type == 'click':
    logger.warning("🛑 RECURSION GUARD: Prevented duplicate click")
    # Force wait and retry with warning
```

### 4. **Stagnation Detection**
Monitors DOM hash changes to detect when actions have no effect:
```python
if current_hash == session.last_dom_hash:
    session.stagnation_count += 1
    if session.stagnation_count >= max_stagnation:
        await end_mission(session, "The screen isn't responding.")
```

### 5. **Dynamic Session-End Dialogue** ⚡ NEW
TARA no longer uses static goodbye phrases. When a session ends, she:
1. **Analyzes** the conversation history via RAG.
2. **Summarizes** key accomplishments or the last few actions briefly.
3. **Closes** with a proactive follow-up question (e.g., "Is there anything else I can assist you with before I go?").

This makes the transition out of the session feel human and naturally helpful.

**Backend Implementation:**
- `RAG Service`: New `/api/v1/generate_exit` endpoint generates context-aware speech.
- `Orchestrator`: Detects exit keywords (`ws_handler.py`), calls the dynamic exit generator, and streams the result via TTS.

## LLM Prompt Structure

### Input Context
```json
{
  "goal": "User's mission objective",
  "dom_context": "List of visible elements (max 300)",
  "step_number": "Current step in mission",
  "last_action": "Previous action taken",
  "map_hints": "Pre-fetched navigation hints from Qdrant",
  "step_context": "Dynamic element-specific context",
  "current_url": "Current page URL",
  "warning_message": "Any loop/stagnation warnings"
}
```

### Output Schema (with Narration)
```json
{
  "reasoning": "Internal thought process (not shown to user)",
  "confidence": "high|medium|low",
  "speech": "What to say to the user during this step",
  "action": {
    "type": "click|navigate|wait|none",
    "target_id": "element_id_or_text",
    "reasoning": "Why this action was chosen"
  }
}
```

**Key Addition - `speech` Field:**
- TARA now narrates EVERY step of the mission
- The speech is played via TTS while the action executes (co-browsing feel)
- Examples:
  - `"speech": "Clicking on the 'Prompt Caching' link now..."`
  - `"speech": "Let me open this menu to find the settings..."`
  - `"speech": "Here are the 3 villas I found!"` (goal achieved)
  - `"speech": "Just a moment while this loads..."` (wait action)

## Decision Flow

```
┌─────────────────────────────────────┐
│  Receive Plan from LLM              │
└────────────┬────────────────────────┘
             │
             ▼
┌─────────────────────────────────────┐
│  Extract Confidence Level           │
└────────────┬────────────────────────┘
             │
             ▼
        ┌────┴────┐
        │ Is Low? │
        └────┬────┘
             │
      ┌──────┴──────┐
      │             │
     Yes           No
      │             │
      ▼             ▼
┌──────────┐  ┌──────────────┐
│ Ask User │  │ Check Loops  │
│ for Help │  │ & Execute    │
└──────────┘  └──────────────┘
```

## Example Scenarios

### Scenario 1: Valid Map Hint
```
Goal: "Find rate limits documentation"
Map Hint: "To find rate limits, navigate to https://console.groq.com/docs/rate-limits"
Reasoning: "The map hint directly relates to the goal. The URL pattern matches documentation structure."
Confidence: high
Action: navigate to /docs/rate-limits
```

### Scenario 2: Invalid Map Hint (Rejected)
```
Goal: "Find prompt caching docs"
Map Hint: "To find prompt caching, go to /api/images/llama-123.jpg"
Reasoning: "The map hint points to an image URL which is irrelevant. I will ignore it. Looking at the screen, I see a 'Prompt Caching' link in the sidebar which fits better."
Confidence: high
Action: click on tara-fbz4rspfz (Prompt Caching link)
```

### Scenario 3: Low Confidence
```
Goal: "Find the billing page"
Map Hint: None
DOM Context: [Generic navigation items, no clear billing link]
Reasoning: "No map hint provided. The current page shows generic navigation but no clear path to billing settings. I'm unsure which element to try."
Confidence: low
Action: none (mission paused, user asked for guidance)
```

## Benefits of Self-Aware Skepticism

1. **Hallucination Prevention**: Stops the agent from blindly following irrelevant hints
2. **User Trust**: Agent admits when unsure instead of making random clicks
3. **Faster Recovery**: Low confidence triggers early abort instead of wasting 10 steps
4. **Better Debugging**: Confidence scores help identify weak prompts or bad indexing

## Configuration

### Planner Settings
- **Model**: `llama-3.3-70b-versatile` (Groq)
- **Temperature**: 0 (deterministic)
- **Response Format**: JSON object
- **Max Steps**: 10 per mission
- **Max Stagnation**: 3 identical DOM states

### Confidence Thresholds
- **low**: Pause execution, ask user
- **medium**: Proceed but log warning
- **high**: Execute normally

## Future Improvements

1. **Confidence Learning**: Track which confidence scores led to success/failure
2. **Hint Quality Scoring**: Flag low-quality map hints in Qdrant for retraining
3. **User Feedback Loop**: Allow user to correct bad decisions and update prompts
4. **Multi-Model Validation**: Use a second LLM to validate high-stakes actions

---

**Last Updated**: 2026-02-05  
**Author**: Daytona Engineering Team

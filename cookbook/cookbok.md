docker compose -f docker-compose.local-eu.yml up -d --build

The TARA Visual Copilot pipeline is a high-performance orchestration system designed to move from a user's utterance to a physical browser action with minimum latency and maximum grounding.

Here is the breakdown of the scripts involved in the overall pipeline, categorized by their layer of responsibility:

1. The Orchestration Layer (The "Brain")
This layer decides the overall flow of the mission and handles transitions between navigation and interaction.


visual_copilot/orchestration/plan_next_step_flow.py
: The Main Orchestrator. It manages the state machine for every request, calling the Pre-Decision gate, the Last-Mile agent, or falling back to the Router.

mission_brain.py
: The Persistence Engine. It tracks the mission's lifecycle, stores action history, and manages the sequence of subgoals (the "Strategy").

visual_copilot/orchestration/stages/pre_decision_stage.py
: Determines if the current goal is a "Mission" (needs navigation) or "Last Mile" (ready to extract). It enables the Fast-Track mode to bypass expensive LLM checks when a strategy is known.

visual_copilot/orchestration/stages/last_mile_stage.py
: The entry handler for the final completion phase. It manages retry attempts and handovers to the compound agent.
2. The Strategy & Intelligence Layer
This layer translates high-level goals into specific UI strategies.


visual_copilot/api/map_hints.py
: The Hive Mind Interface. It queries the vector database for known navigation strategies and "Hints" for specific domains.

hive_interface.py
: The low-level connector to the Hive (Qdrant), handling the storage and retrieval of past successful navigations.

visual_copilot/intent/mind_reader_service.py
: The Intent Classifier. When no strategy is found in the Hive, this uses an LLM to "read the user's mind" and plan the next navigation step based on the URL and visible links.
3. The Execution Layer (The "Hands")
This layer handles the actual interaction with the current page.


visual_copilot/mission/last_mile.py
: The Compound Last-Mile Agent. This is a mini-agent within TARA that runs a fast internal loop (Read → Act → Verify) to complete terminal goals like data extraction.

visual_copilot/mission/last_mile_tools.py
: Defines the specific tools (Click, Scroll, Read) that the Last-Mile agent is allowed to use.

visual_copilot/mission/tool_executor.py
: The runtime that actually invokes the internal tool calls for the compound agent.
visual_copilot/routing/action_guard.py: A safety layer that validates if a target ID truly exists and is interactable before TARA is allowed to click it.
4. The Data & Vision Layer
This layer provides the "Eyes" of the system, turning raw DOM elements into sorted graph data.


live_graph.py
: The DOM Parser. It scans the page, identifies interactive vs. static elements, and assigns unique, stable IDs (like t-abcdef) to every node.

tara_models.py
: Contains the core Data Schemas (MissionState, Subgoal, GraphNode) that ensure consistent communication across all scripts.
visual_copilot/routing/semantic_detective.py: Uses semantic similarity to find buttons or links when the exact ID is missing or the page structure has changed slightly.
5. Integration / Entry Points
ultimate_api.py / 

plan_next_step.py
: The FastAPI endpoints that the frontend TARA widget hits to start or continue a mission.
websocket_handler_ultimate.py: Manages the persistent real-time connection between the TARA service and the browser extension.
The Pipeline Flow (Simplified):
Request hits ultimate_api.
plan_next_step_flow calls map_hints to check the Hive for a strategy.
If a strategy exists → Fast-Track (Bypass Mind Reader).
If at the target page → Last-Mile Agent (

last_mile.py
) takes over to extract data.
Action Guard validates the ID.
Response is sent back to the browser to execute the click/scroll.
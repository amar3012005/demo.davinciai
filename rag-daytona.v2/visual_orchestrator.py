import asyncio
import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, AsyncGenerator
from urllib.parse import urlparse

from llm_providers.groq_provider import GroqProvider

# Qdrant imports for HiveMind checks
try:
    from qdrant_client.http import models
    from qdrant_client.http.models import Filter, FieldCondition, MatchText, MatchValue
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

logger = logging.getLogger(__name__)

VOICE_STREAM_PROMPT = """You are TARA, a visual co-browsing pilot. Give a brief welcome.

PAGE: {ui_context}
SITE MODE: {mission_mode}

RULES:
1. Identify the website from the page context (e.g., "Groq", "GitHub", "Airbnb").
2. One or two sentences maximum.
3. If MIND mode: mention you have a map of this site.
4. Do NOT list buttons, menus, or UI elements.

EXAMPLES:
  "Welcome to the Groq console. I'm TARA, your visual co-pilot. How can I help?"
  "I see we're on GitHub. I'm TARA — ready to navigate."
  "Welcome to Airbnb. I have a map of this site. What are you looking for?"

User Query: {query}
"""

ACTION_STREAM_PROMPT = """
<system_configuration>
You are a Visual Co-Pilot for the Daytona Dashboard.
Goal: analyze the DOM context and user query to decide on the NEXT visual action.

RULES:
1. **Output ONLY a valid json object** (required).
2. Choose from: click, scroll_to, type_text, wait.
3. If multiple actions are needed, just pick the FIRST one.
4. If no action is possible or needed, output {{"type": "none"}}.
5. AVOID LOOPS: Don't repeat the same action.

Targeting Rules (CRITICAL):
- If the element has a valid "id" (not null), use it as "target_id".
- If "id" is null, YOU MUST CALCULATE THE CENTER COORDINATES using the "rect" data.
  Center X = rect.x + (rect.width / 2)
  Center Y = rect.y + (rect.height / 2)
  Output: {{"type": "click_coords", "x": 123, "y": 456}}

Format (json):
{{
  "type": "click",
  "target_id": "element_id_or_exact_text",
  "text": "Start Building"
}}
{{
  "type": "scroll_to",
  "target_id": "id",
  "text": "Section Title"
}}
{{
  "type": "scroll",
  "target_id": "",
  "text": "scrolling down"
}}
{{
  "type": "type_text",
  "target_id": "id",
  "text": "hello"
}}
</system_configuration>

Respond in json format.

Visible Elements:
{dom_context}

User Query: {query}
History: {history}
"""

NEXT_STEP_PROMPT = """
You are TARA, a Strategic Visual Co-Pilot (Partner Level).
Your job: Navigate collaboratively with the user, clarifying ambiguities and protecting privacy.

DECISION PROTOCOL (HITL - Human In The Loop):

1. **AMBIGUITY CHECK (Critical)**:
   - If the user's request could apply to >1 elements (e.g., two "Book Now" buttons), DO NOT GUESS.
   - Output action type: "clarify".
   - Ask: "I see multiple options. Which one?"

2. **PRIVACY GUARDIAN**:
   - If the next step involves entering sensitive data (password, email, credit card), STOP.
   - Output action type: "user_input_required".
   - Say: "I'll wait here while you enter your details securely."

3. **MAP VALIDATION (HiveMind)**:
   - "MAP HINTS" are from a pre-indexed HiveMind database. They provide the *target destination*.
   - **CRITICAL**: Do NOT use the map hint's URL to "navigate" directly.
   - Instead, use the hint to identify *which* visible element (button/link) will take you closer to that destination.
   - Example: Hint says "Go to /settings". Action: Click the "Settings" icon.

4. **NAVIGATION RESTRICTION (STRICT)**:
   - **DIRECT NAVIGATION (changing URL) IS FORBIDDEN** unless you have failed to find a path for 3 consecutive turns.
   - The user wants you to *use the UI*, not teleport.
   - **ALWAYS** find a clickable element (button, link, tab) that moves towards the goal.
   - If a specific link isn't visible, scroll or look for a parent menu (e.g., "Menu", "More").

5. **PERSISTENCE & SCROLLING (CRITICAL)**:
   - **DO NOT GIVE UP**. If you cannot find the EXACT element (e.g. "Reasoning"):
     1. **SCROLL**: Content is often below the fold. Output {{"type": "scroll", "target_id": "", "text": "scrolling to find target"}}.
     2. **SEARCH**: Look for a "Search" icon or input.
     3. **MENU**: Look for a "Menu" or "Hamburger" icon.
   - Only use "none" if you have tried scrolling/searching at least 3 times.

6. **ACTION EXECUTION**:
   - **TARGETING**: Prefer elements with `interactive='true'`. If you see a label "Sign In" and a button "Sign In", click the **BUTTON**.
   - If clear path exists -> "click", "type_text".
   - If loading -> "wait".
   - "navigate" -> **LAST RESORT ONLY**.

NARRATION (SPEAK LIKE A HUMAN):
- **CONVERSATION AWARENESS**: Check CONVERSATION_HISTORY. If this is NOT the first interaction, DO NOT say "Hello" again.
- **CONTEXTUAL DESCRIPTIONS**: Never read exact button IDs or technical text. Use natural descriptions.
- **FIRST STEP ONLY**: If Step 1 AND no conversation history, give a brief greeting.
- Otherwise, just describe the action naturally without re-introducing yourself.

OUTPUT SCHEMA (STRICT JSON):
{{
  "reasoning": "1. AGGRESSIVELY analyze DOM for the User's Goal. 2. If vague, pick the MOST PROMINENT logical step (e.g. 'Get Started'). 3. If exact text missing, find SYNONYMS (e.g., 'Log In' -> 'Sign In'). 4. Persist.",
  "confidence": "high|medium|low",
  "speech": "What to say to the user (conversational)",
  "action": {{
    "type": "click|type_text|wait|none|clarify|user_input_required|navigate|scroll|scroll_to",
    "speech": "What to say to the user (conversational)",
    "target_id": "element_id_or_text (use empty string if not applicable)",
    "text": "text_to_type (use empty string if not applicable)",
    "url": "optional_url (use empty string if not applicable)"
  }}
}}

RULES:
- **Output ONLY valid json**.
- ALWAYS include "speech".
- AVOID LOOPS: Check ACTION_HISTORY.
- **TARGETING RULE**: Prefer INTERACTIVE elements (interactive='true') over text labels.
- **MISSION PERSISTENCE**: Do NOT use action type "none" unless validly done. SCROLL if blocked.
- **CONFIDENCE**: If you have a solid hypothesis (e.g. "it's probably down there"), use "medium". Use "low" ONLY if you are totally confused and asking a question. For "scroll" actions, use "medium".


Respond in json format.

GOAL: "{goal}"
MISSION MODE: {mission_mode}
STEP: {step_number} / 10

CONVERSATION_HISTORY (Recent context - use this to avoid repeating greetings):
{conversation_history}

LAST ACTION: {last_action}
ACTION_HISTORY (do NOT repeat these): {action_history}
CONTEXT DIFF (What changed?): {dom_diff}
WARNINGS: {warning_message}

KNOWN MAP HINTS: {map_hints}
ELEMENT CONTEXT: {step_context}
CURRENT URL: {current_url}

CURRENT SCREEN STATE (Visible Elements):
{dom_context}
"""


FAST_FILTER_PROMPT = """Filter the DOM elements below to keep ONLY those relevant to the GOAL.

GOAL: "{goal}"

RULES:
1. Keep ALL interactive elements (buttons, links, inputs) that relate to the goal.
2. Keep section headers that provide context for those elements.
3. Remove everything else (decorative text, icons, spacers).
4. speech: Under 12 words acknowledging the page.

DOM ELEMENTS:
{dom_context}

OUTPUT JSON:
{{
  "speech": "brief acknowledgment",
  "relevant_ids": ["id1", "id2", "id3"]
}}"""


FAST_SENSE_PROMPT = """You are TARA, a Visual Co-Pilot. Give an INSTANT spoken acknowledgment.

GOAL: "{goal}"
VISIBLE PAGE:
{dom_context}

RULES:
1. "speech": Under 12 words. Acknowledge the goal and state your PLAN (what you'll do next).
2. "relevant_ids": Up to 8 element IDs you'll likely interact with.
3. GREETING (hi, hello, hey) → respond warmly. Skip page details.
4. TASK/QUESTION → state what action you're about to take ("Let me check the docs section.").
5. AMBIGUOUS/CONVERSATIONAL ("what's wrong?", "what happened?", "why?") → acknowledge and describe what you see that might be relevant. NEVER parrot the user's words back.
6. NEVER repeat the user's exact words as your response. NEVER output the goal text as speech.

OUTPUT JSON:
{{
  "speech": "your response here",
  "relevant_ids": ["id1", "id2"]
}}

EXAMPLES:
  Goal "show me reasoning models" → "Let me find the reasoning models for you."
  Goal "go to docs" → "Heading to the docs section now."
  Goal "hey" → "Hello! How can I help you today?"
  Goal "what's wrong?" → "Let me take a look at what's happening here."
  Goal "what models are available" → "I can see several models listed. Let me check."
  Goal "find the pricing" → "Let me look for the pricing page."

Your speech should tell the user WHAT YOU'RE ABOUT TO DO, not describe what you see."""

# ── Prompt Chain Definitions (Phase B) ─────────────────────────────────────────

GOAL_DECOMPOSITION_PROMPT = """Break the user's goal into 1-3 steps. Use the VISIBLE ELEMENTS and optionally the SITE MAP to plan.

GOAL: "{goal}"

SITE MAP (navigation suggestions — may or may not be relevant):
{map_hints}

CURRENT PAGE ELEMENTS:
{page_context}

STEP 0 — CLASSIFY THE GOAL INTENT:
Before planning, determine what the user actually wants:
- PERSONAL DATA ("my expenses", "my usage", "my API keys", "my account", "my billing")
  → Navigate to the user's DASHBOARD, SETTINGS, ACCOUNT, or USAGE page — NOT documentation.
  → Look for links like "Dashboard", "Usage", "Billing", "Settings", "API Keys", "Account" in the visible elements.
- INFORMATION ("show me reasoning models", "what is X", "how do I", "docs")
  → Navigate to documentation, feature pages, or informational sections.
  → The SITE MAP is useful here.
- VISIBLE ON PAGE ("what's on this page", data already displayed)
  → Answer directly from visible elements. 1 step only.

PLANNING RULES:

1. **INTENT FIRST, MAP SECOND**: The site map is a SUGGESTION. If the map says "docs/spend-limits" but the user wants "my expenses", go to Dashboard/Usage instead — not docs. Only use the map when it actually matches the user's intent.

2. **NEVER navigate directly to a URL**. Always click through visible links/buttons on the page. Plan clicks on elements you can see in CURRENT PAGE ELEMENTS.

3. **CHECK IF ANSWER IS ALREADY VISIBLE**: If the page already shows data that answers the goal, plan ONLY 1 step: "Read the visible data and answer the user."

4. **USE VISIBLE ELEMENTS**: Every step must reference an element ID from CURRENT PAGE ELEMENTS. If you can't find a matching link, plan to click the most logical navigation link visible (Dashboard, Settings, etc.).

5. **CLICK NAVIGATION LINKS, NOT CONTENT BUTTONS**: Click sidebar links (a), nav links — NOT content cards or showcase buttons. A button labeled "GPT OSS 120B" in a model grid is NOT navigation. A link labeled "Docs" or "Dashboard" in the nav IS.

6. **success_signal for navigation steps**:
   - Defaults to "URL changed" which is usually sufficient.
   - Only specify a path segment if you are 100% sure (e.g. "URL contains /dashboard").
   - If unsure, just use "URL changed".

7. Keep it to 1-3 steps maximum.

OUTPUT FORMAT:
{{
  "steps": [
    {{
      "description": "Click the '[element text]' link (id: [element_id])",
      "type": "navigate|observe|interact|answer",
      "success_signal": "URL changed"
    }}
  ]
}}"""

                                                                             
OBSERVE_PROMPT = """You are the EYES of an autonomous web navigation agent. Your ONLY job is to look at the page and report what you see. You do NOT decide what to do — another module handles that.

TASK: Read the DOM snapshot below and extract a structured observation.

USER GOAL (for context only — do NOT act on it, just use it to identify relevant elements):
"{user_goal}"

CURRENT URL: {url}
PAGE CLASSIFICATION: {page_state_classification}

STEP-BY-STEP INSTRUCTIONS:

1. IDENTIFY THE PAGE:
   - Read the URL path. Extract the page name from it (e.g., /home -> "Home", /docs/models -> "Docs > Models", /dashboard/usage -> "Usage").
   - Read section headers (## lines in the DOM). The FIRST major header is usually the page title.
   - If a nav item is marked [active], that confirms which page you are on.

2. SCAN FOR GOAL-RELEVANT DATA:
   - If the user goal mentions "reasoning model" and you see a section header "REASONING" with model buttons listed under it — report those elements AND their section.
   - If the user goal mentions "usage" and you see cost/token data on screen — report the actual numbers you can read.
   - If data that answers the user's goal is VISIBLE RIGHT NOW, copy it into visible_data. This is critical — do NOT leave visible_data empty if relevant data is on screen.

3. MAP INTERACTIVE ELEMENTS:
   - List every [button], [a] (link), and [input] that could help achieve the goal.
   - Include the section header they fall under (e.g., "Under REASONING: button GPT OSS 120B").
   - Note which elements are marked [active], [expanded], or [NEW].

4. IDENTIFY OBSTACLES:
   - Modals, popups, loading spinners, error messages.
   - Required form fields blocking progress.

VISIBLE ELEMENTS:
{dom_compact}

OUTPUT FORMAT (respond with ONLY this JSON, no extra text):
{{
  "current_page": "Page name from URL and headers (e.g., 'Dashboard Home', 'Docs > Models', 'Usage')",
  "active_nav": "Which nav item is marked [active], or 'none'",
  "situation": "One sentence: I am on [page]. I can see [key visible content].",
  "visible_data": "If data answering the user's goal is on screen, write it here. Otherwise empty string.",
  "sections_found": ["List of section headers you can see (## lines)"],
  "relevant_elements": [
    {{"id": "element_id", "type": "button", "text": "element text", "section": "parent section header"}}
  ],
  "obstacles": []
}}"""

ORIENT_PROMPT = """You are the STRATEGIC BRAIN of an autonomous web navigation agent. You receive an observation of the current page and must decide the next strategic move.

MISSION GOAL: "{goal_raw}"
CURRENT SUB-GOAL: {current_subgoal_desc} (attempt #{current_subgoal_attempts})
SUB-GOAL STATUS: {subgoal_status_summary}

OBSERVATION FROM PERCEPTION MODULE:
  Page: {current_page}
  Active Nav: {active_nav}
  Situation: {observe_situation}
  Visible Data: {visible_data}
  Sections Found: {sections_found}
  Obstacles: {observe_obstacles}
  Relevant Elements: {relevant_elements_summary}

NAVIGATION HISTORY (what I have tried so far — learn from failures, do NOT repeat):
{monologue_history}

SITE MAP (CRITICAL — this is pre-indexed knowledge of the site's pages and navigation paths):
{map_hints}
CURRENT URL: {current_url}

MANDATORY THINKING PROCESS (Must be reflected in 'reasoning' field):

1. **Map Check**: Does the SITE MAP or URL provide a hint? (e.g. "navigate to /docs")
2. **Answer Check**: Is the answer visible in 'Visible Data'? If yes -> PRIORITY "answer_from_data".
3. **Obstacle Check**: Am I blocked? Am I looping (check history)?
4. **Strategy**: Why is this the SINGLE BEST move?

Q0. DOES THE SITE MAP TELL ME WHERE TO GO?
    -> If map_hints provides a URL path (e.g., "navigate to /docs/reasoning"), I should click nav links that lead toward that URL.
    -> Match visible nav elements (Docs, API Keys, Dashboard, etc.) to the map hint's URL path.
    -> Example: Map says "navigate to /docs/reasoning" and I see a "Docs" link -> Click "Docs" first, then find "Reasoning" on the docs page.
    -> Do NOT click model cards, showcase buttons, or content tiles. Click NAVIGATION LINKS (sidebar, top nav).

Q1. IS THE ANSWER ALREADY ON SCREEN?
    -> Check visible_data. If it contains information that answers the mission goal, set priority to "answer_from_data" and write the answer. Do NOT navigate away from data you already have.
    -> Example: Goal is "show me usage", visible_data says "Total Spend $3.63" -> ANSWER, don't click anything.
    -> Example: Goal is "best reasoning model", visible_data says "REASONING section: GPT OSS 120B, GPT OSS 20B" -> ANSWER with "GPT OSS 120B is the largest reasoning model available."

Q2. AM I ALREADY ON THE RIGHT PAGE?
    -> Compare current_page and active_nav against the sub-goal destination.
    -> If active_nav says "Dashboard" and sub-goal says "navigate to Dashboard" -> sub-goal is ALREADY DONE. Set priority to "advance_subgoal".
    -> If active_nav says "Usage" and goal mentions "usage" -> I'm already here. Look for data on screen.

Q3. DID MY LAST ACTION FAIL?
    -> Check monologue history. If the same action was tried 2+ times with no progress, I MUST try a completely different approach.
    -> Alternatives: scroll down, use search bar, try a different nav link, use sidebar instead of top nav.

Q4. IS THERE AN OBSTACLE BLOCKING ME?
    -> Modal, popup, cookie banner, loading screen -> priority "dismiss_obstacle".

Q5. WHAT IS THE SINGLE BEST NEXT ACTION?
    -> If I need to navigate: identify the exact link/button from relevant_elements.
    -> If I need to scroll: the target might be below the fold.
    -> If I need to read: extract the answer from visible_data.
    -> NEVER navigate to a page I'm already on. NEVER click an element I've already clicked.

CONFIDENCE CALIBRATION (this is important — do not always say "high"):
  "high"   = I can see the exact element I need to click, OR the answer is visible on screen
  "medium" = I have a reasonable plan but the element might not be exactly right, OR I need to scroll to find it
  "low"    = I am confused, lost, or my previous attempts have all failed — I should ask the user for help

OUTPUT FORMAT (respond with ONLY this JSON):
{{
  "priority": "continue_subgoal",
  "reasoning": "1. Map: [Check] 2. Answer: [Check] 3. Obstacle: [Check] 4. Strategy: [Decision]",
  "answer": "2-4 sentences MAX. Be specific and concise. Lead with the key fact, then add 1-2 supporting details. No fluff.",
  "active_subgoal_index": 0,
  "confidence": "high|medium|low",
  "speech_needed": false,
  "failed_approach": ""
}}

PRIORITY VALUES (choose exactly one):
  "answer_from_data"  — I have the answer visible on screen. Put it in the "answer" field (2-4 sentences MAX, brief and specific). Mission can complete.
  "continue_subgoal"  — I need to click/scroll/type to make progress. The right element is identified.
  "advance_subgoal"   — Current sub-goal is done (I'm on the right page / data is loaded). Move to next sub-goal.
  "dismiss_obstacle"  — A modal/popup/banner is blocking me. I need to close it first.
  "backtrack"         — My approach has failed 3+ times. I need a completely new strategy. Describe the new strategy in "reasoning".
  "goal_complete"     — The entire mission is done and I've already communicated the result.
  "clarify"           — The goal is ambiguous and I need user input to proceed.

ANTI-PATTERNS (if you do any of these, the mission will fail):
  - Setting priority to "continue_subgoal" when visible_data already answers the goal
  - Setting priority to "answer_from_data" when visible_data is empty
  - Clicking a nav item that is already [active] — this reloads the same page
  - Always setting confidence to "high" — calibrate honestly
  - Repeating the same action from monologue_history that already failed"""

DECIDE_PROMPT = """You are the HAND of an autonomous web navigation agent. The BRAIN has decided the strategy. Your job is to pick exactly ONE action from the visible elements.

STRATEGIC CONTEXT:
  Priority: {priority}
  Strategy (Concise): {reasoning}
  Sub-goal: {subgoal_desc}
  Success Signal: {success_signal}
  Relevant Elements (from observation): {relevant_elements}

SYSTEM WARNING: {warning}

RECENT ACTIONS (these ALREADY HAPPENED — repeating any of them is a CRITICAL FAILURE):
{recent_actions}

VISIBLE ELEMENTS ON SCREEN:
{dom_filtered}

ACTION SELECTION RULES:

RULE 0 — PRIORITY CHECK (CRITICAL):
  If the strategy Priority is "answer_from_data", do NOT analyze clicks or scrolls. Output action type "answer" immediately.
  The 'reasoning' from the BRAIN says the answer is visible. TRUST IT. Do not "double check" by scrolling.

RULE 1 — ANSWER MODE:
  If priority is "answer_from_data", output action type "answer" with the answer text.
  Do NOT click anything. The answer is already known.

RULE 2 — CLICK TARGETING:
  Only click elements that appear in VISIBLE ELEMENTS above. If an ID is not listed, it DOES NOT EXIST.
  Prefer elements that were flagged in "Relevant Elements" from the observation.
  Prefer: [button] > [a] (link) > [input]. Never click plain text (div, span, p) unless it's the only option.

RULE 3 — NO REPEATS:
  Check RECENT ACTIONS. If you see "click on t-xyz" in recent actions, do NOT output target_id "t-xyz".
  If all obvious targets are in recent actions, try "scroll" but respect RULE 9.

RULE 4 — BLOCKED ELEMENTS:
  If SYSTEM WARNING mentions "element X is blocked", choose a DIFFERENT element that achieves the same goal.
  Look for alternative paths: sidebar links, dropdown menus, search bar, breadcrumbs.

RULE 5 — NAVIGATION LINKS vs CONTENT BUTTONS:
  For navigation sub-goals, click SIDEBAR or TOP NAV links ([a] elements in nav sections), NOT content cards or model buttons.
  If you see a section ## NAVIGATION or ## ASIDE, prefer links in that section for navigation tasks.
  Example: To reach Docs, click the "Docs" link in the sidebar (nav), NOT a "Docs" card in the content area.
  A button labeled "GPT OSS 120B" in a model grid opens model details, it does NOT navigate to documentation.

RULE 6 — SCROLL ONLY WHEN CONTENT IS TRULY MISSING:
  If the goal is to FIND information, scan the VISIBLE ELEMENTS first. If you see the text "Llama 3B instant cost: $0.05", DO NOT SCROLL. Use action "answer".
  Only scroll if you have checked ALL visible elements and NONE contain the answer or navigation link.
  {{"action": {{"type": "scroll", "target_id": "", "text": "down", "url": ""}}, ...}}

RULE 7 — NEVER NAVIGATE DIRECTLY TO URLs:
  Always navigate by clicking through links on the page. Never output a raw "navigate" action with a URL.
  The user wants to see TARA surf the page through actions, not teleport.

RULE 8 — CONFIDENCE MUST MATCH REALITY:
  "high"   = The element text clearly matches the goal, and it hasn't been tried before.
  "low"    = I'm guessing because nothing obvious matches.

RULE 9 — SCROLL LIMIT & SEARCH (CRITICAL):
  If RECENT ACTIONS show 3+ consecutive scrolls, STOP SCROLLING. The target is likely not here.
  Instead, use a search bar (type_text) or click a high-level parent category in ## NAVIGATION to re-orient.
  Do NOT continue scrolling solely on "medium" or "low" confidence. Change strategy.

OUTPUT FORMAT (respond with ONLY this JSON — keep it short to avoid token overflow):
{{
  "action": {{
    "type": "click",
    "target_id": "the_element_id",
    "text": "",
    "url": ""
  }},
  "expected_outcome": "Short description of what should happen",
  "monologue": "1. Constraint Check: [Check Scroll Limit/Recent Actions] 2. Scan: [Found element X with ID Y] 3. Verify: [Matches Rule 5?] 4. Decision: [I chose X because...]",
  "confidence": "high"
}}

## MANDATORY THINKING PROCESS (Must be reflected in 'monologue'):
1. **Constraint Check**: Check RECENT ACTIONS. Have we scrolled 3 times? Is the priority 'answer_from_data'?
2. **Scan Elements**: Look at VISIBLE ELEMENTS. Do any match the sub-goal? Check IDs carefully.
3. **Verify Strategy**: Does the selected element match RULE 5 (Navigation vs Content)?
4. **Final Decision**: Explain why this action is the single best move.

VALID ACTION TYPES:
  "click"     — Click an interactive element. Requires target_id.
  "type_text" — Type into an input. Requires target_id and text.
  "scroll"    — Scroll the page. Set target_id="" and text="down" or "up".
  "scroll_to" — Scroll to a specific element. Requires target_id.
  "wait"      — Wait for page to load. No target needed.
  "navigate"  — Go to a URL directly. Requires url. LAST RESORT ONLY.
  "answer"    — Speak the answer to the user. Requires text with the answer.
  "clarify"   — Ask the user a question. Requires text with the question.
  "none"      — Do nothing. Only use after 3+ failed scroll attempts."""


SPEECH_PROMPT = """You are the VOICE of TARA, a friendly Visual Co-Pilot guiding a user through a website.

Generate ONE short, natural sentence to say out loud. This will be converted to speech via TTS.

WHAT JUST HAPPENED: {action_summary}
WHY: {reasoning}
USER'S GOAL: {goal}
IS THIS THE FIRST STEP? {is_first_step}

RULES:
1. For navigation/clicks: Maximum 15 words. Shorter is better.
2. For ANSWERS: Maximum 3-4 sentences. Lead with the key fact, then 1-2 supporting details. No filler.
3. Sound like a helpful human copilot sitting next to the user, not a robot.
4. If this is the first step, briefly acknowledge the goal: "Let's find the reasoning models for you."
5. If navigating, describe WHERE you're going: "Heading to the models page."
6. If answering, lead with the key fact: "GPT OSS 120B is the top reasoning model here. It has 120B parameters, making it the most capable for complex tasks."
7. If scrolling, say nothing (return empty string "").
8. If stuck/retrying, be honest: "That didn't work, trying another way."
9. NEVER say element IDs, CSS selectors, or technical DOM terms.
10. NEVER re-introduce yourself after the first step. No "Hello" or "I'm TARA" after step 0.
11. NEVER describe what buttons look like. Just say what you're DOING.

OUTPUT: A single sentence (or up to 4 for answers). No JSON. No quotes. Just the text. Or empty string if nothing to say."""




# ── v5: Single REASON Prompt (replaces OBSERVE+ORIENT+DECIDE) ────────────────

REASON_PROMPT = """You are TARA, an autonomous web navigation agent.

MISSION: "{goal}"
SUB-GOAL: {subgoal} (attempt #{attempts})

PAGE STATE:
{page_slice}

HISTORY (learn from these — NEVER repeat a failed action):
{compressed_history}

SELF-CORRECTIONS (from previous failures on this mission):
{reflexion_memory}

STAGNATION STATUS:
{stagnation_hint}

INSTRUCTIONS:
1. **STATE-GOAL-GAP ANALYSIS**:
   - STATE: Where am I? (e.g. "Pricing Page", "Docs > Models").
   - GAP: Does this page help the MISSION?
   - DECISION: If I am on a deep page (e.g. /docs/caching) but the mission is unrelated (e.g. "my usage"), I MUST navigate to Home, Dashboard, or Search first.

2. **CHECK VISIBLE DATA**:
   - Is the answer LITERALLY VISIBLE in the page state above?
   - If yes, use "answer". If no, navigate.

3. **EVALUATE SUB-GOAL RELEVANCE**:
   - If the sub-goal says "Click 'Prompt Caching'" but the mission is "Kilo Code", IGNORE the sub-goal. It is stale.
   - Instead, find a link for "Kilo" or "Docs" or "Search".

4. **CHOOSE ONE ACTION**:
   - Pick the element most likely to advance the MISSION.

OUTPUT (JSON only):
{{
  "observation": "State: [Page Name]. Gap: [Analysis].",
  "reasoning": "The goal is [X]. I am on [Y]. This is [relevant/irrelevant]. I see [Element Z]. Therefore I will [Action].",
  "action": {{
    "type": "click|type_text|scroll_to_view|answer|clarify|none",
    "target_id": "element_id",
    "text": ""
  }},
  "confidence": "high|medium|low",
  "speech": "Brief narration of the action. For answers, summarize the answer text."
}}

MANDATORY:
- Keys MUST be exactly: "observation", "reasoning", "action", "confidence", "speech".
- NO other keys like "obletter" or comments.
- ESCAPE ALL strings for valid JSON.

SPEECH RULES:
- Describe YOUR ACTION, not the page.
- "Heading to the dashboard." (Good)
- "I see the dashboard link." (Bad)
- FOR ANSWERS: Summarize the answer text briefly (2-3 sentences). DO NOT just say "Here is the answer."

ACTION RULES:
- "answer": Mission complete. Text must be visible.
- "scroll_to_view": Only for offscreen elements found in the Page Graph.
- "click": Must use valid ID from Page State.
- "none": Use only if truly stuck (explain in reasoning).

CRITICAL:
- If the current page is irrelevant to the mission, DITCH the sub-goal and click a high-level nav link (Home, Dashboard, Docs).
"""


# Interactive element types for smart DOM filtering
_INTERACTIVE_TYPES = frozenset({"button", "a", "input", "select", "textarea", "label", "option"})
_LANDMARK_TYPES = frozenset({"h1", "h2", "h3", "title", "header", "nav", "main", "form"})

# ── Speech Templates (Zero-LLM Fast Path for Routine Actions) ────────────────
# None = skip speech entirely; str = use template; missing key = fall through to LLM
# None = skip speech entirely; str = use template; missing key = fall through to LLM
SPEECH_TEMPLATES = {
    "scroll": "Scrolling down.",                                       # Minimal
    "scroll_to": "Scrolling to find {text}.",                          # Minimal
    "wait": "Waiting a moment.",                                       # Minimal
    "highlight": None,                                                 # Silent
    "spotlight": None,                                                 # Silent
    "clear": None,                                                     # Silent
    "click_routine": "Clicking {text}.",                               # Minimal
    "click_after_silence": "Clicking {text}.",                         # Minimal
    "type_text": "Typing {text}.",                                     # Minimal
    "obstacle_modal": "Closing popup.",                                # Minimal
    "obstacle_loading": "Loading.",                                    # Minimal
    "failure_retry": "Retrying.",                                      # Minimal
    "answer_data": None,                                   # Always LLM-generated (the answer IS the speech)
    "goal_complete": None,                                 # Always LLM-generated
    "clarify": None,                                       # Always LLM-generated
}

# How many silent (no-speech) consecutive actions before forcing a spoken update
_SPEECH_SILENCE_THRESHOLD = 3


# ── Page State Classification (heuristic, no LLM) ────────────────────────────

def classify_page_state(dom_elements: list, url: str) -> "PageState":
    """
    Classify the current page type from DOM elements using heuristics.
    Runs in pure Python — no LLM call.  Returns a PageState dataclass.
    """
    from session_manager import PageState
    from collections import Counter

    types = Counter(el.get("type", "div").lower() for el in dom_elements)
    texts = [el.get("text", "").lower() for el in dom_elements]
    url_lower = (url or "").lower()

    has_modal = any(
        "modal" in (el.get("id", "") + " " + el.get("text", "")).lower()
        or el.get("type", "").lower() in ("dialog",)
        or "dialog" in el.get("text", "").lower()
        for el in dom_elements
    )

    has_search = any(
        el.get("type", "").lower() in ("input", "search")
        and el.get("interactive", False)
        and any(
            kw in (el.get("text", "") + " " + el.get("id", "")).lower()
            for kw in ("search", "find", "query", "location", "destination", "suche")
        )
        for el in dom_elements
    )

    input_count = types.get("input", 0) + types.get("textarea", 0) + types.get("select", 0)
    link_count = types.get("a", 0)
    button_count = types.get("button", 0)
    total = len(dom_elements)

    # Classification logic (priority order)
    if has_modal:
        page_type = "modal"
    elif "/login" in url_lower or "/signin" in url_lower or (
        input_count >= 3 and any("password" in t for t in texts)
    ):
        page_type = "auth"
    elif any(kw in t for t in texts[:10] for kw in ("error", "404", "not found", "page not found")):
        page_type = "error"
    elif any("loading" in t or "spinner" in t for t in texts[:5]):
        page_type = "loading"
    elif input_count >= 4:
        page_type = "form"
    elif link_count > 20 and has_search:
        page_type = "results"
    elif has_search and link_count < 10:
        page_type = "search"
    elif link_count > 15:
        page_type = "landing"
    else:
        page_type = "detail"

    # Extract navigation elements
    primary_nav_ids = [
        el.get("id", "")
        for el in dom_elements
        if el.get("type", "").lower() in ("nav", "a")
        and el.get("interactive", False)
        and el.get("id")
    ][:10]

    # Extract form fields
    form_fields = [
        el.get("id", "") or el.get("text", "")[:20]
        for el in dom_elements
        if el.get("type", "").lower() in ("input", "textarea", "select")
    ]

    # Content density
    if total < 20:
        content_density = "sparse"
    elif total > 150:
        content_density = "dense"
    else:
        content_density = "normal"

    return PageState(
        page_type=page_type,
        has_modal=has_modal,
        has_search=has_search,
        primary_nav_ids=primary_nav_ids,
        form_fields=form_fields,
        scroll_position="top",  # Widget can provide this later
        content_density=content_density,
    )


# ── Validation Result ─────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    success: bool
    reason: str
    method: str = "heuristic"     # "heuristic" | "llm"
    side_effect: str = ""         # "modal_appeared" | "" etc.
    signal: str = ""              # v5: url_changed|nav_changed|content_loaded|modal_changed|no_effect


# ── Stagnation Actions ────────────────────────────────────────────────────────

class StagnationAction:
    CONTINUE = "continue"
    RETRY_DIFFERENT_ELEMENT = "retry_different_element"
    SCROLL_AND_RETRY = "scroll_and_retry"
    CLARIFY_WITH_USER = "clarify_with_user"
    NAVIGATE_DIRECT = "navigate_direct"
    SKIP_SUBGOAL = "skip_subgoal"


class VisualOrchestrator:
    """Manages dual-stream generation for Visual Co-Pilot"""

    def __init__(self, groq_provider: GroqProvider, qdrant_client=None, embeddings=None, redis_client=None):
        self.groq = groq_provider
        self.qdrant = qdrant_client
        self.embeddings = embeddings
        self.redis = redis_client
        self.session_mgr = None
        self.graph_mgr = None  # v5: PageGraphManager
        # Load model IDs from environment variables (specified in docker-compose.yml)
        self.llm_model = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
        self.analytics_model = os.getenv("ANALYTICS_MODEL", "qwen/qwen3-32b")
        self.use_hivemind = str(os.getenv("HIVEMIND_IN_VISUAL_COPILOT", "true")).lower() == "true"

        # DOM serialization cache: {session_id: (dom_hash, compact_dom_str)}
        self._dom_cache: Dict[str, tuple] = {}

        # Initialise session manager and page graph manager if Redis available
        if redis_client:
            from session_manager import SessionManager
            from redis_page_graph import PageGraphManager
            self.session_mgr = SessionManager(redis_client)
            self.graph_mgr = PageGraphManager(redis_client)
            logger.info("Session manager + PageGraphManager initialised with Redis")

        logger.info(f"VisualOrchestrator initialized | LLM={self.llm_model} | ANALYTICS={self.analytics_model} | HiveMind={self.use_hivemind} | Sessions={'Redis' if self.session_mgr else 'None'}")

    # ── Heuristic Action Validation (A5) ────────────────────────────────────

    async def validate_action_outcome(
        self,
        action_type: str,
        pre_dom_hash: int,
        post_outcome: dict,
    ) -> ValidationResult:
        """
        v5 Deterministic validation: URL → nav state → element count → modal → no effect.
        No auto-advance fallback. Actions either succeed with a clear signal or fail.
        """
        # ── Check 1: URL changed (strongest signal for navigation) ────────
        if post_outcome.get("url_changed"):
            return ValidationResult(
                success=True,
                reason=f"URL changed",
                signal="url_changed",
                method="heuristic",
            )

        # ── Check 2: Active nav state changed ────────────────────────────
        prev_active = post_outcome.get("prev_active_page", "")
        curr_active = post_outcome.get("curr_active_page", "")
        if curr_active and curr_active != prev_active:
            return ValidationResult(
                success=True,
                reason=f"Active page changed: {prev_active} → {curr_active}",
                signal="nav_changed",
                method="heuristic",
            )

        # ── Check 3: New content appeared (more than 5 new elements) ─────
        new_count = post_outcome.get("new_elements_count", 0)
        if new_count > 5:
            return ValidationResult(
                success=True,
                reason=f"{new_count} new elements appeared",
                signal="content_loaded",
                method="heuristic",
            )

        # ── Check 4: Modal/dialog appeared or disappeared ────────────────
        prev_modal = post_outcome.get("prev_has_modal", False)
        curr_modal = post_outcome.get("has_modal", False)
        if prev_modal != curr_modal:
            return ValidationResult(
                success=True,
                reason="Modal state changed",
                signal="modal_changed",
                side_effect="modal_appeared" if curr_modal else "modal_dismissed",
                method="heuristic",
            )

        # ── Check 5: Action-type specific pass-throughs ──────────────────
        if action_type in ("scroll", "scroll_to"):
            return ValidationResult(success=True, reason="Viewport shifted", signal="scroll", method="heuristic")
        if action_type == "type_text":
            return ValidationResult(success=True, reason="Input action completed", signal="input", method="heuristic")
        if action_type == "wait":
            return ValidationResult(success=True, reason="Wait completed", signal="wait", method="heuristic")
        if action_type in ("highlight", "spotlight", "clear"):
            return ValidationResult(success=True, reason=f"{action_type} completed", signal=action_type, method="heuristic")
        if action_type == "answer":
            return ValidationResult(success=True, reason="Answer delivered", signal="answer", method="heuristic")

        # ── Check 6: DOM changed at all? ─────────────────────────────────
        if not post_outcome.get("dom_changed"):
            return ValidationResult(
                success=False,
                reason=f"Click had no observable effect. URL, nav state, and content unchanged.",
                signal="no_effect",
                method="heuristic",
            )

        # Small DOM change (1-5 new elements) — inconclusive but pass
        if new_count > 0:
            return ValidationResult(
                success=True,
                reason=f"{new_count} new elements (minor change)",
                signal="minor_change",
                method="heuristic",
            )

        # ── FAILURE: Nothing meaningful changed ──────────────────────────
        return ValidationResult(
            success=False,
            reason=f"Click had no observable effect. URL, nav state, and content unchanged.",
            signal="no_effect",
            method="heuristic",
        )

    def handle_stagnation(self, session, validation: ValidationResult) -> str:
        """
        Stagnation escalation protocol.
        Returns a StagnationAction string indicating what to do next.
        """
        if validation.success:
            session.stagnation_count = 0
            session.consecutive_failures = 0
            return StagnationAction.CONTINUE

        session.consecutive_failures += 1

        if session.consecutive_failures == 1:
            return StagnationAction.RETRY_DIFFERENT_ELEMENT
        elif session.consecutive_failures == 2:
            return StagnationAction.SCROLL_AND_RETRY
        elif session.consecutive_failures == 3:
            return StagnationAction.CLARIFY_WITH_USER
        else:
            if session.hivemind_mode == "mapped":
                return StagnationAction.NAVIGATE_DIRECT
            else:
                return StagnationAction.SKIP_SUBGOAL

    def _smart_dom_filter(self, dom_context: list) -> list:
        """
        Filters raw DOM to keep only interactive elements and key structure.
        Falls back to original DOM if filtering is too aggressive (< 5 elements).
        Also strips SVG noise elements as a backend safety net.
        """
        if not dom_context:
            return []
        
        # SVG noise types to strip (backend safety net in case widget didn't filter)
        _SVG_NOISE = frozenset({
            "svg", "path", "rect", "circle", "line", "polyline", "polygon",
            "ellipse", "use", "defs", "clippath", "g", "mask", "symbol",
            "lineargradient", "radialgradient", "stop", "pattern", "marker"
        })
        
        filtered = []
        for el in dom_context:
            el_type = el.get("type", "div").lower()
            
            # Skip SVG internals — zero interaction value
            if el_type in _SVG_NOISE:
                continue
            
            is_interactive = el.get("interactive", False) or el_type in _INTERACTIVE_TYPES
            
            # Keep if interactive
            if is_interactive:
                filtered.append(el)
                continue
                
            # Keep if it has meaningful text and is a heading, data label, paragraph, or data container
            text = el.get("text", "").strip()
            if text and (el_type in _LANDMARK_TYPES or el_type in ("td", "th", "li", "dd", "dt", "p", "span", "label") or len(text) > 3):
                filtered.append(el)
        
        # GUARD: If filter is too aggressive, return original (minus SVG noise)
        if len(filtered) < 5 and len(dom_context) >= 5:
            fallback = [el for el in dom_context if el.get("type", "div").lower() not in _SVG_NOISE]
            logger.warning(f"Smart DOM filter too aggressive: {len(dom_context)} → {len(filtered)}. Using fallback ({len(fallback)}).")
            return fallback[:300]
        
        return filtered

    def _get_compact_dom(self, dom_context: List[Dict[str, Any]], limit: int = 200, token_budget: int = 4000) -> str:
        """
        Hierarchical DOM serialization with section grouping.
        Groups elements under their nearest section header (h1-h6, header).
        Adds state markers: [active], [selected], [expanded], [NEW].
        
        Output format:
        ## REASONING
          [button] GPT OSS 120B (id: t-ozm7tn) ← interactive
          [button] GPT OSS 20B (id: t-pw8fjf) ← interactive
        ## NAV
          [a] Dashboard (id: t-1ild671) [active]
          [a] Playground (id: t-cdi3dr)
        """
        CHAR_PER_TOKEN = 4
        char_budget = token_budget * CHAR_PER_TOKEN

        # Pass 1: Build sections by grouping elements under headers
        sections = []  # list of (header_text, [elements])
        current_section = ("PAGE", [])  # Default section for elements before any header
        
        HEADER_TYPES = {"h1", "h2", "h3", "h4", "h5", "h6", "header", "nav", "aside", "footer"}
        
        for el in dom_context:
            el_type = el.get("type", "div").lower()
            text = el.get("text", "").strip()
            
            if el_type in HEADER_TYPES:
                # Start a new section
                if current_section[1]:  # Save previous if non-empty
                    sections.append(current_section)
                
                # Use text if available, otherwise use tag name
                section_name = text[:40].upper() if text else el_type.upper()
                if "NAV" in section_name or el_type == "nav":
                    section_name = f"NAVIGATION ({section_name})"
                
                current_section = (section_name, [])
            else:
                current_section[1].append(el)
        
        # Don't forget the last section
        if current_section[1]:
            sections.append(current_section)

        # Pass 2: Serialize with hierarchy
        lines = []
        char_count = 0
        elem_count = 0

        for section_name, elements in sections:
            if elem_count >= limit:
                break
                
            # Filter section to interactive + text elements only
            section_elements = []
            for el in elements:
                el_type = el.get("type", "div")
                is_interactive = el.get("interactive", False) or el_type in _INTERACTIVE_TYPES
                has_text = bool(el.get("text", "").strip())
                el_id = el.get("id", "")

                if is_interactive or (has_text and el_type in _LANDMARK_TYPES):
                    section_elements.append(el)
                elif el_type == "nav" and has_text:
                    section_elements.append(el)
                elif has_text and el_type in ("td", "th", "li", "dd", "dt", "p", "span", "label"):
                    # Include data-bearing text elements so the LLM can see visible content
                    section_elements.append(el)
                elif has_text and el_type == "div" and len(el.get("text", "").strip()) > 3:
                    # Include divs with meaningful text (e.g. 'STARTUPS', 'PRICING' wrappers)
                    section_elements.append(el)
            
            if not section_elements:
                continue
            
            # Section header
            header_line = f"\n## {section_name}"
            header_len = len(header_line) + 1
            if char_count + header_len > char_budget:
                break
            lines.append(header_line)
            char_count += header_len

            for el in section_elements:
                if elem_count >= limit:
                    break

                el_type = el.get("type", "div")
                el_id = el.get("id", "")
                text = el.get("text", "").strip()[:80]
                is_interactive = el.get("interactive", False) or el_type in _INTERACTIVE_TYPES
                is_new = el.get("isNew", False) or el.get("is_new", False)
                
                # State detection
                state_markers = []
                el_state = el.get("state", "")
                if el_state == "active" or el.get("ariaSelected") == "true" or el.get("ariaCurrent"):
                    state_markers.append("[active]")
                if el.get("ariaExpanded") == "true":
                    state_markers.append("[expanded]")
                if is_new:
                    state_markers.append("[NEW]")
                
                state_str = " ".join(state_markers)
                
                # Build entry
                if is_interactive:
                    entry = f"  [{el_type}] {text} (id: {el_id}){' ' + state_str if state_str else ''}"
                else:
                    entry = f"  ({el_type}) {text}"
                
                entry_len = len(entry) + 1
                if char_count + entry_len > char_budget:
                    break

                lines.append(entry)
                char_count += entry_len
                elem_count += 1

        return "\n".join(lines)

    def _extract_site_skeleton(self, dom_context: List[Dict[str, Any]], url: str) -> 'SiteSkeleton':
        """(C2) Heuristic extraction of site navigation structure."""
        from session_manager import SiteSkeleton, SiteNode
        from urllib.parse import urlparse
        
        domain = urlparse(url).netloc
        nav_items = []
        headings = []
        search_elems = []
        
        for el in dom_context:
            el_type = el.get("type", "").lower()
            text = el.get("text", "").strip()
            
            if el_type == "nav" or (el_type == "a" and len(text) < 20):
                nav_items.append({"text": text, "id": el.get("id")})
            elif el_type in ["h1", "h2", "h3"]:
                headings.append({"text": text, "type": el_type})
            elif el_type in ["input", "search"] or "search" in (el.get("id") or ""):
                search_elems.append({"id": el.get("id"), "type": el_type})
                
        return SiteSkeleton(
            domain=domain,
            nodes={url: SiteNode(url=url, page_type="landing", key_elements=[h["text"] for h in headings[:3]])},
            primary_nav=nav_items[:10],
            search_elements=search_elems,
            main_headings=headings[:5],
            page_count=1,
            discovered_urls=[url]
        )

    async def _explorer_first_contact(self, session, dom_context: list, url: str):
        """(C2) Protocol for first time visiting a domain in explorer mode."""
        logger.info(f"🌍 Explorer Mode: First contact with {url}")
        
        # 1. Extract Skeleton
        skeleton = self._extract_site_skeleton(dom_context, url)
        session.site_skeleton = skeleton
        session.domain = skeleton.domain
        session.explorer_config = {"strategy": "breadth_first"} # Stub for future config
        
        # 2. Add to monologue
        from session_manager import MonologueEntry
        session.internal_monologue.append(MonologueEntry(
            step=0,
            thought=f"Landed on {session.domain}. Validating page structure.",
            confidence="high",
            page_type="landing",
            timestamp=time.time()
        ))

    async def _run_fast_sense(self, goal: str, dom_context: list) -> dict:
        """
        (C5) Fast Sense: Rapidly analyze DOM for immediate TTS feedback.
        Returns speech string and list of relevant element IDs.
        """
        # Give fast_sense enough context: headers + interactive elements (min 15)
        dom_str = self._get_compact_dom(dom_context, limit=80)
        
        prompt = FAST_SENSE_PROMPT.format(
            goal=goal,
            dom_context=dom_str
        )
        
        try:
            response = await self.groq.generate(
                prompt,
                model=self.llm_model, # Use fast model (Llama-3-8b)
                temperature=0.1,
                max_tokens=300,
                response_format={"type": "json_object"}
            )
            data = json.loads(response)
            return {
                "speech": data.get("speech", "I'm looking at the page now."),
                "relevant_ids": data.get("relevant_ids", [])
            }
        except Exception as e:
            logger.error(f"Fast Sense failed: {e}")
            return {"speech": "", "relevant_ids": []}

    # ── v5: Single REASON call (replaces OBSERVE + ORIENT + DECIDE) ────────

    async def _reason(self, session, page_slice: str, goal: str, stagnation_action: str = "continue") -> dict:
        """
        Single-call reasoning: observe + orient + decide in one shot.
        Uses query-focused slice instead of raw DOM.
        """
        t0 = time.time()

        # Current sub-goal
        current_subgoal = None
        if session.goal_plan and session.goal_plan.subgoals and 0 <= session.current_subgoal_index < len(session.goal_plan.subgoals):
            current_subgoal = session.goal_plan.subgoals[session.current_subgoal_index]

        subgoal_desc = current_subgoal.description if current_subgoal else goal
        subgoal_attempts = current_subgoal.attempts if current_subgoal else 0

        # Compressed history (last 5 actions + outcomes)
        history = self._compress_history(session.action_ledger[-5:])

        # Reflexion memory (self-corrections from failures)
        reflexion = ""
        if session.reflexion_memory:
            reflexion = session.reflexion_memory.format_for_prompt()
        if not reflexion:
            reflexion = "(No prior failures)"

        # Stagnation hint — tells the LLM what recovery strategy to use
        _STAGNATION_HINTS = {
            "continue": "No stagnation. Proceed normally.",
            "retry_different_element": "WARNING: Last action FAILED. You MUST try a DIFFERENT element than your last attempt.",
            "scroll_and_retry": "WARNING: 2 consecutive failures. Try scrolling to reveal new elements, then pick a different target.",
            "clarify_with_user": "CRITICAL: 3+ consecutive failures. Ask the user for clarification using action type 'clarify'.",
            "navigate_direct": "CRITICAL: Stuck in a loop. Use a navigation link to go to a completely different page.",
            "skip_subgoal": "CRITICAL: This sub-goal appears unreachable. Use action type 'none' and explain why.",
        }
        stagnation_hint = _STAGNATION_HINTS.get(stagnation_action, "No stagnation. Proceed normally.")

        prompt = REASON_PROMPT.format(
            goal=session.goal_raw or goal,
            subgoal=subgoal_desc,
            attempts=subgoal_attempts,
            page_slice=page_slice,
            compressed_history=history,
            reflexion_memory=reflexion,
            stagnation_hint=stagnation_hint,
        )

        try:
            result = await self.groq.generate_with_reasoning(
                prompt,
                model="openai/gpt-oss-20b",
                reasoning_effort="high",
                max_completion_tokens=4096,
                response_format={"type": "json_object"}
            )
            logger.info(f"💭 REASON CoT: {result['reasoning'][:300]}..." if result.get('reasoning') else "REASON: no CoT")
            content = result['content'].strip()
            cot = result.get('reasoning', '')

            # ── Parse JSON with multi-stage recovery ───────────────────
            data = self._parse_reason_json(content, cot)
            data["_ms"] = int((time.time() - t0) * 1000)
            data["_cot"] = cot[:500]
            return data
        except Exception as e:
            logger.error(f"REASON phase failed: {e}")
            return {
                "observation": "Error observing page",
                "reasoning": f"Reason failed: {e}",
                "action": {"type": "wait", "target_id": "", "text": ""},
                "confidence": "low",
                "speech": "",
                "_ms": 0,
            }

    def _parse_reason_json(self, content: str, cot: str) -> dict:
        """
        Multi-stage JSON recovery for LLM output.
        Stage 1: Direct parse
        Stage 2: Fix trailing commas, single quotes
        Stage 3: Extract JSON from content or CoT via regex
        Stage 4: Fallback wait action
        """
        import re
        fallback = {
            "observation": "Parse error",
            "reasoning": "JSON recovery failed",
            "action": {"type": "wait", "target_id": "", "text": ""},
            "confidence": "low",
            "speech": "",
        }

        sources = [content, cot]
        for i, raw in enumerate(sources):
            if not raw:
                continue
            source_name = "content" if i == 0 else "CoT"

            # Stage 1: Direct parse
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass

            # Stage 2: Fix common JSON issues (trailing commas, single quotes)
            cleaned = raw
            # Remove trailing commas before } or ]
            cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
            # Replace single-quoted keys/values with double-quoted
            cleaned = re.sub(r"(?<=[{,])\s*'([^']+)'\s*:", r' "\1":', cleaned)
            try:
                data = json.loads(cleaned)
                logger.info(f"🔧 JSON recovered via cleanup from {source_name}")
                return data
            except json.JSONDecodeError:
                pass

            # Stage 3: Extract first JSON object via regex
            m = re.search(r'\{[\s\S]*\}', raw)
            if m:
                extracted = m.group(0)
                # Apply same comma cleanup to extracted
                extracted = re.sub(r',\s*([}\]])', r'\1', extracted)
                try:
                    data = json.loads(extracted)
                    logger.info(f"🔧 JSON recovered via regex extraction from {source_name}")
                    return data
                except json.JSONDecodeError:
                    pass

        logger.error(f"🛑 JSON recovery failed across all stages. Content: {content[:200]}")
        return fallback

    def _compress_history(self, action_ledger: list) -> str:
        """Compress last N action records into a compact string for the REASON prompt."""
        if not action_ledger:
            return "(First step — no history)"
        lines = []
        for record in action_ledger:
            outcome = record.actual_outcome or "pending"
            target = record.target_text or record.target_id or ""
            lines.append(f"  Step {record.step}: {record.action_type} on '{target}' → {outcome}")
        return "\n".join(lines)

    async def _decompose_goal(self, goal: str, dom_context: list, map_hints: str = "") -> 'GoalPlan':
        """(C3) Decompose raw goal into sub-goals using LLM + HiveMind map."""
        from session_manager import GoalPlan, SubGoal

        dom_str = self._get_compact_dom(dom_context, limit=100)

        prompt = GOAL_DECOMPOSITION_PROMPT.format(
            goal=goal,
            map_hints=map_hints or "(No site map available — plan based on visible elements only)",
            page_context=dom_str
        )
        
        try:
            result = await self.groq.generate_with_reasoning(
                prompt,
                model="openai/gpt-oss-20b",
                reasoning_effort="medium",
                max_tokens=1024,
                response_format={"type": "json_object"}
            )
            logger.info(f"\ud83d\udcad DECOMPOSE CoT: {result['reasoning'][:300]}..." if result['reasoning'] else "DECOMPOSE: no CoT")
            content = result['content'].strip()
            if not content:
                import re
                m = re.search(r'\{[\s\S]*\}', result.get('reasoning', ''))
                content = m.group(0) if m else '{"steps": [{"description": "' + goal + '", "type": "general", "success_signal": "task done"}]}'
                logger.warning(f"DECOMPOSE: content was empty, extracted from reasoning")
            data = json.loads(content)
            
            subgoals = []
            if isinstance(data, list):
                # If raw list returned
                raw_list = data
            elif isinstance(data, dict) and "steps" in data:
                 raw_list = data["steps"]
            else:
                 # Fallback/Error in format
                 raw_list = [{"description": goal, "type": "general", "success_signal": "task done"}]

            for step in raw_list:
                subgoals.append(SubGoal(
                    description=step.get("description", ""),
                    type=step.get("type", "action"),
                    success_signal=step.get("success_signal", ""),
                    status="pending"
                ))
            
            # Activate first goal
            if subgoals:
                subgoals[0].status = "active"
                
            return GoalPlan(subgoals=subgoals, original_utterance=goal)
            
        except Exception as e:
            logger.error(f"Goal Decomposition failed: {e}")
            # Fallback to single goal
            return GoalPlan(
                subgoals=[SubGoal(description=goal, type="general", status="active", success_signal="unknown")],
                original_utterance=goal
            )

    # ── Helper: Format relevant elements for prompt injection ────────────────

    @staticmethod
    def _format_relevant_elements(elements: list) -> str:
        """Format relevant_elements from OBSERVE output for ORIENT/DECIDE prompts."""
        if not elements:
            return "(none identified)"
        lines = []
        for el in elements[:8]:
            if isinstance(el, dict):
                section = el.get("section", "")
                lines.append(f"  [{el.get('type', '?')}] {el.get('text', '?')} (id: {el.get('id', '?')}) — in section: {section}")
            else:
                lines.append(f"  {el}")
        return "\n".join(lines)

    # ── Main Pipeline (Replaces plan_next_step) ──────────────────────────────

    # ── Phase B: Prompt Chain Methods ────────────────────────────────────────

    async def _observe(self, dom_filtered: list, page_state, url: str, user_goal: str = "", cached_dom_str: str = None) -> dict:
        """Phase 1: OBSERVE - What do I see? (Fast perception, no reasoning)"""
        t0 = time.time()

        dom_compact = cached_dom_str if cached_dom_str else self._get_compact_dom(dom_filtered, limit=100)

        # Convert PageState to string representation
        page_state_str = f"Type: {page_state.page_type}, Modal: {page_state.has_modal}, Density: {page_state.content_density}"

        prompt = OBSERVE_PROMPT.format(
            user_goal=user_goal,
            url=url,
            page_state_classification=page_state_str,
            dom_compact=dom_compact
        )

        try:
            # Phase 1: OBSERVE - Fast perception only, no reasoning needed
            response = await self.groq.generate(
                prompt,
                model="llama-3.1-8b-instant",
                temperature=0.1,
                max_tokens=800,
                response_format={"type": "json_object"}
            )
            data = json.loads(response)
            data["_ms"] = int((time.time() - t0) * 1000)
            data["_dom_str"] = dom_compact  # Cache for reuse

            # Normalize field names for backward compatibility
            if "current_page" not in data:
                data["current_page"] = data.get("current_page_identity", "unknown")
            if "active_nav" not in data:
                data["active_nav"] = data.get("active_nav_item", "none")
            if "sections_found" not in data:
                data["sections_found"] = []
            # Ensure relevant_elements is a list (may be list of dicts or list of strings)
            if "relevant_elements" not in data:
                data["relevant_elements"] = data.get("opportunities", [])

            return data
        except Exception as e:
            logger.error(f"OBSERVE phase failed: {e}")
            return {
                "current_page": "unknown",
                "active_nav": "none",
                "situation": "Error observing page",
                "visible_data": "",
                "sections_found": [],
                "relevant_elements": [],
                "obstacles": [],
                "_ms": 0
            }

    async def _orient(self, session, observe_output: dict, step_context: str, stagnation_action: str) -> dict:
        """Phase 2: ORIENT - What should I do? (Strategic reasoning with CoT)"""
        t0 = time.time()

        # Format detailed status
        current_subgoal = None
        if session.goal_plan and session.goal_plan.subgoals and 0 <= session.current_subgoal_index < len(session.goal_plan.subgoals):
            current_subgoal = session.goal_plan.subgoals[session.current_subgoal_index]

        subgoal_desc = current_subgoal.description if current_subgoal else "No active sub-goal"
        subgoal_attempts = current_subgoal.attempts if current_subgoal else 0
        subgoal_status = current_subgoal.status if current_subgoal else "none"

        # Stagnation override
        if stagnation_action != "continue":
            subgoal_status += f" (STAGNATION: {stagnation_action})"

        # Monologue history
        monologue_txt = "\n".join([f"Step {m.step}: {m.thought}" for m in session.internal_monologue[-5:]])

        # Format relevant elements summary from OBSERVE output
        relevant_elements_summary = self._format_relevant_elements(observe_output.get("relevant_elements", []))

        prompt = ORIENT_PROMPT.format(
            goal_raw=session.goal_raw,
            current_subgoal_desc=subgoal_desc,
            current_subgoal_attempts=subgoal_attempts,
            subgoal_status_summary=subgoal_status,
            current_page=observe_output.get("current_page", "unknown"),
            active_nav=observe_output.get("active_nav", "none"),
            observe_situation=observe_output.get("situation", ""),
            visible_data=observe_output.get("visible_data", ""),
            sections_found=json.dumps(observe_output.get("sections_found", [])),
            observe_obstacles=json.dumps(observe_output.get("obstacles", [])),
            relevant_elements_summary=relevant_elements_summary,
            monologue_history=monologue_txt or "(First step — no history)",
            map_hints=session.map_hints or step_context or "None",
            current_url=session.last_url or ""
        )

        try:
            # v5 User Request: Swap 120B with 20B but with high reasoning
            result = await self.groq.generate_with_reasoning(
                prompt,
                model="openai/gpt-oss-20b",
                reasoning_effort="high",
                max_completion_tokens=4096
            )
            # result has 'content' (JSON) and 'reasoning' (CoT)
            logger.info(f"💭 ORIENT CoT: {result['reasoning'][:300]}..." if result['reasoning'] else "ORIENT: no CoT")
            content = result['content'].strip()
            if not content:
                import re
                m = re.search(r'\{[\s\S]*\}', result.get('reasoning', ''))
                content = m.group(0) if m else '{"priority": "continue_subgoal", "reasoning": "Extracted from CoT", "active_subgoal_index": 0, "confidence": "medium", "speech_needed": false}'
                logger.warning(f"ORIENT: content was empty, extracted from reasoning")
            data = json.loads(content)
            data["_ms"] = int((time.time() - t0) * 1000)
            data["_cot"] = result['reasoning'][:500]  # Store CoT for debugging
            return data
        except Exception as e:
            logger.error(f"ORIENT phase failed: {e}")
            # Fallback safe orientation
            return {
                "priority": "continue_subgoal",
                "reasoning": "Fallback due to error",
                "active_subgoal_index": session.current_subgoal_index,
                "confidence": "low",
                "speech_needed": False,
                "_ms": 0
            }




    async def _decide(self, session, observe_output: dict, orient_output: dict, filtered_dom: list, warning: str = "") -> dict:
        """Phase 3: DECIDE - Pick exact element to interact with."""
        t0 = time.time()

        # Pre-filter DOM: use OBSERVE's relevant elements to shrink input
        relevant_ids = set(
            el.get("id", "") for el in observe_output.get("relevant_elements", [])
            if isinstance(el, dict) and el.get("id")
        )
        if relevant_ids:
            # Keep: relevant elements + ALL interactive elements (buttons, links, inputs)
            # Previously only kept `<a interactive=true>`, missing many clickable elements
            priority_dom = [
                el for el in filtered_dom
                if el.get("id") in relevant_ids
                or el.get("type", "").lower() in ("nav", "header", "h1", "h2", "h3")
                or el.get("interactive", False)
                or el.get("type", "").lower() in _INTERACTIVE_TYPES
            ]
            dom_str = self._get_compact_dom(priority_dom, limit=80)
            logger.info(f"DECIDE DOM pre-filter: {len(filtered_dom)} → {len(priority_dom)} elements")
        else:
            dom_str = self._get_compact_dom(filtered_dom, limit=100)

        # Recent actions
        recent_actions = "\n".join([f"- {a.action_type} on {a.target_text} ({a.actual_outcome})" for a in session.action_ledger[-3:]]) or "(None)"

        # CRITICAL FALLBACK: If priority implies navigation but no elements found, FORCE SCROLL
        priority = orient_output.get("priority", "continue")
        relevant = observe_output.get("relevant_elements", [])

        # If we need to act but see nothing relevant, and we aren't answering from data
        if priority == "continue_subgoal" and not relevant:
            logger.warning("DECIDE: No relevant elements seen. Injecting SCROLL fallback.")
            return {
                "action": {"type": "scroll", "target_id": "", "text": "down", "url": ""},
                "expected_outcome": "Reveal more content",
                "monologue": "No relevant elements identified in viewport, scrolling down to search.",
                "confidence": "medium",
                "_ms": 0
            }

        # Format relevant elements from OBSERVE for the DECIDE prompt
        relevant_elements_str = self._format_relevant_elements(observe_output.get("relevant_elements", []))

        # Get current subgoal info safely
        current_sg = None
        if session.goal_plan and session.goal_plan.subgoals and 0 <= session.current_subgoal_index < len(session.goal_plan.subgoals):
            current_sg = session.goal_plan.subgoals[session.current_subgoal_index]

        prompt = DECIDE_PROMPT.format(
            priority=orient_output.get("priority", "continue_subgoal"),
            reasoning=orient_output.get("reasoning", ""),
            relevant_elements=relevant_elements_str,
            subgoal_desc=current_sg.description if current_sg else "General Navigation",
            success_signal=current_sg.success_signal if current_sg else "None",
            recent_actions=recent_actions,
            warning=warning or "(None)",
            dom_filtered=dom_str
        )
        
        try:
            # Adaptive reasoning effort: medium is enough when ORIENT is confident
            effort = "medium" if orient_output.get("confidence") == "low" else "low"
            result = await self.groq.generate_with_reasoning(
                prompt,
                model="openai/gpt-oss-120b",
                reasoning_effort=effort,
                max_completion_tokens=1024
            )
            # result has 'content' (JSON) and 'reasoning' (CoT)
            logger.info(f"💭 DECIDE CoT [{effort}]: {result['reasoning'][:300]}..." if result['reasoning'] else "DECIDE: no CoT")
            content = result['content'].strip()
            if not content:
                import re
                m = re.search(r'\{[\s\S]*\}', result.get('reasoning', ''))
                content = m.group(0) if m else '{"action": {"type": "wait", "target_id": "", "text": "Reasoning produced no action output", "url": ""}, "expected_outcome": "None", "monologue": "Content extraction failed", "confidence": "low"}'
                logger.warning(f"DECIDE: content was empty, extracted from reasoning")
            data = json.loads(content)
            data["_ms"] = int((time.time() - t0) * 1000)
            data["_cot"] = result['reasoning'][:500]  # Store CoT for debugging
            return data
        except Exception as e:
            logger.error(f"DECIDE phase failed: {e}")
            return {
                "action": {"type": "wait", "text": "Error in decision phase, waiting.", "target_id": ""},
                "expected_outcome": "None",
                "monologue": "Evaluation failed, holding position.",
                "confidence": "low",
                "_ms": 0
            }

    async def _generate_speech(self, session, orient_output: dict, goal: str, decide_output: dict = None) -> str:
        """
        Parallel Voice Generation (Phase 4 Co-Routine) with Speech Templates.
        TURBO MODE: Aggressively silence intermediate steps to reduce latency.
        """
        priority = orient_output.get("priority", "continue_subgoal")
        speech_needed = orient_output.get("speech_needed", False)
        
        # Check for Turbo Mode (steered by session or env)
        is_turbo = (getattr(session, "interaction_mode", "interactive") == "turbo")

        # ── Critical Moments: ALWAYS Speak (Answer, Clarify, Completion) ──────
        if priority in ("goal_complete", "clarify", "clarify_with_user", "answer_from_data"):
             pass # Fall through to LLM for high-quality final response
        
        # ── Intermediate Steps: SILENCE or Templates ─────────────────────────
        else:
            # If explicit speech requested by Orient (rare), allow it
            if speech_needed:
                pass
            else:
                # TURBO MODE: Use templates for routine actions
                if is_turbo:
                    # Determine template key based on action/priority
                    template_key = None
                    action_type = "unknown"
                    if decide_output:
                        action_type = decide_output.get("action", {}).get("type", "")

                    if "scroll" in action_type:
                        template_key = "scroll" if action_type == "scroll" else "scroll_to"
                    elif "click" in action_type:
                        template_key = "click_routine"
                    elif "type" in action_type:
                        template_key = "type_text"
                    elif "wait" in action_type:
                        template_key = "wait"
                    
                    # Special overrides from Orient priority
                    if priority == "dismiss_obstacle":
                        template_key = "obstacle_modal"
                    elif session.consecutive_failures >= 1:
                        template_key = "failure_retry"

                    # Apply template if found
                    if template_key and template_key in SPEECH_TEMPLATES:
                        template = SPEECH_TEMPLATES[template_key]
                        if template:
                            session.silent_action_count = 0
                            # Format with action text details
                            action_text = ""
                            if decide_output:
                                action_text = decide_output.get("action", {}).get("text", "") 
                            return template.format(text=action_text)
                    
                    # If we fell through here => silence or LLM fallback
                    session.silent_action_count += 1
                    return ""

        # ── LLM Path: For Answer / Clarify / Completion ──────────────────────
        session.silent_action_count = 0  # Reset on LLM speech

        # Build action summary from decide output if available
        action_summary = f"Priority: {priority}"
        if decide_output:
            action_data = decide_output.get("action", {})
            action_type = action_data.get("type", "unknown")
            action_text = action_data.get("text", "") or action_data.get("target_id", "")
            action_summary = f"{action_type} on {action_text}"

        is_first_step = "Yes" if session.step_number == 0 else "No"

        prompt = SPEECH_PROMPT.format(
            reasoning=orient_output.get("reasoning", "Proceeding with task."),
            action_summary=action_summary,
            goal=goal,
            is_first_step=is_first_step
        )

        try:
            # Quick 60-token response for answers/completion
            response = await self.groq.generate(
                prompt,
                model=self.llm_model,
                temperature=0.5,
                max_tokens=80
            )
            return response.strip().replace('"', '')
        except:
            return ""

    async def orchestrate(self, query: str, dom_context: list, history: str = "", language: str = "en") -> AsyncGenerator[dict, None]:
        """
        Streaming Orchestrator (Phase A - Legacy/Single-Turn Wrapper)
        This is kept for backward compatibility with the streaming endpoint /visual_orchestrate
        In v4, plan_next_step is the primary method, but this wraps it for streaming clients.
        """
        # 1. Yield "thinking" state
        yield {"type": "status", "content": "Analyzing page structure..."}
        
        # 2. Fast Sense (Parallel)
        sense_task = asyncio.create_task(self._run_fast_sense(query, dom_context))
        
        # 3. Full Plan (mapped to plan_next_step logic)
        # We construct a synthetic session context for this stateless request
        # In a real v4 app, the client calls plan_next_step directly.
        # This wrapper emulates a single-step plan.
        
        try:
             # Wait for sense to yield speech early
            sense_res = await sense_task
            if sense_res.get("speech"):
                 yield {"type": "voice", "content": sense_res["speech"]}
            
            plan = await self.plan_next_step(
                goal=query,
                dom_context=dom_context,
                step_number=0, # Stateless assumption
                client_id="stream_user",
                session_id=f"stream_{int(time.time())}"
            )
            
            # Yield action
            if plan.get("action"):
                yield {"type": "action", "content": plan["action"]}
                
            # Yield final thought
            if plan.get("reasoning"):
                yield {"type": "reasoning", "content": plan["reasoning"]}
                
        except Exception as e:
            logger.error(f"Orchestration stream failed: {e}")
            yield {"type": "error", "content": str(e)}

    # ── Main Pipeline (Replaces plan_next_step) ──────────────────────────────

    def _get_domain(self, url: str) -> str:
        """Extract clean domain from URL (e.g. console.groq.com -> groq.com)"""
        if not url:
            return "unknown"
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc or parsed.path
            domain = netloc.replace("www.", "")
            return domain
        except Exception:
            return "unknown"

    async def plan_next_step(
        self,
        goal: str,
        dom_context: list,
        step_number: int,
        warning_message: str = "",
        current_url: str = "",
        last_action: str = "",
        map_hints: str = "",
        client_id: str = "demo",
        action_history: list = None,
        dom_diff: str = "",
        conversation_history: str = "",
        last_dom_context: list = None,
        fast_sense_speech: str = None,
        interaction_mode: str = "interactive",
        session_id: str = "default",
        active_states: dict = None,
        data_tables: list = None,
        page_title: str = "",
    ) -> dict:
        """
        v5 Pipeline: PageGraph → Query-Focused Slice → Single REASON call.
        Replaces the 3-call OBSERVE→ORIENT→DECIDE pipeline.
        """
        pipeline_start = time.time()

        # 1. Load or Create Session
        if not self.session_mgr:
            logger.error("Session Manager missing")
            raise RuntimeError("Session Manager not initialized")

        session = await self.session_mgr.get_or_create(session_id, client_id)

        # Update Session with current request data
        session.interaction_mode = interaction_mode
        session.map_hints = map_hints
        session.domain = self._get_domain(current_url)

        # Detect new mission
        is_new_mission = (session.step_number == 0 and not session.goal_raw) or (goal and goal != session.goal_raw)

        if is_new_mission:
            if session.goal_raw and goal != session.goal_raw:
                logger.info(f"🔄 New mission detected! Old: '{session.goal_raw[:40]}' → New: '{goal[:40]}'. Resetting session.")
                from session_manager import GoalPlan, ReflexionMemory
                session.goal_plan = GoalPlan()
                session.current_subgoal_index = 0
                session.mission_status = "idle"
                session.internal_monologue = []
                session.action_ledger = []
                session.reflexion_memory = ReflexionMemory()
                session.stagnation_count = 0
                session.consecutive_failures = 0
                session.step_number = 0
                session.silent_action_count = 0
                session.dom_hash = 0
                session.last_active_states = None
                session.last_has_modal = False
                session.last_element_ids = None

            session.goal_raw = goal
            session.map_hints = map_hints

            # v5: Stale Context Warning (Turn 0)
            # If we start a new mission on a deep page, warn the planner.
            if current_url and current_url.count('/') > 3:  # Heuristic for deep URL
                 logger.info(f"⚠️ New mission starting on deep URL: {current_url}. Adding context warning.")
                 session.map_hints = (session.map_hints or "") + "\nWARNING: User started on a deep page. If it looks irrelevant to '" + goal + "', navigate Home or to Dashboard first."

            # Explorer Mode: First Contact
            if session.hivemind_mode == "explorer" and not session.site_skeleton:
                await self._explorer_first_contact(session, dom_context, current_url)

            # Goal Decomposition
            logger.info("🧩 Decomposing Goal into Sub-Goals...")
            decomposed_plan = await self._decompose_goal(goal, dom_context, map_hints=session.map_hints or "")
            session.goal_plan = decomposed_plan
            session.current_subgoal_index = 0
            plan_desc = " -> ".join([sg.description for sg in session.goal_plan.subgoals])
            logger.info(f"📋 Goal Plan: {plan_desc}")

        # 2. Build or Update PageGraph (v5)
        page_state = classify_page_state(dom_context, current_url)
        session.page_state = page_state

        page_graph = None
        if self.graph_mgr:
            page_graph = self.graph_mgr.from_widget_data(
                elements=dom_context,
                url=current_url,
                title=page_title,
                active_states=active_states,
                data_tables=data_tables,
            )
            page_graph.step_number = session.step_number
            page_graph.previous_url = session.last_url
            await self.graph_mgr.store(session_id, page_graph)

        # 3. Validate Previous Action (v5 deterministic)
        #    Compute real deltas instead of hardcoded values.
        validation_res = None
        curr_element_ids = {e.get("id") for e in dom_context if e.get("id")} if dom_context else set()
        curr_dom_hash = hash(frozenset(curr_element_ids)) if curr_element_ids else 0

        if session.action_ledger:
            last_record = session.action_ledger[-1]
            url_changed = bool(current_url and session.last_url and current_url != session.last_url)

            # v5: Nav state comparison using persisted previous state
            prev_active_page = ""
            curr_active_page = ""
            if session.last_active_states:
                prev_active_page = session.last_active_states.get("activePage", "") or ""
            if active_states:
                curr_active_page = active_states.get("activePage", "") or ""

            # v5: DOM change detection using element ID sets
            prev_ids = set(session.last_element_ids) if session.last_element_ids else set()
            new_ids = curr_element_ids - prev_ids
            removed_ids = prev_ids - curr_element_ids
            dom_actually_changed = bool(new_ids or removed_ids) or (curr_dom_hash != session.dom_hash)

            outcome_dict = {
                "url_changed": url_changed,
                "dom_changed": dom_actually_changed,
                "new_elements_count": len(new_ids),
                "has_modal": page_state.has_modal,
                "prev_has_modal": session.last_has_modal,
                "prev_active_page": prev_active_page,
                "curr_active_page": curr_active_page,
            }

            validation_res = await self.validate_action_outcome(
                last_record.action_type,
                session.dom_hash,
                outcome_dict
            )

            # Update ledger
            last_record.actual_outcome = validation_res.reason
            last_record.dom_changed = validation_res.success

            # v5: Reflexion memory — record failure for self-correction
            if not validation_res.success and session.reflexion_memory:
                action_desc = f"{last_record.action_type} on '{last_record.target_text or last_record.target_id}'"
                session.reflexion_memory.add_failure(
                    step=session.step_number,
                    action=action_desc,
                    outcome=validation_res.reason,
                )
                logger.info(f"📝 Reflexion: recorded failure at step {session.step_number}")

            # Stagnation check
            stagnation_action = self.handle_stagnation(session, validation_res)
        else:
            stagnation_action = "continue"
            validation_res = ValidationResult(True, "First step")

        # 4. Query-Focused Slice (v5 — zero LLM cost, ~2ms)
        if page_graph and self.graph_mgr:
            page_slice = self.graph_mgr.query_slice(page_graph, goal, max_elements=60)
        else:
            # Fallback to legacy DOM serialization if PageGraph unavailable
            filtered_dom = self._smart_dom_filter(dom_context)
            page_slice = self._get_compact_dom(filtered_dom, limit=100)

        # 4b. Stale Target ID Detection — trigger re-plan if sub-goal target is gone
        current_subgoal = None
        if session.goal_plan and session.goal_plan.subgoals and 0 <= session.current_subgoal_index < len(session.goal_plan.subgoals):
            current_subgoal = session.goal_plan.subgoals[session.current_subgoal_index]

        if current_subgoal and current_subgoal.status != "done" and current_subgoal.attempts >= 2:
            # Extract target ID from sub-goal description (e.g. "(id: t-1dusf1b)")
            import re
            sg_id_match = re.search(r'\(id:\s*([^\)]+)\)', current_subgoal.description)
            if sg_id_match:
                sg_target_id = sg_id_match.group(1).strip()
                if sg_target_id not in curr_element_ids:
                    logger.warning(
                        f"🔄 RE-PLAN: Sub-goal target '{sg_target_id}' not found in DOM after "
                        f"{current_subgoal.attempts} attempts. Scrapping plan and re-decomposing."
                    )
                    # Re-decompose the goal with current page context
                    new_plan = await self._decompose_goal(goal, dom_context, map_hints=session.map_hints or "")
                    session.goal_plan = new_plan
                    session.current_subgoal_index = 0
                    # Reset stagnation since we have a fresh plan
                    session.consecutive_failures = 0
                    session.stagnation_count = 0
                    plan_desc = " -> ".join([sg.description for sg in new_plan.subgoals])
                    logger.info(f"📋 New Goal Plan: {plan_desc}")
                    # Update current_subgoal for the REASON call
                    if new_plan.subgoals:
                        current_subgoal = new_plan.subgoals[0]

        # 5. Single REASON call (v5 — replaces OBSERVE + ORIENT + DECIDE)
        reason_res = await self._reason(session, page_slice, goal, stagnation_action=stagnation_action)

        action_data = reason_res.get("action", {})
        action_type = action_data.get("type", "wait")
        speech = reason_res.get("speech", "")
        confidence = reason_res.get("confidence", "medium")

        # ── PREMATURE ANSWER GUARD ──────────────────────────────────────
        # If the goal plan has pending navigation sub-goals and we haven't
        # navigated yet, block "answer" actions — the model is hallucinating.
        if action_type == "answer" and session.goal_plan and session.goal_plan.subgoals:
            pending_nav = [
                sg for sg in session.goal_plan.subgoals
                if sg.status == "pending" and sg.type == "navigate"
            ]
            has_navigated = bool(session.last_url and current_url and current_url != session.last_url)
            if pending_nav and not has_navigated and session.step_number < 3:
                first_nav = pending_nav[0]
                logger.warning(
                    f"🛡️ ANSWER BLOCKED: Model tried to answer on step {session.step_number} "
                    f"but navigation sub-goal '{first_nav.description}' is still pending. "
                    f"Overriding to follow the plan."
                )
                # Try to extract target element ID from sub-goal description (e.g. "(id: t-uioz0c)")
                import re
                id_match = re.search(r'\(id:\s*([^\)]+)\)', first_nav.description)
                target_id = id_match.group(1).strip() if id_match else ""

                # Also scan DOM for a link matching the sub-goal text
                if not target_id:
                    nav_words = set(w.lower() for w in first_nav.description.split() if len(w) > 3)
                    for el in dom_context:
                        if el.get("interactive") and el.get("text"):
                            el_text_lower = el["text"].lower()
                            if any(w in el_text_lower for w in nav_words):
                                target_id = el.get("id", "")
                                break

                action_data = {"type": "click", "target_id": target_id, "text": ""}
                action_type = "click"
                speech = "Let me navigate there first."
                confidence = "medium"
                reason_res["_answer_blocked"] = True

        # ── GHOST TARGET GUARD ─────────────────────────────────────────
        # If the model output a target_id that doesn't exist in the DOM,
        # try to find a text-match substitute before sending to the widget.
        if action_type == "click" and action_data.get("target_id"):
            target_id = action_data["target_id"]
            if target_id not in curr_element_ids:
                # Target doesn't exist on this page — find closest text match
                reason_text = reason_res.get("reasoning", "").lower()
                substitute_id = ""
                best_score = 0
                for el in dom_context:
                    if not el.get("interactive") or not el.get("id"):
                        continue
                    el_text = el.get("text", "").lower()
                    if not el_text:
                        continue
                    # Score: how many words from reasoning match element text
                    score = sum(1 for w in el_text.split() if w in reason_text)
                    if score > best_score:
                        best_score = score
                        substitute_id = el["id"]
                if substitute_id:
                    logger.warning(
                        f"👻 GHOST TARGET: '{target_id}' not in DOM. "
                        f"Substituted with '{substitute_id}' (text match score: {best_score})"
                    )
                    action_data["target_id"] = substitute_id
                else:
                    logger.warning(f"👻 GHOST TARGET: '{target_id}' not in DOM and no substitute found. Action will likely fail.")

        # FAST SENSE SYNC: If fast sense already spoke, don't double-speak
        if fast_sense_speech and action_type not in ("answer", "clarify"):
            logger.info(f"⏭️ Skipping REASON speech (Fast Sense already spoke)")
            speech = ""

        # Handle answer action — speech IS the answer
        if action_type == "answer":
            answer_text = action_data.get("text", "") or reason_res.get("observation", "")
            
            # v5 User Request: Always read the answer text, don't rely on generic speech like "Here is the answer."
            # If speech is too short (< 20 chars) or generic, use the full answer text.
            if not speech or len(speech) < 20 or "providing details" in speech.lower() or "here is the answer" in speech.lower():
                speech = answer_text
            
            # Truncate for TTS
            if len(speech) > 400:
                speech = speech[:397].rsplit('.', 1)[0] + '.'


        # 6. Finalize & Save Session
        from session_manager import ActionRecord, MonologueEntry

        session.internal_monologue.append(MonologueEntry(
            step=session.step_number,
            thought=reason_res.get("reasoning", ""),
            confidence=confidence,
            page_type=page_state.page_type,
            timestamp=time.time()
        ))
        session.internal_monologue = session.internal_monologue[-10:]

        new_record = ActionRecord(
            step=session.step_number,
            action_type=action_type,
            target_id=action_data.get("target_id", ""),
            target_text=action_data.get("text", ""),
            expected_outcome=reason_res.get("observation", ""),
            actual_outcome="pending"
        )
        session.action_ledger.append(new_record)

        # Sub-goal advancement check
        self._check_subgoal_advancement(session, page_state, dom_context, validation_res, current_url=current_url)

        # Progressive site model update (Explorer mode)
        if session.hivemind_mode == "explorer" and current_url and current_url != session.last_url:
            self._record_page_visit(session, current_url, page_state, dom_context)

        if action_type == "answer":
            session.mission_status = "completed"

        session.step_number += 1
        session.last_url = current_url

        # v5: Persist current state for next step's validation comparison
        session.dom_hash = curr_dom_hash
        session.last_active_states = active_states
        session.last_has_modal = page_state.has_modal
        session.last_element_ids = list(curr_element_ids) if curr_element_ids else []

        await self.session_mgr.save(session)

        total_time = int((time.time() - pipeline_start) * 1000)

        logger.info(
            f"⚡ v5 Turn {session.step_number} | "
            f"{total_time}ms total | "
            f"REASON: {reason_res.get('_ms', 0)}ms | "
            f"page={page_state.page_type} | "
            f"action={action_type} | "
            f"confidence={confidence} | "
            f"validation={validation_res.reason if validation_res else 'N/A'} | "
            f"reflexion={len(session.reflexion_memory.entries) if session.reflexion_memory else 0} | "
            f"subgoal={session.current_subgoal_index}/{len(session.goal_plan.subgoals) if session.goal_plan else 0}"
        )

        return {
            "action": action_data,
            "reasoning": reason_res.get("reasoning", ""),
            "confidence": confidence,
            "speech": speech,
            "_timing_ms": total_time,
            "_phase_timings": {
                "reason_ms": reason_res.get("_ms", 0),
                "total_ms": total_time,
            },
            "_validation": {
                "success": validation_res.success if validation_res else True,
                "reason": validation_res.reason if validation_res else "N/A",
                "signal": validation_res.signal if validation_res else "",
            }
        }

    def _check_subgoal_advancement(self, session, page_state, dom_context: list, validation: 'ValidationResult' = None, current_url: str = ""):
        """
        Check if the current sub-goal's success signal has been met.
        Advances to the next sub-goal if so.

        Uses strict URL-based validation for navigation sub-goals to prevent
        false completion (the #1 cause of "already here" blindness).
        """
        if not session.goal_plan or not session.goal_plan.subgoals:
            return

        idx = session.current_subgoal_index
        if idx < 0 or idx >= len(session.goal_plan.subgoals):
            return

        sg = session.goal_plan.subgoals[idx]
        current_url = current_url or getattr(page_state, 'url', '') or session.last_url

        # Only increment attempts here (once per turn)
        sg.attempts += 1

        # Guard: Skip sub-goals already marked done (prevents double-advancement)
        if sg.status == "done":
            logger.info(f"⏭️ Sub-goal {idx} already done, skipping advancement check.")
            return

        advanced = False

        if validation and validation.success:
            signal = sg.success_signal.lower() if sg.success_signal else ""
            desc = sg.description.lower()
            sg_type = sg.type.lower() if sg.type else ""

            # Detect if this is a navigation sub-goal
            is_navigational = (
                sg_type == "navigate"
                or "navigate" in desc
                or "go to" in desc
                or "open" in desc
                or "click" in desc and ("page" in signal or "url" in signal)
            )
            url_changed = current_url and session.last_url and current_url != session.last_url

            # ── STRICT NAVIGATION VALIDATION ──
            if is_navigational:
                if url_changed:
                    # URL changed — but does it match the sub-goal's expected destination?
                    # If success_signal has a URL pattern, verify the new URL matches.
                    if signal and ("url" in signal or "/" in signal):
                        # Extract expected URL fragment from success_signal
                        import re
                        url_fragments = re.findall(r'/[\w\-/]+', signal)
                        if url_fragments:
                            matches_expected = any(frag in current_url.lower() for frag in url_fragments)
                            if matches_expected:
                                advanced = True
                                logger.info(f"✅ Sub-goal {idx}: URL changed to expected destination ({current_url})")
                            else:
                                logger.warning(
                                    f"⚠️ Sub-goal {idx}: URL changed ({session.last_url} → {current_url}) "
                                    f"but does NOT match expected signal '{signal}'. Wrong page!"
                                )
                                # Don't advance — we navigated to the wrong page
                        else:
                            # No URL pattern in signal, but maybe a keyword check?
                            # v5: Stricter check. If success_signal mentions words, check URL or Page Title.
                            signal_words = [w for w in signal.split() if len(w) > 4 and w not in ("contains", "changed", "loaded", "check", "verify")]
                            if signal_words:
                                matching = [w for w in signal_words if w in current_url.lower()]
                                if matching:
                                    advanced = True
                                    logger.info(f"✅ Sub-goal {idx}: URL matches verification keywords {matching}")
                                else:
                                    logger.warning(f"⚠️ Sub-goal {idx}: URL changed to {current_url} but missing keywords {signal_words}")
                                    # Fallback: Is title matching or page type valid?
                                    if getattr(page_state, 'page_type', 'unknown') not in ('unknown', 'error', 'auth'):
                                        advanced = True # Allow widely if we landed on a valid content page
                                        logger.info(f"✅ Sub-goal {idx}: Keyword mismatch overridden because page_type ({page_state.page_type}) differs from 'unknown'.")
                            else:
                                # Truly generic signal
                                advanced = True
                                logger.info(f"✅ Sub-goal {idx}: URL changed ({session.last_url} -> {current_url})")
                    else:
                        # No signal or signal doesn't have URL check — accept URL change
                        advanced = True
                        logger.info(f"✅ Sub-goal {idx}: URL changed ({session.last_url} -> {current_url})")
                elif "modal" in signal or "popup" in signal:
                    # Special case: modals don't change URL but are valid
                    if page_state.has_modal:
                        advanced = True
                elif validation.reason and "new elements" in validation.reason:
                    # SPA page loads typically add 30+ elements.
                    # Dropdowns/menus add 5-20 elements — NOT a page transition.
                    try:
                        new_count = int(validation.reason.split()[0])
                        if new_count > 30:
                            advanced = True
                            logger.info(f"✅ Sub-goal {idx}: {new_count} new elements — likely SPA page load")
                        else:
                            logger.info(f"🔍 Sub-goal {idx}: Only {new_count} new elements — likely dropdown/menu, not page transition")
                    except (ValueError, IndexError):
                        pass

                if not advanced:
                    logger.info(f"🛑 Sub-goal {idx}: Navigation required but URL unchanged. Holding. (attempt {sg.attempts})")
                    return

            # ── NON-NAVIGATION SUB-GOALS ──
            else:
                # For observe/interact/answer sub-goals, use heuristic checks
                if "visible" in signal or "appears" in signal or "loaded" in signal:
                    if validation.reason in ("URL changed", "DOM changed (heuristic pass-through)") or "new elements" in validation.reason:
                        advanced = True

                elif "input" in signal or "focused" in signal or "entered" in signal:
                    if sg_type == "input" and validation.success:
                        advanced = True

                elif "filter" in signal or "applied" in signal:
                    if validation.success and "DOM" in validation.reason:
                        advanced = True

                elif sg_type in ("observe", "answer"):
                    # Observe/answer sub-goals advance when the ORIENT phase says "answer_from_data"
                    # This is handled in plan_next_step directly, not here
                    pass

        if advanced:
            sg.status = "done"
            logger.info(f"✅ Sub-goal {idx} DONE: '{sg.description}' (after {sg.attempts} attempts)")

            # Move to next pending sub-goal
            next_idx = idx + 1
            while next_idx < len(session.goal_plan.subgoals):
                next_sg = session.goal_plan.subgoals[next_idx]
                if next_sg.status == "pending":
                    next_sg.status = "active"
                    session.current_subgoal_index = next_idx
                    logger.info(f"🎯 Advanced to sub-goal {next_idx}: '{next_sg.description}'")
                    return
                next_idx += 1

            # All sub-goals done
            session.mission_status = "completed"
            logger.info(f"🏁 All sub-goals completed for mission: '{session.goal_raw}'")

    def _record_page_visit(self, session, url: str, page_state, dom_context: list):
        """(C3) Progressive site model: record a new page visit in explorer mode."""
        if not session.site_skeleton:
            return

        from session_manager import SiteNode

        if url not in session.site_skeleton.nodes:
            key_elements = [
                el.get("text", "")[:30]
                for el in dom_context
                if el.get("interactive", False) and el.get("text", "").strip()
            ][:8]

            session.site_skeleton.nodes[url] = SiteNode(
                url=url,
                page_type=page_state.page_type,
                key_elements=key_elements,
                discovered_from=session.last_url,
                timestamp=time.time(),
            )
            session.site_skeleton.page_count += 1
            session.site_skeleton.discovered_urls.append(url)
            logger.debug(f"🗺️ Explorer: Recorded page visit #{session.site_skeleton.page_count}: {url} ({page_state.page_type})")

    async def persist_explorer_knowledge(self, session):
        """
        (C4) Explorer → Mapped Flywheel.
        At session end, persist discovered site model to Qdrant as a Website_Map entry.
        """
        if session.hivemind_mode != "explorer":
            return
        if not session.site_skeleton or session.site_skeleton.page_count < 3:
            return  # Not enough data to be useful
        if not self.qdrant or not hasattr(self.qdrant, 'enabled') or not self.qdrant.enabled:
            return

        map_text = f"Website: {session.site_skeleton.domain}\n"
        map_text += f"Pages discovered: {session.site_skeleton.page_count}\n"
        for url, node in session.site_skeleton.nodes.items():
            map_text += f"- {url} ({node.page_type}): {', '.join(node.key_elements[:5])}\n"

        try:
            # Use existing Qdrant client to upsert
            import uuid
            from qdrant_client.http.models import PointStruct

            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=self.embeddings.embed_query(map_text) if self.embeddings else [0.0] * 384,
                payload={
                    "text": map_text,
                    "doc_type": "Website_Map",
                    "domain": session.site_skeleton.domain,
                    "client_id": session.client_id,
                    "page_count": session.site_skeleton.page_count,
                    "discovered_urls": session.site_skeleton.discovered_urls[:20],
                    "source": "explorer_flywheel",
                }
            )
            await self.qdrant.client.upsert(
                collection_name=self.qdrant.collection_name,
                points=[point],
            )
            logger.info(
                f"🧠 Explorer Flywheel: Persisted site map for '{session.site_skeleton.domain}' "
                f"({session.site_skeleton.page_count} pages) to Qdrant"
            )
        except Exception as e:
            logger.warning(f"Failed to persist explorer knowledge: {e}")

    async def check_hivemind_status(self, current_url: str, client_id: str) -> dict:
        """
        Check if the current domain is known to HiveMind (pre-indexed).
        Uses doc_type=Website_Map for precise domain matching.
        Returns a status dict with mode='mapped'|'explorer'.
        """
        if not self.qdrant or not hasattr(self.qdrant, 'enabled') or not self.qdrant.enabled:
            return {"mode": "explorer", "reason": "HiveMind disabled"}

        if not current_url:
            return {"mode": "explorer", "reason": "No URL provided"}

        try:
            from urllib.parse import urlparse
            parsed = urlparse(current_url)
            domain = parsed.netloc.replace("www.", "")
            
            if not domain:
                 # Handle cases like "about:blank" or valid URLs without netloc (e.g. file://)
                 return {"mode": "explorer", "reason": "No valid domain extracted"}

            # Query with BOTH new schema and legacy fields via should
            hits_response = await self.qdrant.client.query_points(
                collection_name=self.qdrant.collection_name,
                query=self.embeddings.embed_query(f"website map {domain}"),
                query_filter=Filter(
                    must=[
                        FieldCondition(key="domain", match=MatchValue(value=domain)),
                    ],
                    should=[
                        # New schema
                        FieldCondition(key="doc_type", match=MatchValue(value="Website_Map")),
                        # Legacy
                        FieldCondition(key="label", match=MatchValue(value="website_sitemap")),
                    ]
                ),
                limit=1
            )

            if hits_response.points:
                logger.info(f"🧠 HiveMind MAPPED: Domain '{domain}' has Website_Map (score: {hits_response.points[0].score:.3f})")
                return {"mode": "mapped", "reason": f"Website_Map found for {domain}"}

            # Fallback: Check by URL text match
            fallback_response = await self.qdrant.client.query_points(
                collection_name=self.qdrant.collection_name,
                query=self.embeddings.embed_query("sitemap home page structure"),
                query_filter=Filter(
                    must=[
                        FieldCondition(key="url", match=MatchText(text=domain)),
                    ],
                    should=[
                        FieldCondition(key="client_id", match=MatchValue(value=client_id)),
                        FieldCondition(key="tenant_id", match=MatchValue(value=client_id)),
                    ]
                ),
                limit=1
            )

            if fallback_response.points:
                logger.info(f"🧠 HiveMind Match (legacy): Domain '{domain}' is KNOWN.")
                return {"mode": "mapped", "reason": f"Indexed domain (legacy): {domain}"}

            logger.info(f"🌑 HiveMind: Domain '{domain}' is UNKNOWN (Explorer Mode).")
            return {"mode": "explorer", "reason": "New territory"}

        except Exception as e:
            logger.warning(f"HiveMind check failed: {e}")
            return {"mode": "explorer", "reason": "Check failed"}

    async def get_navigation_hints(self, goal: str, client_id: str, current_url: str = "") -> str:
        """
        Query Qdrant for pre-indexed site navigation hints (STATIC - called once at Step 0).
        Returns a string like: "HINT: To find villas, navigate to /search. Key elements: #search-bar, .filter-price"
        
        IMPORTANT: Filters results by the current domain to prevent cross-domain confusion
        (e.g. returning Groq hints when user is on Daytona).
        """
        if not self.qdrant or not hasattr(self.qdrant, 'enabled') or not self.qdrant.enabled:
            return ""
        
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            
            if not self.embeddings:
                return ""
            
            # Extract current domain for filtering
            current_domain = self._get_domain(current_url) if current_url else ""
                
            # Match both new schema AND legacy format
            hits_response = await self.qdrant.client.query_points(
                collection_name=self.qdrant.collection_name,
                query=self.embeddings.embed_query(goal),
                query_filter=Filter(
                    should=[
                        FieldCondition(key="doc_type", match=MatchValue(value="Website_Map")),
                        FieldCondition(key="label", match=MatchValue(value="website_sitemap")),
                    ]
                ),
                limit=3  # Fetch more candidates so we can filter by domain
            )
            hits = hits_response.points
            
            if hits:
                for hit in hits:
                    payload = hit.payload
                    url = payload.get("url", "")
                    
                    # Domain filter: skip hints from other websites
                    if current_domain and url:
                        hint_domain = self._get_domain(url)
                        if hint_domain != "unknown" and current_domain != "unknown":
                            # Check if domains share a root (e.g. console.groq.com vs groq.com)
                            if not (current_domain.endswith(hint_domain) or hint_domain.endswith(current_domain)):
                                logger.info(f"🗺️ Map hint skipped (domain mismatch): {hint_domain} ≠ {current_domain}")
                                continue
                    
                    selectors = payload.get("key_selectors", [])
                    concept = payload.get("text") or payload.get("concept", "")
                    
                    hint = f"HINT: To '{concept}', navigate to {url}."
                    if selectors:
                        hint += f" Key elements: {', '.join(selectors)}."
                    return hint
            
            return ""
        except Exception as e:
            logger.warning(f"Qdrant map lookup failed: {e}")
            return ""

    async def get_step_context(self, dom_context: list, current_url: str, client_id: str) -> str:
        """
        Query Qdrant for DYNAMIC per-step context based on current DOM elements.
        Called EVERY step to provide element-specific guidance.
        """
        if not self.qdrant or not hasattr(self.qdrant, 'enabled') or not self.qdrant.enabled:
            return ""
        
        try:
            # Build a query from visible elements
            key_elements = []
            for el in dom_context[:15]:
                text = el.get("text", "").strip()
                el_type = el.get("type", "")
                if text and len(text) < 50:
                    if el_type in ["button", "a", "h1", "h2", "h3", "label", "input"]:
                        key_elements.append(text)
            
            if not key_elements:
                return ""
            
            query_text = f"Page: {current_url}. Elements: {', '.join(key_elements[:5])}"
            
            if not self.embeddings:
                return ""

            from qdrant_client.models import Filter, FieldCondition, MatchValue
            
            # Match both new schema AND legacy
            hits_response = await self.qdrant.client.query_points(
                collection_name=self.qdrant.collection_name,
                query=self.embeddings.embed_query(query_text),
                query_filter=Filter(
                    should=[
                        FieldCondition(key="doc_type", match=MatchValue(value="Element_Context")),
                        FieldCondition(key="label", match=MatchValue(value="element_context")),
                    ]
                ),
                limit=2,
                score_threshold=0.5
            )
            hits = hits_response.points
            
            if hits:
                contexts = []
                for hit in hits:
                    ctx = hit.payload.get("text") or hit.payload.get("context", "")
                    if ctx:
                        contexts.append(ctx)
                if contexts:
                    return "ELEMENT CONTEXT: " + " | ".join(contexts)
            
            return ""
        except Exception as e:
            logger.warning(f"Qdrant step context lookup failed: {e}")
            return ""

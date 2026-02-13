import asyncio
import json
import logging
import os
import time
from typing import Dict, Any, List, Optional, AsyncGenerator

from llm_providers.groq_provider import GroqProvider

# Qdrant imports for HiveMind checks
try:
    from qdrant_client.http import models
    from qdrant_client.http.models import Filter, FieldCondition, MatchText, MatchValue
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

logger = logging.getLogger(__name__)

VOICE_STREAM_PROMPT = """
<system_configuration>
You are TARA, an expressive Visual Co-Pilot helping users navigate websites.
Goal: Identify the website/domain from the context and give a warm, confident welcome.

Context: The user just started a mission on a webpage.
Page Title/Heading: {ui_context}
HiveMind Hints: {map_hints}
MISSION MODE: {mission_mode}

Tone: Professional yet warm, authoritative but helpful (Visual Co-Pilot persona).

Rules:
1. Identify the website name or domain (e.g., "Groq", "Airbnb", "Daytona").
2. Introduce yourself as TARA, their visual co-browsing pilot.
3. Be brief and punchy. Do NOT list UI elements like buttons or menus.
4. If HiveMind is active (MIND mode), mention you have mapped this site.

Examples:
- "Welcome to the Groq console. I am TARA, your visual co-browsing pilot. I can help you navigate the documentation or API settings."
- "I see we are on Airbnb. I'm TARA, your pilot. Let's find you a great place to stay."
- "Welcome to GitHub. I have a map of this repository. I'm TARA, ready to assist."
</system_configuration>
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
{{"type": "click", "target_id": "element_id_or_exact_text", "text": "Start Building"}}
{{"type": "scroll_to", "target_id": "id", "text": "Section Title"}}
{{"type": "scroll", "target_id": "", "text": "scrolling down"}}
{{"type": "type_text", "target_id": "id", "text": "hello"}}
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


FAST_FILTER_PROMPT = """You are a DOM filter for a Visual Co-Pilot. Given a user's GOAL and a list of DOM elements, select ONLY the elements relevant to achieving the goal.

GOAL: "{goal}"

RULES:
1. Output ONLY valid JSON.
2. **PRIORITY**: Select elements marked `interactive='true'`.
3. Include headings/labels ONLY if they are essential for context (e.g. "Sign In" header).
4. **FILTER AGGRESSIVELY**: Do NOT select plain text divs/spans unless they are the ONLY way to interact.
5. If a button and its label are separate, select the BUTTON (interactive='true').
6. "speech" must be under 15 words.

Respond in json format.

DOM ELEMENTS:
{dom_context}
"""

FAST_VALIDATOR_PROMPT = """You are an action validator for a Visual Co-Pilot. Compare the DOM state BEFORE and AFTER an action to determine if it succeeded.

LAST ACTION: {last_action}
EXPECTED OUTCOME: The page should have changed in response to the action.

PRE-ACTION DOM (key elements):
{pre_dom}

POST-ACTION DOM (key elements):
{post_dom}

RULES:
1. Output ONLY valid JSON: {{"success": true/false, "reason": "brief explanation", "changes_detected": "what actually changed"}}
2. Success = the DOM changed meaningfully (new content, navigation, form state change).
3. Failure = DOM is identical or only trivially changed (timestamps, animations).
4. If action was "click" on a link but URL didn't change and no modal appeared, that's a FAILURE.
5. Be strict: minor text changes without structural change = failure.

Respond in json format.
"""

# Interactive element types for smart DOM filtering
_INTERACTIVE_TYPES = frozenset({"button", "a", "input", "select", "textarea", "label", "option"})
_LANDMARK_TYPES = frozenset({"h1", "h2", "h3", "title", "header", "nav", "main", "form"})


class VisualOrchestrator:
    """Manages dual-stream generation for Visual Co-Pilot"""
    
    def __init__(self, groq_provider: GroqProvider, qdrant_client=None, embeddings=None):
        self.groq = groq_provider
        self.qdrant = qdrant_client
        self.embeddings = embeddings
        # Load model IDs from environment variables (specified in docker-compose.yml)
        self.llm_model = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
        self.analytics_model = os.getenv("ANALYTICS_MODEL", "qwen/qwen3-32b")
        self.use_hivemind = str(os.getenv("HIVEMIND_IN_VISUAL_COPILOT", "true")).lower() == "true"
        logger.info(f"🚀 VisualOrchestrator initialized with models: LLM={self.llm_model}, ANALYTICS={self.analytics_model}, HiveMind={self.use_hivemind}")

    def _get_compact_dom(self, dom_context: List[Dict[str, Any]], limit: int = 200) -> str:
        """Produces a minimal string representation of the DOM to save tokens."""
        compact = []
        for el in dom_context[:limit]:
            el_type = el.get("type", "div")
            el_id = el.get("id")
            text = el.get("text", "").strip()
            is_interactive = el.get("interactive", False)
            
            if not el_id and not text:
                continue
                
            entry = f"<{el_type}"
            if el_id:
                entry += f" id='{el_id}'"
            if is_interactive:
                entry += " interactive='true'"
            if text:
                # Truncate text to avoid bloat
                entry += f"> {text[:60]}"
            else:
                entry += " />"
            compact.append(entry)
        return "\n".join(compact)

    def _smart_dom_filter(self, dom_context: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Pre-filter DOM to keep only interactive + landmark elements.
        Reduces token count by ~60% before sending to LLM.
        """
        filtered = []
        for el in dom_context:
            el_type = el.get("type", "div").lower()
            text = el.get("text", "").strip()
            el_id = el.get("id")

            # Always keep interactive elements
            if el_type in _INTERACTIVE_TYPES:
                filtered.append(el)
            # Keep landmarks (headings, nav, forms) for orientation
            elif el_type in _LANDMARK_TYPES:
                filtered.append(el)
            # Keep elements with IDs (likely meaningful targets)
            elif el_id and text:
                filtered.append(el)

        logger.info(f"🔍 Smart DOM Filter: {len(dom_context)} -> {len(filtered)} elements ({len(dom_context) - len(filtered)} decorative removed)")
        return filtered

    def _dom_fingerprint(self, dom_context: List[Dict[str, Any]], limit: int = 30) -> str:
        """
        Lightweight DOM fingerprint for fast comparison.
        Captures key element IDs + text for validator prompt.
        """
        parts = []
        for el in dom_context[:limit]:
            el_type = el.get("type", "div")
            el_id = el.get("id", "")
            text = el.get("text", "").strip()[:40]
            if el_id or text:
                parts.append(f"{el_type}:{el_id or '-'}:{text}")
        return "\n".join(parts)

    async def _run_fast_validate(self, last_action: str, pre_dom: List[Dict[str, Any]], post_dom: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Fast Validator: Compare pre/post DOM to check if the last action worked.
        Uses the fast LLM model for <500ms response.
        Returns: {"success": bool, "reason": str, "changes_detected": str}
        """
        pre_fingerprint = self._dom_fingerprint(pre_dom)
        post_fingerprint = self._dom_fingerprint(post_dom)

        # Quick hash check: if fingerprints are identical, skip LLM call
        if pre_fingerprint == post_fingerprint:
            logger.info("⚡ Fast Validate: DOM identical (hash match) -> FAILED")
            return {"success": False, "reason": "DOM unchanged after action", "changes_detected": "none"}

        prompt = FAST_VALIDATOR_PROMPT.format(
            last_action=last_action,
            pre_dom=pre_fingerprint,
            post_dom=post_fingerprint
        )

        try:
            response = await self.groq.generate(
                prompt,
                stream=False,
                model=self.llm_model,
                temperature=0,
                max_tokens=256,
                response_format={"type": "json_object"}
            )
            result = json.loads(response)
            status = "PASSED" if result.get("success") else "FAILED"
            logger.info(f"✅ Fast Validate: {status} - {result.get('reason', 'N/A')}")
            return result
        except Exception as e:
            logger.warning(f"Fast validation failed: {e} - assuming success")
            return {"success": True, "reason": "Validation error (assumed pass)", "changes_detected": "unknown"}

    async def _run_fast_sense(self, goal: str, dom_context: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Fast Sense: Quick DOM scan + speech generation.
        Returns: {"speech": str, "relevant_ids": [str]}
        """
        dom_str = self._get_compact_dom(dom_context, limit=100)

        prompt = FAST_FILTER_PROMPT.format(goal=goal, dom_context=dom_str)

        try:
            response = await self.groq.generate(
                prompt,
                stream=False,
                model=self.llm_model,
                temperature=0,
                max_tokens=512,
                response_format={"type": "json_object"}
            )
            result = json.loads(response)
            ids = result.get("relevant_ids", [])
            logger.info(f"⚡ Fast Sense: {len(ids)} relevant IDs identified, speech: \"{result.get('speech', '')}\"")
            return result
        except Exception as e:
            logger.warning(f"Fast sense failed: {e}")
            return {"speech": "", "relevant_ids": []}

    async def orchestrate(self, 
                          query: str, 
                          dom_context: List[Dict[str, Any]], 
                          history: str = "",
                          language: str = "en") -> AsyncGenerator[Dict[str, Any], None]:
        """Runs Voice and Action streams in parallel and yields combined results"""
        
        queue = asyncio.Queue()

        async def producer(gen_func, *args, **kwargs):
            try:
                # Call the generator function to get the generator object
                logger.info(f"🚀 Starting producer for {gen_func.__name__}")
                async for item in gen_func(*args, **kwargs):
                    logger.debug(f"📤 Producer {gen_func.__name__} yielded: {item}")
                    await queue.put(item)
                logger.info(f"✅ Finished producer for {gen_func.__name__}")
            except Exception as e:
                logger.error(f"❌ Error in producer {gen_func.__name__}: {e}", exc_info=True)

        # Start producers as tasks
        # Fetch initial map hints and determine mode ONCE at the start
        map_hints = ""
        mission_mode = "EXPLORER (Mapping new territory)"
        
        if self.qdrant and dom_context:
            # Check domain match for the first interaction
            from urllib.parse import urlparse
            # We don't have URL here, but we can infer from session or wait for first plan
            # For orchestrate, it's called with current URL usually but let's check signature
            pass

        # Since orchestrate doesn't have current_url, let's assume it's explorer unless hints found
        if self.qdrant:
            map_hints = await self.get_navigation_hints(query, "demo")
            if map_hints:
                mission_mode = "MIND (Using pre-indexed site map)"

        t1 = asyncio.create_task(producer(self._run_voice_stream, query, language, dom_context, map_hints, mission_mode))
        t2 = asyncio.create_task(producer(self._run_action_generic, query, dom_context, history))
        
        # Wait until both are done
        pending = {t1, t2}
        while pending:
            # Yield everything currently in the queue
            while not queue.empty():
                item = await queue.get()
                yield item
            
            # Brief wait for more items or task completion
            done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
            
        # Final empty check to catch any items added right before tasks finished
        while not queue.empty():
            yield await queue.get()

    async def _run_voice_stream(self, query: str, language: str, dom_context: list = None, map_hints: str = "", mission_mode: str = "EXPLORER") -> AsyncGenerator[Dict[str, Any], None]:
        """Generates conversational acknowledgement"""
        """Generates conversational acknowledgement"""
        ui_context = ""
    
        # Extract Title or Main Heading for high-level context
        if dom_context:
            titles = [el.get("text", "") for el in dom_context if el.get("type") in ["title", "h1", "header"]]
            if titles:
                ui_context = titles[0]  # Grab the first robust heading
            else:
                # Fallback to domain-like text or just the first few relevant words
                ui_context = ", ".join([el.get("text", "")[:20] for el in dom_context[:3] if el.get("text")])
            
        prompt = VOICE_STREAM_PROMPT.format(
            query=query, 
            ui_context=ui_context or "Loading page...", 
            map_hints=map_hints or "Standard exploration.",
            mission_mode=mission_mode
        )
        full_text = ""
        try:
            # Use LLM_MODEL for voice stream (fast response)
            async for chunk in self.groq.generate(prompt, stream=True, model=self.llm_model, temperature=0.5):
                full_text += chunk
                yield {"type": "voice", "content": chunk}
            
            logger.info(f"🗣️ Voice Response Generated: \"{full_text}\"")
        except Exception as e:
            logger.error(f"Voice stream failed: {e}")

    async def _run_action_generic(self, query: str, dom_context: List[Dict[str, Any]], history: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Generates visual action"""
        # Optimized DOM representation for token savings
        dom_str = self._get_compact_dom(dom_context, limit=300)
        
        logger.info(f"🧠 Reasoning on DOM Context ({len(dom_context)} elements)...")
        if dom_context:
            logger.debug(f"📄 DOM Snippet for LLM: {dom_str[:500]}...")

        # logger.debug(f"📝 ACTION SYSTEM PROMPT SNIPPET: {prompt[:500]}...")
        
        try:
            # Use ANALYTICS_MODEL for action reasoning with higher token limit for JSON stability
            response = await self.groq.generate(
                prompt, 
                stream=False, 
                model=self.analytics_model, 
                temperature=0, 
                max_tokens=2048,
                response_format={"type": "json_object"}
            )
            logger.debug(f"🤖 Raw Action LLM Response: {response}")
            
            # Parse JSON from response
            try:
                # Find JSON block
                start = response.find("{")
                end = response.rfind("}") + 1
                if start != -1 and end != -1:
                    action_json = response[start:end]
                    action = json.loads(action_json)
                    
                    # NORMALIZE: Ensure action fields are never null (use empty strings instead)
                    if isinstance(action, dict):
                        for key in ["target_id", "text", "url"]:
                            if action.get(key) is None:
                                action[key] = ""
                    
                    if action.get("type") != "none":
                        logger.info(f"🎬 Action Parsed: {json.dumps(action, indent=2)}")
                        yield {"type": "action", "payload": action}
                    else:
                        logger.info("🎬 Action Parsed: NO_ACTION")
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse action JSON: {response}")
        except Exception as e:
            logger.error(f"Action stream failed: {e}")

    async def plan_next_step(self, goal: str, dom_context: list, step_number: int, warning_message: str = "", current_url: str = "", last_action: str = "", map_hints: str = "", client_id: str = "demo", action_history: list = None, dom_diff: str = "", conversation_history: str = "", last_dom_context: list = None) -> dict:
        """
        Determines the next action in a mission loop.
        Pipeline: Validate -> Smart Filter -> Fast Sense -> Plan
        """
        plan_start = time.time()

        # ── PHASE 0: Fast Validation (compare pre/post DOM) ──────────────
        validation_warning = ""
        if last_dom_context and last_action and step_number > 0:
            try:
                validation = await self._run_fast_validate(last_action, last_dom_context, dom_context)
                if not validation.get("success", True):
                    validation_warning = (
                        f"VALIDATION FAILED: Last action '{last_action}' did NOT work. "
                        f"Reason: {validation.get('reason', 'unknown')}. "
                        f"Changes: {validation.get('changes_detected', 'none')}. "
                        f"You MUST try a different approach."
                    )
                    logger.warning(f"⚠️ {validation_warning}")
            except Exception as e:
                logger.warning(f"Validation phase error: {e}")

        # Merge validation warning with any existing warning
        combined_warning = warning_message or ""
        if validation_warning:
            combined_warning = f"{validation_warning} | {combined_warning}" if combined_warning else validation_warning

        # ── PHASE 1: Smart DOM Filter (reduce tokens) ────────────────────
        filtered_dom = self._smart_dom_filter(dom_context)
        dom_str = self._get_compact_dom(filtered_dom, limit=200)

        # Format action history for prompt
        action_history_str = ", ".join(action_history) if action_history else "(None yet)"

        # STATIC: map_hints are PRE-FETCHED at mission start (Step 0)
        # DYNAMIC: step_context is queried EVERY step based on current DOM
        step_context = ""
        if self.use_hivemind:
            step_context = await self.get_step_context(dom_context, current_url, client_id)

        # Determine Mission Mode based on HiveMind match
        mission_mode = "EXPLORER (Mapping new territory)"
        if self.qdrant and self.use_hivemind:
            status = await self.check_hivemind_status(current_url, client_id)
            if status.get("mode") == "mapped":
                mission_mode = "MIND (Using pre-indexed site map)"

        prompt = NEXT_STEP_PROMPT.format(
            goal=goal,
            mission_mode=mission_mode,
            step_number=step_number,
            conversation_history=conversation_history or "(First interaction - no prior context)",
            warning_message=combined_warning or "None",
            map_hints=map_hints or "No pre-indexed hints available.",
            step_context=step_context or "No element-specific context available.",
            current_url=current_url or "Unknown",
            last_action=last_action or "(First step)",
            action_history=action_history_str,
            dom_diff=dom_diff or "First step (Recent history unavailable).",
            dom_context=dom_str
        )

        logger.info(f"🧠 Planning next step (Goal: {goal}, Step: {step_number}, URL: {current_url}, DOM: {len(dom_context)}->{len(filtered_dom)} elements)...")
        if map_hints:
            logger.info(f"🗺️ Static Map Hints: {map_hints[:80]}...")
        if step_context:
            logger.info(f"🔍 Dynamic Step Context: {step_context[:80]}...")

        try:
            # Use ANALYTICS_MODEL for strategic reasoning with higher token limit
            # Optimized for Reasoning Models (DeepSeek-R1 / GPT-OSS)
            response = await self.groq.generate(
                prompt,
                stream=False,
                model=self.analytics_model,
                temperature=0.6,
                max_tokens=2048,
                response_format={"type": "json_object"},
                include_reasoning=True,
                reasoning_effort="high"
            )

            logger.debug(f"🤖 RAW PLANNER RESPONSE: {response}")

            plan = json.loads(response)

            # NORMALIZE: Ensure action fields are never null (use empty strings instead)
            action = plan.get("action", {})
            if isinstance(action, dict):
                # Ensure speech is in action for frontend streaming
                if "speech" in plan and "speech" not in action:
                    action["speech"] = plan["speech"]

                for key in ["target_id", "text", "url", "speech"]:
                    if action.get(key) is None:
                        action[key] = ""

            # Inject validation metadata
            plan["_validation"] = {
                "ran": bool(last_dom_context and last_action and step_number > 0),
                "passed": not bool(validation_warning),
            }
            plan["_timing_ms"] = int((time.time() - plan_start) * 1000)

            logger.info(f"✅ PARSED PLAN ({plan['_timing_ms']}ms): {json.dumps(plan, indent=2)}")
            return plan
        except Exception as e:
            logger.error(f"Planning failed: {e}")
            raise

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

    async def get_navigation_hints(self, goal: str, client_id: str) -> str:
        """
        Query Qdrant for pre-indexed site navigation hints (STATIC - called once at Step 0).
        Returns a string like: "HINT: To find villas, navigate to /search. Key elements: #search-bar, .filter-price"
        """
        if not self.qdrant or not hasattr(self.qdrant, 'enabled') or not self.qdrant.enabled:
            return ""
        
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            
            if not self.embeddings:
                return ""
                
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
                limit=1
            )
            hits = hits_response.points
            
            if hits:
                payload = hits[0].payload
                url = payload.get("url", "")
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

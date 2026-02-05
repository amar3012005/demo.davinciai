import asyncio
import json
import logging
import time
from typing import Dict, Any, List, Optional, AsyncGenerator

from llm_providers.groq_provider import GroqProvider

logger = logging.getLogger(__name__)

VOICE_STREAM_PROMPT = """
<system_configuration>
You are TARA, an expressive Visual Co-Pilot helping users navigate websites.
Goal: Warmly acknowledge the user's request and set expectations.

Context: The user just asked you to help with something on a webpage.

Tone: Helpful, warm, conversational (like a friendly guide sitting next to them).

Rules:
1. Acknowledge the goal explicitly.
2. Mention specific UI elements you see if relevant.
3. Be natural and human-like (2-3 sentences max).
4. Set expectations for what you're about to do.

Examples:
- User: "Find the pricing page" → "I can help with that. I see the pricing link in the main navigation, let me take you there."
- User: "Show me villas in Hamburg" → "Sure thing! Let me navigate to the property search and filter for villas in Hamburg."
- User: "Find docs about prompt caching" → "Absolutely. I'll look for the prompt caching documentation in the sidebar."
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
{{"type": "type_text", "target_id": "id", "text": "hello"}}
</system_configuration>

Respond in json format.

Visible Elements:
{dom_context}

User Query: {query}
History: {history}
"""

NEXT_STEP_PROMPT = """
<system_configuration>
You are TARA, an expressive Visual Co-Pilot helping users navigate websites.
Your job: Determine the NEXT action AND narrate what you're doing (like a human guide).

DECISION PROTOCOL (SELF-AWARE SKEPTICISM):
1. **VALIDATE MAP HINT FIRST**: Does the provided Map Hint actually help achieve the Goal?
   - If IRRELEVANT or NONSENSICAL → IGNORE IT.
   - If hint URL contains random IDs, image paths, or unrelated content → REJECT IT.
   - Only trust hints that make SEMANTIC SENSE for the goal.

2. Am I on the right page for this goal?
   - If NO and Map Hint is VALIDATED → navigate to suggested URL
   - If NO and Map Hint is REJECTED → look for navigation elements on current screen

3. What is the next physical action?
4. If goal is ACHIEVED → output "none" with summary
5. If page is LOADING → output "wait"

CONFIDENCE ASSESSMENT:
- "high": Action clearly moves toward goal
- "medium": Might help but uncertain
- "low": Unsure which element to click, or confused by page structure

NARRATION (CRITICAL - SPEAK LIKE A HUMAN):
- Generate a short, casual sentence explaining THIS STEP to the user.
- NEVER spell out raw element IDs, CSS classes, or technical button names.
- Instead, describe what the element DOES or MEANS contextually.
  BAD:  "Clicking on btn-cta-pricing-monthly"
  GOOD: "Let me click on the monthly pricing option for you."
  BAD:  "Clicking tara-nav-item-docs"
  GOOD: "Opening the documentation section."
- Be warm and conversational, like you're sitting next to them.

OUTPUT SCHEMA (json):
{{
  "reasoning": "Internal thought process (not shown to user)",
  "confidence": "high|medium|low",
  "speech": "What to say to the user during this step (conversational, 1-2 sentences)",
  "action": {{"type": "click|navigate|wait|none", "target_id": "...", ...}}
}}

RULES:
- **Output ONLY valid json** (this is required).
- ALWAYS include "speech" field with CONTEXTUAL narration (no raw IDs).
- Prefer element IDs over text for action targeting. But narrate MEANINGFULLY.
- NEVER invent elements. Only use what you SEE in the DOM.
- AVOID LOOPS: Check ACTION_HISTORY - if you already did an action, DO NOT repeat it.
- GOAL COMPLETION: If screen shows success message, confirmation, or goal is achieved → output type "none" immediately.

</system_configuration>

Respond in json format.

GOAL: "{goal}"
STEP: {step_number} / 10
LAST ACTION: {last_action}
ACTION_HISTORY (do NOT repeat these): {action_history}
WARNINGS: {warning_message}

KNOWN MAP HINTS: {map_hints}
ELEMENT CONTEXT: {step_context}
CURRENT URL: {current_url}

CURRENT SCREEN STATE (Visible Elements):
{dom_context}
"""


class VisualOrchestrator:
    """Manages dual-stream generation for Visual Co-Pilot"""
    
    def __init__(self, groq_provider: GroqProvider, qdrant_client=None, embeddings=None):
        self.groq = groq_provider
        self.qdrant = qdrant_client  # Optional Qdrant for map hints
        self.embeddings = embeddings

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
        # Each task wraps the execution of its respective stream
        t1 = asyncio.create_task(producer(self._run_voice_stream, query, language))
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

    async def _run_voice_stream(self, query: str, language: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Generates conversational acknowledgement"""
        prompt = VOICE_STREAM_PROMPT.format(query=query)
        full_text = ""
        try:
            # Use llama-3-8b-8192 for ultra-fast response
            async for chunk in self.groq.generate(prompt, stream=True, model="llama-3.1-8b-instant", temperature=0.5):
                full_text += chunk
                yield {"type": "voice", "content": chunk}
            
            logger.info(f"🗣️ Voice Response Generated: \"{full_text}\"")
        except Exception as e:
            logger.error(f"Voice stream failed: {e}")

    async def _run_action_generic(self, query: str, dom_context: List[Dict[str, Any]], history: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Generates visual action"""
        # Truncate dom_context to fit in context window if needed
        dom_str = json.dumps(dom_context[:300], indent=2) # Matching orchestrator limit
        
        logger.info(f"🧠 Reasoning on DOM Context ({len(dom_context)} elements)...")
        if dom_context:
            logger.debug(f"📄 DOM Snippet for LLM: {dom_str[:500]}...")

        prompt = ACTION_STREAM_PROMPT.format(query=query, dom_context=dom_str, history=history)
        
        logger.info(f"📝 ACTION SYSTEM PROMPT SNIPPET: {prompt[:500]}...")
        
        try:
            # Use 70b and enforce JSON mode
            response = await self.groq.generate(prompt, stream=False, model="llama-3.3-70b-versatile", temperature=0, response_format={"type": "json_object"})
            logger.info(f"🤖 Raw Action LLM Response: {response}")
            
            # Parse JSON from response
            try:
                # Find JSON block
                start = response.find("{")
                end = response.rfind("}") + 1
                if start != -1 and end != -1:
                    action_json = response[start:end]
                    action = json.loads(action_json)
                    if action.get("type") != "none":
                        logger.info(f"🎬 Action Parsed: {json.dumps(action, indent=2)}")
                        yield {"type": "action", "payload": action}
                    else:
                        logger.info("🎬 Action Parsed: NO_ACTION")
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse action JSON: {response}")
        except Exception as e:
            logger.error(f"Action stream failed: {e}")

    async def plan_next_step(self, goal: str, dom_context: list, step_number: int, warning_message: str = "", current_url: str = "", last_action: str = "", map_hints: str = "", client_id: str = "demo", action_history: list = None) -> dict:
        """Determines the next action in a mission loop with optional map hints."""
        # Truncate DOM context
        dom_str = json.dumps(dom_context[:300], indent=2)
        
        # Format action history for prompt
        action_history_str = ", ".join(action_history) if action_history else "(None yet)"
        
        # STATIC: map_hints are PRE-FETCHED at mission start (Step 0)
        # DYNAMIC: step_context is queried EVERY step based on current DOM
        step_context = await self.get_step_context(dom_context, current_url, client_id)
        
        prompt = NEXT_STEP_PROMPT.format(
            goal=goal,
            step_number=step_number,
            warning_message=warning_message or "None",
            map_hints=map_hints or "No pre-indexed hints available.",
            step_context=step_context or "No element-specific context available.",
            current_url=current_url or "Unknown",
            last_action=last_action or "(First step)",
            action_history=action_history_str,
            dom_context=dom_str
        )

        
        logger.info(f"📝 PLANNING PROMPT SNIPPET: {prompt[:1000]}...")
        
        logger.info(f"🧠 Planning next step (Goal: {goal}, Step: {step_number}, URL: {current_url})...")
        if map_hints:
            logger.info(f"🗺️ Static Map Hints: {map_hints[:80]}...")
        if step_context:
            logger.info(f"🔍 Dynamic Step Context: {step_context[:80]}...")
        
        try:
            # Use 70b-versatile for reasoning
            response = await self.groq.generate(
                prompt, 
                stream=False, 
                model="llama-3.3-70b-versatile", 
                temperature=0, 
                response_format={"type": "json_object"}
            )
            
            logger.info(f"🤖 RAW PLANNER RESPONSE: {response}")
            
            plan = json.loads(response)
            logger.info(f"✅ PARSED PLAN: {json.dumps(plan, indent=2)}")
            return plan
        except Exception as e:
            logger.error(f"Planning failed: {e}")
            raise

    async def get_navigation_hints(self, goal: str, client_id: str) -> str:
        """
        Query Qdrant for pre-indexed site navigation hints (STATIC - called once at Step 0).
        Returns a string like: "HINT: To find villas, navigate to /search. Key elements: #search-bar, .filter-price"
        """
        if not self.qdrant or not hasattr(self.qdrant, 'enabled') or not self.qdrant.enabled:
            return ""
        
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            
            # Search for navigation hints in the client's sitemap collection
            if not self.embeddings:
                return ""
                
            hits_response = await self.qdrant.client.query_points(
                collection_name=self.qdrant.collection_name,
                query=self.embeddings.embed_query(goal),
                query_filter=Filter(
                    must=[
                        FieldCondition(key="label", match=MatchValue(value="website_sitemap")),
                        FieldCondition(key="client_id", match=MatchValue(value=client_id))
                    ]
                ),
                limit=1
            )
            hits = hits_response.points
            
            if hits:
                payload = hits[0].payload
                url = payload.get("url", "")
                selectors = payload.get("key_selectors", [])
                concept = payload.get("concept", "")
                
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
        
        Examples:
        - "Button #submit-form submits the booking request"
        - "The filter dropdown allows filtering by price range"
        """
        if not self.qdrant or not hasattr(self.qdrant, 'enabled') or not self.qdrant.enabled:
            return ""
        
        try:
            # Build a query from visible elements (focus on headings, buttons, labels)
            key_elements = []
            for el in dom_context[:15]:  # Limit to first 15 elements
                text = el.get("text", "").strip()
                el_type = el.get("type", "")
                if text and len(text) < 50:  # Skip long text
                    if el_type in ["button", "a", "h1", "h2", "h3", "label", "input"]:
                        key_elements.append(text)
            
            if not key_elements:
                return ""
            
            # Create a query from visible elements
            query_text = f"Page: {current_url}. Elements: {', '.join(key_elements[:5])}"
            
            if not self.embeddings:
                return ""

            from qdrant_client.models import Filter, FieldCondition, MatchValue
            
            hits_response = await self.qdrant.client.query_points(
                collection_name=self.qdrant.collection_name,
                query=self.embeddings.embed_query(query_text),
                query_filter=Filter(
                    must=[
                        FieldCondition(key="label", match=MatchValue(value="element_context")),
                        FieldCondition(key="client_id", match=MatchValue(value=client_id))
                    ]
                ),
                limit=2,
                score_threshold=0.5  # Only high-confidence matches
            )
            hits = hits_response.points
            
            if hits:
                contexts = []
                for hit in hits:
                    ctx = hit.payload.get("context", "")
                    if ctx:
                        contexts.append(ctx)
                if contexts:
                    return "ELEMENT CONTEXT: " + " | ".join(contexts)
            
            return ""
        except Exception as e:
            logger.warning(f"Qdrant step context lookup failed: {e}")
            return ""

"""
mind_reader.py

PURPOSE: Translates messy human input (voice/text) into strict TacticalSchema.
         First barrier between user chaos and system logic. Uses fast LLM
         (Llama-3.1-8B) for quick intent parsing.

DEPENDENCIES:
    - json: For parsing LLM JSON responses
    - logging: For debug output
    - re: For input sanitization
    - time: For timestamps
    - tara_models: TacticalSchema, ActionIntent for output
    - llm_providers.groq_provider: GroqProvider for LLM calls

USED BY:
    - visual_orchestrator.py: translate() called on user input
    - mission_brain.py: Receives TacticalSchema for mission creation

MIGRATION STATUS: [NEW] - Intelligence layer for Ultimate TARA

ERROR HANDLING:
    - LLM timeout → Falls back to heuristic-based schema
    - JSON parse error → Returns safe default schema
    - Invalid action → Defaults to NAVIGATION

Example:
    from mind_reader import MindReader
    from llm_providers.groq_provider import GroqProvider
    
    groq = GroqProvider(api_key="...")
    mind_reader = MindReader(groq)
    
    schema = await mind_reader.translate(
        user_input="Buy a white shirt size medium",
        current_url="https://shop.com/clothing"
    )
    print(schema.action)  # ActionIntent.PURCHASE
    print(schema.missing_constraints())  # []
"""

import json
import logging
import re
import time
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from tara_models import TacticalSchema, ActionIntent

# Import GroqProvider - handle both possible import paths
# Note: We use a broad exception to catch Python version compatibility issues
GroqProvider = None
try:
    from llm_providers.groq_provider import GroqProvider
except Exception as e:
    # Catch all exceptions including TypeError from Python version issues
    logger.warning(f"Could not import GroqProvider (this is OK for fallback mode): {type(e).__name__}")
    GroqProvider = None


class MindReader:
    """
    Input sanitizer and schema generator.
    Uses fast LLM (Llama-3-8B) to structure user intent.
    
    Attributes:
        llm: LLM provider instance (GroqProvider)
    
    Example:
        mind_reader = MindReader(groq_provider)
        schema = await mind_reader.translate("Find my API usage")
    """

    TRANSLATION_PROMPT = """You are an intent parser for a browser co-pilot. Convert the user's request into a structured schema.

USER INPUT: "{user_input}"
CURRENT URL: {current_url}
CURRENT DOMAIN: {domain}

Extract:
1. ACTION: What type of task?
2. TARGET ENTITY: What specific thing is the user asking about? 
3. CONSTRAINTS: Any filters or requirements mentioned? (color, size, date range, etc.)

ACTION DEFINITIONS (choose carefully):
- "extraction": User wants to VIEW, READ, or CHECK existing data/stats/info on the page (usage, billing, account info, metrics, status, history, token counts). Keywords: show me, check, what is, how much, my usage, my stats, tell me.
- "navigation": User wants to GO TO a specific page or section. Keywords: go to, open, navigate, take me to.
- "search": User wants to SEARCH for products/items by typing into a search box. Keywords: search for, find products, look for items, filter by.
- "purchase": User wants to BUY or ADD TO CART. Keywords: buy, purchase, order, add to cart.
- "interaction": User wants to CLICK, TOGGLE, or interact with a specific UI element. Keywords: click, press, toggle, select, enable, disable.

CRITICAL RULES:
- "show me my X" or "show me X data/usage/stats" → extraction (NOT search)
- "show me X products" or "find X items" → search
- PRESERVE_ALL_IDENTIFIERS: Keep ALL model names, product names, API names, brand names, versions (e.g., "whisper large", "llama-3-70b", "GPT-4", "iPhone 15 Pro")
- NEVER simplify target_entity by removing specific identifiers - it must match the user's exact terminology
- If a constraint is mentioned but no value given, mark it as null
- Be conservative: only extract constraints that are explicitly mentioned

OUTPUT FORMAT (JSON only, no extra text):
{{
  "action": "extraction|navigation|search|purchase|interaction",
  "target_entity": "string - EXACT specificity from user input, preserve all model/product names",
  "domain": "example.com",
  "constraints": {{
    "key": "value or null"
  }}
}}

EXAMPLES - FOLLOW THESE EXACTLY:

User: "my whisper large model token usage, show me"
Output: {{"action": "extraction", "target_entity": "whisper large model token usage", "domain": "current", "constraints": {{}}}}

User: "check my Llama 3 70B API usage"
Output: {{"action": "extraction", "target_entity": "Llama 3 70B API usage", "domain": "current", "constraints": {{}}}}

User: "Show me white T-shirts"
Output: {{"action": "search", "target_entity": "white T-shirt", "domain": "current", "constraints": {{"color": "white", "size": null}}}}

User: "Buy a medium shirt"
Output: {{"action": "purchase", "target_entity": "shirt", "domain": "current", "constraints": {{"size": "medium", "color": null}}}}

User: "check my API usage and billing"
Output: {{"action": "extraction", "target_entity": "API usage and billing", "domain": "current", "constraints": {{}}}}

User: "Go to settings page"
Output: {{"action": "navigation", "target_entity": "settings page", "domain": "current", "constraints": {{}}}}

User: "how many tokens have I used today"
Output: {{"action": "extraction", "target_entity": "token usage today", "domain": "current", "constraints": {{"date_range": "today"}}}}

User: "show me GPT-4 usage stats"
Output: {{"action": "extraction", "target_entity": "GPT-4 usage stats", "domain": "current", "constraints": {{}}}}

User: "find Mixtral 8x7B pricing"
Output: {{"action": "search", "target_entity": "Mixtral 8x7B pricing", "domain": "current", "constraints": {{}}}}
"""

    def __init__(self, llm_provider: Any):
        """
        Initialize MindReader with LLM provider.
        
        Args:
            llm_provider: Instance of GroqProvider or similar LLM provider
        
        Raises:
            ValueError: If llm_provider is None
        """
        if llm_provider is None:
            logger.warning("MindReader initialized without LLM provider - will use fallback only")
        
        self.llm = llm_provider
        logger.info("🧠 MindReader initialized")

    async def translate(
        self,
        user_input: str,
        current_url: str = "",
        context: Optional[Dict[str, Any]] = None
    ) -> TacticalSchema:
        """
        Convert raw input to TacticalSchema.
        
        Args:
            user_input: Raw voice/text from user
            current_url: Current browser URL for context
            context: Optional additional context (conversation history, etc.)
        
        Returns:
            TacticalSchema with structured intent
        
        Example:
            schema = await mind_reader.translate(
                user_input="Buy a white shirt",
                current_url="https://zalando.com"
            )
            print(schema.action)  # ActionIntent.PURCHASE
            print(schema.constraints)  # {"color": "white", "size": null}
        """
        # 1. Sanitize input
        cleaned = self._sanitize_input(user_input)
        
        # 2. Extract domain
        domain = self._extract_domain(current_url)
        
        # 3. Build prompt
        prompt = self.TRANSLATION_PROMPT.format(
            user_input=cleaned,
            current_url=current_url or "unknown",
            domain=domain
        )
        
        # 4. Call LLM (fast model)
        try:
            if self.llm is None:
                raise ValueError("No LLM provider available")
            
            response = await self.llm.generate(
                prompt,
                model="llama-3.1-8b-instant",  # Fast model for intent parsing
                temperature=0.1,  # Low temperature for consistent output
                max_tokens=300,
                response_format={"type": "json_object"}
            )
            
            # 5. Parse response
            data = self._parse_llm_response(response)
            
            # 6. Validate and construct schema
            schema = self._build_schema(data, user_input, domain)
            
            logger.info(
                f"🧠 Mind Reader: '{user_input}' → "
                f"{schema.action.value} on '{schema.target_entity}' "
                f"(missing: {schema.missing_constraints()})"
            )
            
            return schema
            
        except Exception as e:
            logger.error(f"Mind Reader LLM call failed: {e}")
            # Fallback: simple heuristic-based schema
            return self._fallback_schema(user_input, domain)

    def _parse_llm_response(self, response: str) -> Dict[str, Any]:
        """
        Parse LLM JSON response with error handling.
        
        Args:
            response: Raw LLM response string
        
        Returns:
            Parsed JSON dictionary
        
        Raises:
            json.JSONDecodeError: If response is not valid JSON
        """
        # Try to extract JSON from response (in case there's extra text)
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            response = json_match.group()
        
        return json.loads(response)

    def _build_schema(
        self,
        data: Dict[str, Any],
        raw_input: str,
        default_domain: str
    ) -> TacticalSchema:
        """
        Build TacticalSchema from LLM response data.
        
        Args:
            data: Parsed JSON from LLM
            raw_input: Original user input
            default_domain: Domain to use if not specified
        
        Returns:
            Validated TacticalSchema
        """
        # Extract and validate action
        action_str = data.get('action', 'navigation').lower()
        try:
            action = ActionIntent(action_str)
        except ValueError:
            logger.warning(f"Invalid action '{action_str}', defaulting to NAVIGATION")
            action = ActionIntent.NAVIGATION
        
        # Extract domain (use "current" as placeholder if not specified)
        domain = data.get('domain', default_domain)
        if domain == "current" or not domain:
            domain = default_domain
        
        # Build constraints dict
        constraints = data.get('constraints', {})
        if not isinstance(constraints, dict):
            constraints = {}
        
        # Get target entity from LLM
        target_entity = data.get('target_entity', '') or ''
        
        # POST-PROCESSING: Ensure critical identifiers are preserved
        target_entity = self._preserve_identifiers(target_entity, raw_input)
        
        return TacticalSchema(
            action=action,
            target_entity=target_entity,
            domain=domain,
            constraints=constraints,
            raw_utterance=raw_input,
            timestamp=time.time()
        )
    
    def _preserve_identifiers(self, target_entity: str, raw_input: str) -> str:
        """
        Ensure critical model/product/API identifiers from raw input are preserved.
        This catches cases where the LLM over-simplifies the target entity.
        
        Args:
            target_entity: LLM-extracted target entity
            raw_input: Original user input
        
        Returns:
            Target entity with preserved identifiers
        
        Examples:
            "model token usage" + "my whisper large model token usage" → "whisper large model token usage"
            "API usage" + "my Llama 3 70B API usage" → "Llama 3 70B API usage"
        """
        import re
        
        # List of known model/API identifiers to check for
        identifiers = [
            # Groq models
            r'whisper\s+(?:large(?:-v\d+)?|medium|small|tiny)',
            r'llama[-\s]?3[-\s]?\d+b(?:-instruct)?',
            r'llama[-\s]?\d+\.\d+[-\s]?\d+b',
            r'mixtral[-\s]?\d+x\d+b',
            r'gemma[-\s]?\d+b',
            # OpenAI models
            r'gpt[-\s]?\d+[-\s]?(?:turbo|o\d+)?(?:-preview)?',
            # Anthropic models
            r'claude[-\s]?(?:instant)?[-\s]?\d+(?:\.\d+)?',
            # Other common patterns
            r'\bdall[-\s]?e\b',
            r'\bstable[-\s]?diffusion\b',
            # Version patterns (v2, 3.5, etc.)
            r'\bv\d+(?:\.\d+)?\b',
        ]
        
        raw_lower = raw_input.lower()
        entity_lower = target_entity.lower()
        
        # Check if any identifier is in raw input but missing from target entity
        for pattern in identifiers:
            matches_raw = re.findall(pattern, raw_lower, re.IGNORECASE)
            matches_entity = re.findall(pattern, entity_lower, re.IGNORECASE)
            
            if matches_raw and not matches_entity:
                # Identifier present in raw input but missing from target - need to add it
                # Find where to insert it (at the beginning, before generic terms)
                identifier = matches_raw[0]
                logger.debug(f"Preserving identifier '{identifier}' in target_entity")
                
                # If target entity is too generic, prepend the identifier
                generic_terms = ['model', 'api', 'usage', 'stats', 'data', 'info', 'metrics']
                if any(term == entity_lower.strip() or entity_lower.strip().startswith(term + ' ') for term in generic_terms):
                    # Target is just generic term, prepend identifier
                    target_entity = f"{identifier} {target_entity}"
                elif identifier.lower() not in entity_lower:
                    # Insert identifier before the generic noun
                    target_entity = f"{identifier} {target_entity}"
                break
        
        # NEW LOGIC: Catch Capitalized tools/brands (like "Kilo Code")
        proper_nouns = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', raw_input)
        
        entity_lower = target_entity.lower()
        for noun in proper_nouns:
            # Don't preserve common starts of sentences
            if noun.lower() not in entity_lower and noun.lower() not in ['tell', 'show', 'find', 'how', 'what']:
                logger.debug(f"Preserving Proper Noun '{noun}' in target_entity")
                target_entity = f"{noun} {target_entity}"
                
        return target_entity.strip()

    def _sanitize_input(self, text: str) -> str:
        """
        Remove filler words and clean input.
        
        Args:
            text: Raw user input
        
        Returns:
            Cleaned text
        
        Example:
            "Um, like, I want to buy a shirt" → "I want to buy a shirt"
        """
        # Remove common filler words
        fillers = ['um', 'uh', 'like', 'you know', 'basically', 'actually', 'i mean']
        cleaned = text
        
        for filler in fillers:
            # Remove as whole words only
            cleaned = re.sub(rf'\b{filler}\b', '', cleaned, flags=re.IGNORECASE)
        
        # Normalize whitespace
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        
        return cleaned

    def _extract_domain(self, url: str) -> str:
        """
        Extract domain from URL.
        
        Args:
            url: Full URL string (with or without protocol)
        
        Returns:
            Domain name (e.g., "zalando.com")
        
        Example:
            "https://www.zalando.com/shirts" → "zalando.com"
            "example.com" → "example.com"
        """
        if not url:
            return "unknown"
        
        try:
            # Add protocol if missing for proper parsing
            if not url.startswith(('http://', 'https://', 'ftp://')):
                url = 'http://' + url
            
            parsed = urlparse(url)
            domain = parsed.netloc.replace('www.', '')
            return domain if domain else "unknown"
        except Exception:
            logger.warning(f"Failed to parse domain from URL: {url}")
            return "unknown"

    def _fallback_schema(self, user_input: str, domain: str) -> TacticalSchema:
        """
        Simple fallback when LLM fails.
        Uses keyword heuristics to determine intent.
        
        Args:
            user_input: Raw user input
            domain: Extracted domain
        
        Returns:
            TacticalSchema based on heuristics
        """
        input_lower = user_input.lower()
        
        # Action detection based on keywords
        # Order matters: check specific intents before general ones
        
        # Check for extraction FIRST (data, usage, stats are specific)
        if any(kw in input_lower for kw in ['data', 'usage', 'stats', 'metrics', 'account', 'billing', 'api']):
            action = ActionIntent.EXTRACTION
        elif any(kw in input_lower for kw in ['buy', 'purchase', 'order', 'add to cart', 'add to bag']):
            action = ActionIntent.PURCHASE
        elif any(kw in input_lower for kw in ['click', 'select', 'choose', 'tap', 'press']):
            action = ActionIntent.INTERACTION
        elif any(kw in input_lower for kw in ['find', 'show', 'search', 'look for', 'get me']):
            action = ActionIntent.SEARCH
        elif any(kw in input_lower for kw in ['go to', 'navigate', 'open', 'show me', 'take me to']):
            action = ActionIntent.NAVIGATION
        else:
            action = ActionIntent.NAVIGATION  # Default
        
        # Extract simple constraints (color, size keywords)
        constraints = {}
        
        # Color detection
        colors = ['white', 'black', 'red', 'blue', 'green', 'yellow', 'pink', 'purple', 'orange', 'gray', 'grey', 'brown']
        for color in colors:
            if color in input_lower:
                constraints['color'] = color
                break
        
        # Size detection
        sizes = ['small', 'medium', 'large', 'xl', 'xxl', 'xs']
        for size in sizes:
            if size in input_lower:
                constraints['size'] = size
                break
        
        # Extract target entity (simple noun extraction after verb)
        target = user_input
        for kw in ['buy', 'find', 'show', 'search', 'click', 'go to', 'navigate']:
            if kw in input_lower:
                idx = input_lower.find(kw)
                target = user_input[idx + len(kw):].strip()
                # Remove common words
                target = re.sub(r'\b(me|the|a|an|some|to)\b', '', target, flags=re.IGNORECASE).strip()
                break
        
        schema = TacticalSchema(
            action=action,
            target_entity=target[:100] if target else user_input[:100],
            domain=domain,
            constraints=constraints,
            raw_utterance=user_input,
            timestamp=time.time()
        )
        
        logger.info(
            f"🧠 Mind Reader (fallback): '{user_input}' → "
            f"{schema.action.value} on '{schema.target_entity}'"
        )
        
        return schema


# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════

def create_mind_reader(groq_api_key: Optional[str] = None) -> MindReader:
    """
    Factory function to create MindReader instance.
    
    Args:
        groq_api_key: Optional API key (uses env var if not provided)
    
    Returns:
        MindReader instance with GroqProvider
    """
    if GroqProvider is None:
        logger.error("GroqProvider not available - mind_reader will use fallback only")
        return MindReader(None)
    
    try:
        groq = GroqProvider(api_key=groq_api_key)
        return MindReader(groq)
    except Exception as e:
        logger.error(f"Failed to create GroqProvider: {e}")
        return MindReader(None)

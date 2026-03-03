"""
hive_interface.py

PURPOSE: Dual-retrieval system for Qdrant vector database.
         Retrieves BOTH high-level strategy sequences (for planning)
         AND low-level visual hints (for element detection).

DEPENDENCIES:
    - qdrant_client: Async Qdrant client
    - redis.asyncio: For caching query results
    - tara_models: TacticalSchema, StrategyHint, VisualHint, HiveResponse
    - numpy: For embedding operations (optional)

USED BY:
    - mission_brain.py: Gets strategy for sub-goal planning
    - semantic_detective.py: Gets visual hints for element scoring

MIGRATION STATUS: [NEW] - Intelligence layer for Ultimate TARA

QDRANT DOCUMENT SCHEMA:
    Strategy_Sequence:
        - doc_type: "Strategy_Sequence"
        - domain: "groq.com"
        - action: "extraction" | "navigation" | "interaction" | "purchase"
        - sequence: ["Navigate to Dashboard", "Click Usage", "Read data"]
        - constraints_order: ["date_range", "metric_type"]
        - blocking_rules: {"export": ["date_selected"]}
        - example_url: "https://groq.com/dashboard"
        - text: "Strategy for extraction on groq.com: Navigate to Dashboard → Click Usage"

    Visual_Hint:
        - doc_type: "Visual_Hint"
        - domain: "groq.com"
        - entity: "usage export button"
        - selector: "#export-btn" | text pattern
        - element_type: "button" | "link" | "input" | "dropdown"
        - zone: "nav" | "main" | "sidebar" | "modal" | "toolbar"
        - text_pattern: "Export.*"
        - confidence: 0.95

ERROR HANDLING:
    - Qdrant unavailable → Returns None/empty, system degrades gracefully
    - Redis cache failure → Logs warning, queries Qdrant directly
    - Embedding failure → Returns empty response

Example:
    from hive_interface import HiveInterface
    from tara_models import TacticalSchema, ActionIntent
    
    hive = HiveInterface(qdrant_client, embeddings, redis_client)
    
    schema = TacticalSchema(
        action=ActionIntent.EXTRACTION,
        target_entity="API usage data",
        domain="groq.com",
        constraints={"date_range": "last_30_days"}
    )
    
    response = await hive.retrieve(schema)
    print(response.strategy.sequence)  # ["Navigate to Dashboard", ...]
    print(response.visual_hints[0].selector)  # "#usage-export"
"""

import json
import logging
import time
import hashlib
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from tara_models import (
    TacticalSchema, ActionIntent,
    StrategyHint, VisualHint, HiveResponse
)

# Optional imports with graceful fallback
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Filter, FieldCondition, MatchValue, MatchText,
        PointStruct, Range
    )
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantClient = None

try:
    from redis import asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)


class HiveInterface:
    """
    Split-brain memory retriever for TARA.
    
    Query A: Strategy sequences for high-level planning.
    Query B: Visual hints for low-level element detection.
    
    Attributes:
        qdrant: Qdrant client instance
        redis: Redis client for caching (optional)
        embeddings: Embedding model for vector search
        collection: Qdrant collection name
        cache_ttl: Cache TTL in seconds
    """

    def __init__(
        self,
        qdrant_client: Optional[Any] = None,
        embeddings: Optional[Any] = None,
        redis_client: Optional[Any] = None,
        collection_name: str = "tara_hive",
        cache_ttl: int = 300  # 5 minutes cache
    ):
        """
        Initialize HiveInterface.
        
        Args:
            qdrant_client: Qdrant client instance (can be None for fallback)
            embeddings: Embedding model for vector search
            redis_client: Redis client for caching (optional)
            collection_name: Qdrant collection name
            cache_ttl: Cache TTL in seconds
        """
        self.qdrant = qdrant_client
        self.redis = redis_client
        self.embeddings = embeddings
        self.collection = collection_name
        self.cache_ttl = cache_ttl
        
        logger.info(
            f"🧠 HiveInterface initialized: "
            f"Qdrant={QDRANT_AVAILABLE and qdrant_client is not None}, "
            f"Redis={REDIS_AVAILABLE and redis_client is not None}"
        )

    async def is_domain_indexed(self, domain: str) -> bool:
        """Check if a domain has any indexed documents in the Qdrant collection."""
        if not domain or not self.qdrant or not QDRANT_AVAILABLE:
            return False
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            result = await self.qdrant.scroll(
                collection_name=self.collection,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="domain", match=MatchValue(value=domain))
                    ]
                ),
                limit=1,
            )
            points = result[0] if isinstance(result, tuple) else result
            return len(points) > 0
        except Exception as e:
            logger.warning(f"is_domain_indexed check failed for '{domain}': {e}")
            return False

    async def retrieve(
        self,
        schema: TacticalSchema,
        use_cache: bool = True
    ) -> HiveResponse:
        """
        Dual retrieval: Strategy + Visual Hints.
        
        Args:
            schema: TacticalSchema from MindReader
            use_cache: Whether to use Redis cache
        
        Returns:
            HiveResponse with strategy and visual hints
        """
        start_time = time.time()
        
        # Generate cache key
        cache_key = None
        if use_cache and self.redis:
            cache_key = self._get_cache_key(schema)
            cached = await self._get_from_cache(cache_key)
            if cached:
                logger.debug(f"Cache hit for {cache_key}")
                return cached

        # Use asyncio.gather for parallel queries
        import asyncio
        
        # Parallel queries to Qdrant
        strategy, hints = await asyncio.gather(
            self._get_strategy(schema),
            self._get_visual_hints(schema)
        )

        query_time_ms = int((time.time() - start_time) * 1000)

        response = HiveResponse(
            strategy=strategy,
            visual_hints=hints or [],
            cached=False,
            query_time_ms=query_time_ms
        )

        # Cache the response
        if cache_key and self.redis:
            await self._store_in_cache(cache_key, response)

        logger.info(
            f"🧠 Hive: {schema.action.value} on {schema.domain} → "
            f"Strategy: {bool(strategy)}, Hints: {len(hints or [])} ({query_time_ms}ms)"
        )

        return response

    def _get_cache_key(self, schema: TacticalSchema) -> str:
        """Generate Redis cache key from schema."""
        key_data = f"{schema.action.value}:{schema.domain}:{schema.target_entity}"
        key_hash = hashlib.md5(key_data.encode()).hexdigest()[:12]
        return f"hive:cache:{key_hash}"

    async def _get_from_cache(self, cache_key: str) -> Optional[HiveResponse]:
        """Retrieve response from Redis cache."""
        try:
            if not self.redis:
                return None
            
            cached = await self.redis.get(cache_key)
            if cached:
                if isinstance(cached, bytes):
                    cached = cached.decode('utf-8')
                return HiveResponse.from_dict(json.loads(cached))
        except Exception as e:
            logger.warning(f"Cache get failed: {e}")
        return None

    async def _store_in_cache(self, cache_key: str, response: HiveResponse) -> None:
        """Store response in Redis cache."""
        try:
            if not self.redis:
                return
            
            await self.redis.set(
                cache_key,
                json.dumps(response.to_dict()),
                ex=self.cache_ttl
            )
        except Exception as e:
            logger.warning(f"Cache set failed: {e}")

    async def _get_strategy(
        self,
        schema: TacticalSchema
    ) -> Optional[StrategyHint]:
        """
        Query A: High-level navigation strategy.
        Document type: "Strategy_Sequence"
        
        Args:
            schema: TacticalSchema with intent
        
        Returns:
            StrategyHint or None if not found
        """
        if not self.qdrant or not QDRANT_AVAILABLE:
            logger.debug("Qdrant not available, no strategy retrieved")
            return None
        
        try:
            # Build query text for embedding
            query_text = f"strategy for {schema.action.value} {schema.target_entity} on {schema.domain}"
            
            # Get embedding
            vector = await self._embed(query_text) if self.embeddings else None
            
            # Build filter
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="doc_type",
                        match=MatchValue(value="Strategy_Sequence")
                    ),
                    FieldCondition(
                        key="domain",
                        match=MatchValue(value=schema.domain)
                    ),
                ]
            )
            
            # Note: 'action' field has no payload index in tara_hive,
            # so we skip that filter and rely on vector similarity for routing.

            # Query Qdrant
            results = await self._query_qdrant(
                vector=vector,
                query_filter=query_filter,
                limit=1,
                score_threshold=0.6
            )
            
            if not results:
                logger.debug(f"No strategy found for {schema.domain}")
                return None
            
            point = results[0]
            payload = point.get('payload', {}) if isinstance(point, dict) else getattr(point, 'payload', {})
            
            return StrategyHint(
                sequence=payload.get('sequence', []),
                constraints_order=payload.get('constraints_order', []),
                blocking_rules=payload.get('blocking_rules', {}),
                confidence=point.get('score', 0.0) if isinstance(point, dict) else getattr(point, 'score', 0.0),
                source_url=payload.get('example_url', '')
            )
            
        except Exception as e:
            logger.error(f"Strategy query failed: {e}")
            return None

    async def _get_visual_hints(
        self,
        schema: TacticalSchema
    ) -> List[VisualHint]:
        """
        Query B: Search HiveMind with elaborated queries.
        Searches for website data chunks corresponding to user input.
        
        Args:
            schema: TacticalSchema with intent

        Returns:
            List of VisualHint objects
        """
        if not self.qdrant or not QDRANT_AVAILABLE:
            logger.debug("Qdrant not available, no visual hints retrieved")
            return []

        all_hints = []

        try:
            # Build elaborated search queries based on user input
            # This helps find relevant website data chunks in HiveMind
            search_queries = []
            
            # Query 1: Original target entity
            search_queries.append(f"{schema.target_entity}")
            
            # Query 2: Elaborated with action context
            action_context = {
                ActionIntent.SEARCH: "find search locate",
                ActionIntent.NAVIGATION: "navigate go to page",
                ActionIntent.EXTRACTION: "data information show",
                ActionIntent.INTERACTION: "click button select",
                ActionIntent.PURCHASE: "buy cart checkout"
            }
            context = action_context.get(schema.action, "")
            search_queries.append(f"{context} {schema.target_entity} {schema.domain}")
            
            # Query 3: Domain-specific search
            search_queries.append(f"{schema.domain} {schema.target_entity}")
            
            # Query 4: Full elaborated query
            search_queries.append(f"how to {schema.target_entity} on {schema.domain} {schema.raw_utterance}")
            
            # Search with all queries
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="domain",
                        match=MatchValue(value=schema.domain)
                    ),
                ]
            )
            
            # Try text-based search first (for populated website data)
            for query_text in search_queries:
                vector = await self._embed(query_text) if self.embeddings else None
                
                results = await self._query_qdrant(
                    vector=vector or [],
                    query_filter=query_filter,
                    limit=5,
                    score_threshold=0.3  # Lower threshold to find relevant chunks
                )
                
                for point in (results or []):
                    payload = point.get('payload', {}) if isinstance(point, dict) else getattr(point, 'payload', {})
                    score = point.get('score', 0.0) if isinstance(point, dict) else getattr(point, 'score', 0.0)
                    
                    # Extract hints from website data chunks
                    # Look for element selectors, URLs, or navigation paths
                    text_content = payload.get('text', '')
                    
                    # Correctly extract the exact JSON payload fields you pushed
                    selector = payload.get('selector', '')
                    
                    if selector:
                        all_hints.append(VisualHint(
                            selector=selector,
                            element_type=payload.get('element_type', 'link'),
                            text_pattern=payload.get('text_pattern'),
                            zone=payload.get('zone', 'main'),
                            confidence=score
                        ))

            
            # Deduplicate by selector
            seen = set()
            unique_hints = []
            for hint in all_hints:
                hint_key = f"{hint.selector}:{hint.element_type}:{hint.zone}"
                if hint_key not in seen and hint.selector:
                    seen.add(hint_key)
                    unique_hints.append(hint)
            
            logger.info(f"🧠 Hive Visual Hints: Found {len(unique_hints)} hints from {len(search_queries)} queries")
            return unique_hints[:10]  # Limit to top 10
            
        except Exception as e:
            logger.error(f"Visual hints query failed: {e}")
            return []

    async def retrieve_visual_hints_for_queries(
        self,
        schema: TacticalSchema,
        queries: List[str],
        use_cache: bool = True
    ) -> List[VisualHint]:
        """
        Retrieves visual hints for a specific list of queries (e.g., subgoal titles or explicit user intent).
        """
        if not self.qdrant or not QDRANT_AVAILABLE:
            return []

        all_hints = []
        try:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="domain",
                        match=MatchValue(value=schema.domain)
                    ),
                ]
            )
            
            for query_text in queries:
                vector = await self._embed(query_text) if self.embeddings else None
                
                results = await self._query_qdrant(
                    vector=vector or [],
                    query_filter=query_filter,
                    limit=5,
                    score_threshold=0.3
                )
                
                for point in (results or []):
                    payload = point.get('payload', {}) if isinstance(point, dict) else getattr(point, 'payload', {})
                    score = point.get('score', 0.0) if isinstance(point, dict) else getattr(point, 'score', 0.0)
                    
                    selector = payload.get('selector', '')
                    if selector:
                        all_hints.append(VisualHint(
                            selector=selector,
                            element_type=payload.get('element_type', 'link'),
                            text_pattern=payload.get('text_pattern'),
                            zone=payload.get('zone', 'main'),
                            confidence=score
                        ))
            
            seen = set()
            unique_hints = []
            for hint in all_hints:
                hint_key = f"{hint.selector}:{hint.element_type}:{hint.zone}"
                if hint_key not in seen and hint.selector:
                    seen.add(hint_key)
                    unique_hints.append(hint)
            
            return unique_hints[:10]
            
        except Exception as e:
            logger.error(f"Retrieve visual hints for queries failed: {e}")
            return []

    async def retrieve_cross_domain(
        self,
        schema: TacticalSchema
    ) -> Optional['HiveResponse']:
        """
        Cross-Domain Semantic Router.

        When the current domain yields ZERO visual hints, performs a global
        Qdrant search across ALL domains. If the target entity (e.g. 'Kilo Code')
        is found under a DIFFERENT domain (e.g. console.groq.com), it returns
        a HiveResponse with:
          - visual_hints from the correct domain
          - cross_domain_target: the destination domain TARA must bridge to

        This enables the automatic Cross-Domain Bridge without hardcoding.

        Args:
            schema: TacticalSchema whose current domain returned 0 hints.

        Returns:
            HiveResponse with cross_domain_target set, or None.
        """
        if not self.qdrant or not QDRANT_AVAILABLE:
            return None

        logger.info(
            f"🌐 Cross-Domain Search for '{schema.target_entity}' "
            f"(current domain: {schema.domain} had no matches)"
        )

        try:
            import asyncio

            query_text = f"{schema.target_entity} {schema.raw_utterance}"
            vector = await self._embed(query_text) if self.embeddings else None

            # Global search — no domain filter
            results = await self._query_qdrant(
                vector=vector or [],
                query_filter=None,
                limit=10,
                score_threshold=0.35
            )

            if not results:
                logger.info("🌐 Cross-Domain Search: no results globally")
                return None

            # Group results by domain to find the best alternative domain
            domain_scores: Dict[str, float] = {}
            domain_hints: Dict[str, list] = {}

            for point in results:
                payload = (
                    point.get('payload', {})
                    if isinstance(point, dict)
                    else getattr(point, 'payload', {})
                )
                score = (
                    point.get('score', 0.0)
                    if isinstance(point, dict)
                    else getattr(point, 'score', 0.0)
                )
                d = payload.get('domain', '')
                if isinstance(d, str):
                    d = d.strip().lower()
                garbage_domains = {"all", "none", "null", "", "any", "unknown"}
                if d in garbage_domains:
                    continue

                # Skip current domain — we already know it has nothing
                if d == schema.domain or not d:
                    continue

                domain_scores[d] = domain_scores.get(d, 0.0) + score
                if d not in domain_hints:
                    domain_hints[d] = []

                selector = payload.get('selector', '')
                if selector:
                    domain_hints[d].append(VisualHint(
                        selector=selector,
                        element_type=payload.get('element_type', 'link'),
                        text_pattern=payload.get('text_pattern'),
                        zone=payload.get('zone', 'main'),
                        confidence=score
                    ))

            if not domain_scores:
                logger.info("🌐 Cross-Domain Search: no alternative domains found")
                return None

            # Pick domain with highest cumulative score
            best_domain = max(domain_scores, key=domain_scores.__getitem__)
            hints = domain_hints.get(best_domain, [])

            logger.info(
                f"🌐 Cross-Domain Bridge: '{schema.target_entity}' found on "
                f"'{best_domain}' with {len(hints)} hints "
                f"(score: {domain_scores[best_domain]:.2f})"
            )

            return HiveResponse(
                strategy=None,
                visual_hints=hints[:10],
                cached=False,
                query_time_ms=0,
                # Custom field to signal the orchestrator to bridge to this domain
                cross_domain_target=best_domain
            )

        except Exception as e:
            logger.error(f"Cross-domain search failed: {e}")
            return None

    async def _query_qdrant(
        self,
        vector: Optional[List[float]] = None,
        query_filter: Optional[Any] = None,
        limit: int = 5,
        score_threshold: float = 0.5
    ) -> List[Any]:
        """
        Query Qdrant with error handling.
        
        Args:
            vector: Query vector (optional for keyword search)
            query_filter: Qdrant filter
            limit: Max results
            score_threshold: Minimum score

        Returns:
            List of result points
        """
        try:
            if not self.qdrant:
                return []

            # Guard: empty vector causes qdrant to reject the query
            if not vector:
                logger.debug("Qdrant query skipped: empty vector")
                return []

            logger.info(f"🧠 Qdrant query: collection={self.collection}, limit={limit}, threshold={score_threshold}, filter={query_filter}")

            # query_points() is the standard API for qdrant-client >= 1.7
            # It uses 'query=' not 'vector=' (which is the PointStruct field)
            if hasattr(self.qdrant, 'query_points'):
                results = await self.qdrant.query_points(
                    collection_name=self.collection,
                    query=vector,
                    query_filter=query_filter,
                    limit=limit,
                    score_threshold=score_threshold
                )
            elif hasattr(self.qdrant, 'search'):
                # Legacy fallback: search() uses query_vector=
                results = await self.qdrant.search(
                    collection_name=self.collection,
                    query_vector=vector,
                    query_filter=query_filter,
                    limit=limit,
                    score_threshold=score_threshold
                )
            else:
                logger.warning("Qdrant client has no search method")
                return []

            points = results.points if hasattr(results, 'points') else results
            logger.info(f"🧠 Qdrant results: {len(points)} points returned")
            return points

        except Exception as e:
            logger.error(f"Qdrant query failed: {type(e).__name__}: {e}")
            return []

    async def _embed(self, text: str) -> Optional[List[float]]:
        """
        Generate embedding for text.
        
        Args:
            text: Text to embed
        
        Returns:
            Embedding vector or None
        """
        if not self.embeddings:
            return None
        
        try:
            if hasattr(self.embeddings, 'embed_query'):
                return self.embeddings.embed_query(text)
            elif hasattr(self.embeddings, 'encode'):
                return self.embeddings.encode(text).tolist()
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")
        return None

    # ═══════════════════════════════════════════════════════════
    # STORAGE METHODS (for Explorer mode flywheel)
    # ═══════════════════════════════════════════════════════════

    async def store_strategy(
        self,
        domain: str,
        action: str,
        sequence: List[str],
        constraints_order: List[str],
        blocking_rules: Dict[str, List[str]],
        example_url: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Store a new strategy sequence in the Hive.
        Called after successful mission completion (Explorer mode).
        
        Args:
            domain: Website domain
            action: Action type (extraction, navigation, etc.)
            sequence: Step sequence
            constraints_order: Order of constraints
            blocking_rules: Action → required constraints map
            example_url: Example URL this strategy works for
            metadata: Additional metadata
        
        Returns:
            True if stored successfully
        """
        if not self.qdrant or not QDRANT_AVAILABLE:
            return False
        
        try:
            # Build payload
            text = f"Strategy for {action} on {domain}: {' → '.join(sequence)}"
            
            payload = {
                "doc_type": "Strategy_Sequence",
                "domain": domain,
                "action": action,
                "sequence": sequence,
                "constraints_order": constraints_order,
                "blocking_rules": blocking_rules,
                "example_url": example_url,
                "text": text,
                **(metadata or {})
            }
            
            # Generate embedding
            vector = await self._embed(text) if self.embeddings else None
            
            # Create point
            import uuid
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=vector or [],
                payload=payload
            )
            
            # Upsert to Qdrant
            if hasattr(self.qdrant, 'upsert'):
                await self.qdrant.upsert(
                    collection_name=self.collection,
                    points=[point]
                )
            
            logger.info(
                f"💾 Stored strategy: {action} on {domain} "
                f"({len(sequence)} steps)"
            )
            return True
            
        except Exception as e:
            logger.error(f"Store strategy failed: {e}")
            return False

    async def store_visual_hint(
        self,
        domain: str,
        entity: str,
        selector: str,
        element_type: str,
        zone: str = "main",
        text_pattern: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Store a visual hint in the Hive.
        
        Args:
            domain: Website domain
            entity: Entity description (e.g., "export button")
            selector: CSS selector or text pattern
            element_type: Type of element
            zone: Page zone
            text_pattern: Optional text pattern
            metadata: Additional metadata
        
        Returns:
            True if stored successfully
        """
        if not self.qdrant or not QDRANT_AVAILABLE:
            return False
        
        try:
            # Build payload
            text = f"{entity} selector on {domain}: {selector}"
            
            payload = {
                "doc_type": "Visual_Hint",
                "domain": domain,
                "entity": entity,
                "selector": selector,
                "element_type": element_type,
                "zone": zone,
                "text_pattern": text_pattern,
                "text": text,
                **(metadata or {})
            }
            
            # Generate embedding
            vector = await self._embed(text) if self.embeddings else None
            
            # Create point
            import uuid
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=vector or [],
                payload=payload
            )
            
            # Upsert to Qdrant
            if hasattr(self.qdrant, 'upsert'):
                await self.qdrant.upsert(
                    collection_name=self.collection,
                    points=[point]
                )
            
            logger.info(f"💾 Stored visual hint: {entity} → {selector}")
            return True
            
        except Exception as e:
            logger.error(f"Store visual hint failed: {e}")
            return False

    async def store_website_map(
        self,
        domain: str,
        url: str,
        page_type: str,
        key_elements: List[str],
        navigation_paths: List[Dict[str, str]],
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Store a website map (legacy compatibility).
        
        Args:
            domain: Website domain
            url: Page URL
            page_type: Type of page
            key_elements: Key elements on page
            navigation_paths: Navigation paths from this page
            metadata: Additional metadata
        
        Returns:
            True if stored successfully
        """
        if not self.qdrant or not QDRANT_AVAILABLE:
            return False
        
        try:
            text = f"Map of {domain} {page_type} page at {url}"
            
            payload = {
                "doc_type": "Website_Map",
                "domain": domain,
                "url": url,
                "page_type": page_type,
                "key_elements": key_elements,
                "navigation_paths": navigation_paths,
                "text": text,
                **(metadata or {})
            }
            
            vector = await self._embed(text) if self.embeddings else None
            
            import uuid
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=vector or [],
                payload=payload
            )
            
            if hasattr(self.qdrant, 'upsert'):
                await self.qdrant.upsert(
                    collection_name=self.collection,
                    points=[point]
                )
            
            logger.info(f"💾 Stored website map: {url}")
            return True
            
        except Exception as e:
            logger.error(f"Store website map failed: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════

def create_hive_interface(
    qdrant_url: str = "http://localhost:6333",
    redis_url: str = "redis://localhost:6379",
    embeddings: Optional[Any] = None,
    collection_name: str = "tara_hive"
) -> HiveInterface:
    """
    Factory function to create HiveInterface.
    
    Args:
        qdrant_url: Qdrant server URL
        redis_url: Redis server URL
        embeddings: Embedding model
        collection_name: Qdrant collection name
    
    Returns:
        HiveInterface instance
    """
    qdrant_client = None
    redis_client = None
    
    if QDRANT_AVAILABLE:
        try:
            qdrant_client = QdrantClient(url=qdrant_url)
        except Exception as e:
            logger.warning(f"Could not connect to Qdrant: {e}")
    
    if REDIS_AVAILABLE:
        try:
            redis_client = aioredis.from_url(redis_url)
        except Exception as e:
            logger.warning(f"Could not connect to Redis: {e}")
    
    return HiveInterface(
        qdrant_client=qdrant_client,
        embeddings=embeddings,
        redis_client=redis_client,
        collection_name=collection_name
    )

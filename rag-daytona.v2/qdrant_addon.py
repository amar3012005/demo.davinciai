import os
import time
import uuid
import logging
from typing import List, Dict, Optional, Any, Union

# Try to import qdrant_client - graceful degradation if missing
try:
    from qdrant_client import QdrantClient, AsyncQdrantClient
    from qdrant_client.http import models
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

# Universal Payload Schema
from models.hivemind_schema import (
    case_memory_payload, agent_skill_payload, agent_rule_payload,
    website_map_payload, element_context_payload, general_kb_payload,
    read_text, read_summary, read_doc_type, read_created_at,
    SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

class QdrantAddon:
    """
    The 'Memory Add-on' for TARA.
    Handles 'Case Memory' and 'Knowledge Capsules'.
    """
    
    def __init__(self, 
                 embedding_dim: int = 384, 
                 url: Optional[str] = None, 
                 api_key: Optional[str] = None, 
                 collection_name: Optional[str] = None):
        """Initialize Qdrant addon."""
        self.url = url or os.getenv("QDRANT_URL")
        self.api_key = api_key or os.getenv("QDRANT_API_KEY")
        self.collection_name = collection_name or os.getenv("QDRANT_COLLECTION", "tara_case_memory")
        self.embedding_dim = embedding_dim
        self.enabled = False
        self.client = None
        
        if not QDRANT_AVAILABLE:
            logger.warning("⚠️ qdrant-client not installed. Memory features disabled.")
            return

        if not self.url:
            logger.warning("⚠️ QDRANT_URL not set. Memory features disabled.")
            return
            
        try:
            # check_compatibility=False to avoid 403 errors during version check
            self.client = AsyncQdrantClient(
                url=self.url, 
                api_key=self.api_key,
                check_compatibility=False,
                timeout=0.4  # 400ms - fast fail for Hive Mind queries
            )
            # Sync check for startup
            sync_check = QdrantClient(url=self.url, api_key=self.api_key, check_compatibility=False)
            if sync_check.collection_exists(self.collection_name):
                self.enabled = True
            else:
                logger.info(f"Creating Qdrant collection: {self.collection_name}")
                sync_check.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.embedding_dim,
                        distance=models.Distance.COSINE
                    ),
                    # Optimization: HNSW Tuning for diverse data types
                    hnsw_config=models.HnswConfigDiff(
                        m=16,
                        ef_construct=100,
                    ),
                    # Optimization: Scalar Quantization (INT8) saves 75% RAM and speeds up search
                    quantization_config=models.ScalarQuantization(
                        scalar=models.ScalarQuantizationConfig(
                            type=models.ScalarType.INT8,
                            always_ram=True,
                        )
                    )
                )
                
                # Add Payload Indexes for ultra-fast filtering
                self._create_payload_indexes(sync_check)
                self.enabled = True
            
            if self.enabled:
                logger.info(f"✅ Qdrant Memory initialized (URL: {self.url})")
        except Exception as e:
            logger.error(f"❌ Qdrant connection failed: {e}")
            self.enabled = False

    def _create_payload_indexes(self, client):
        """Create keyword indexes for common filter fields."""
        fields = ["doc_type", "domain", "tenant_id", "type", "successful", "schema_version", "label"]
        for field in fields:
            try:
                client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                logger.info(f"✅ Created payload index for: {field}")
            except Exception as e:
                # Likely already exists
                pass

    def _ensure_collection(self):
        """Create collection if it doesn't exist."""
        if self.client is None: 
            return
        
        try:
            if not self.client.collection_exists(self.collection_name):
                logger.info(f"Creating Qdrant collection: {self.collection_name}")
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.embedding_dim,
                        distance=models.Distance.COSINE
                    ),
                    hnsw_config=models.HnswConfigDiff(
                        m=16,
                        ef_construct=100,
                    ),
                    quantization_config=models.ScalarQuantization(
                        scalar=models.ScalarQuantizationConfig(
                            type=models.ScalarType.INT8,
                            always_ram=True,
                        )
                    )
                )
            
            # Always check/create payload indexes on startup
            try:
                sync_check = QdrantClient(url=self.url, api_key=self.api_key, check_compatibility=False)
                self._create_payload_indexes(sync_check)
            except Exception as e:
                logger.warning(f"Failed to verify payload indexes: {e}")

            self.enabled = True  # Successfully connected and verified collection

        except Exception as e:
            logger.error(f"Failed to ensure collection: {e}")
            self.enabled = False

    async def upsert_case(self, 
                    user_id: str, 
                    issue: str, 
                    solution: str, 
                    vector: List[float],
                    tenant_id: str = "demo",
                    domain: Optional[str] = None,
                    metadata: Optional[Dict] = None):
        """
        Store a resolved case in memory using Universal Schema.
        """
        if not self.enabled: return
        
        try:
            payload = case_memory_payload(
                issue=issue,
                solution=solution,
                domain=domain or "all",
                tenant_id=tenant_id,
                user_id=user_id,
            )
            
            if metadata:
                payload.update(metadata)
            
            point_id = payload.pop("uuid")  # Use schema-generated UUID
                
            await self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload
                    )
                ]
            )
            logger.info(f"🧠 Learned new case from User {user_id}: {issue[:30]}...")
        except Exception as e:
            logger.error(f"Failed to upsert case: {e}")

    async def search_hive_mind(self, query_vector: List[float], tenant_id: str = "demo", domain: Optional[str] = None, limit: int = 3, score_threshold: float = 0.4) -> List[Dict]:
        """
        Search GLOBAL memory (Hive Mind) for similar issues within the same tenant and domain.
        Returns list of solutions.
        
        Args:
            query_vector: Embedding vector for semantic search
            tenant_id: Tenant identifier for multi-tenancy
            domain: Optional domain filter (e.g., "groq.com", "airbnb.com")
            limit: Maximum number of results
            score_threshold: Minimum similarity score
        """
        if not self.enabled: return []
        
        try:
            # Multi-tenant filter with optional domain filtering
            filter_conditions = [
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=tenant_id)
                )
            ]
            
            # Add domain filter if provided (MAPPED MODE)
            if domain:
                filter_conditions.append(
                    models.FieldCondition(
                        key="domain",
                        match=models.MatchValue(value=domain)
                    )
                )
                logger.debug(f"🗺️ MAPPED MODE: Filtering HiveMind by domain={domain}")
            else:
                logger.debug("🧭 EXPLORER MODE: No domain filter (searching all domains)")
            
            query_filter = models.Filter(must=filter_conditions)

            response = await self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
                score_threshold=score_threshold
            )
            hits = response.points
            
            if hits:
                domain_info = f" [domain={domain}]" if domain else " [all domains]"
                logger.info(f"🧠 Hive Mind{domain_info}: {len(hits)} matches (top score: {hits[0].score:.3f})")
            else:
                domain_info = f" for domain={domain}" if domain else ""
                logger.info(f"🧠 Hive Mind: No matches{domain_info} above threshold {score_threshold}")
            
            results = []
            for hit in hits:
                p = hit.payload
                results.append({
                    "solution": read_summary(p),
                    "issue": read_text(p),
                    "user_id": p.get("user_id"),
                    "confidence": hit.score,
                    "timestamp": read_created_at(p),
                    "doc_type": read_doc_type(p),
                })
            return results
        except Exception as e:
            logger.error(f"Hive Mind search failed: {e}")
            return []
    
    async def check_domain_has_knowledge(self, domain: str, tenant_id: str = "demo") -> bool:
        """
        Check if HiveMind has any knowledge for a specific domain.
        Used to determine MAPPED vs EXPLORER mode.
        
        Args:
            domain: Domain to check (e.g., "groq.com")
            tenant_id: Tenant identifier
            
        Returns:
            True if domain has HiveMind knowledge (MAPPED MODE)
            False if domain is unknown (EXPLORER MODE)
        """
        if not self.enabled:
            return False
        
        try:
            # Count points for this domain
            result = await self.client.count(
                collection_name=self.collection_name,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="tenant_id",
                            match=models.MatchValue(value=tenant_id)
                        ),
                        models.FieldCondition(
                            key="domain",
                            match=models.MatchValue(value=domain)
                        )
                    ]
                )
            )
            
            has_knowledge = result.count > 0
            mode = "MAPPED" if has_knowledge else "EXPLORER"
            logger.info(f"🗺️ Domain '{domain}': {result.count} HiveMind entries → {mode} MODE")
            return has_knowledge
            
        except Exception as e:
            logger.error(f"Failed to check domain knowledge: {e}")
            return False

    async def search_skills_and_rules(
        self,
        query_vector: List[float],
        tenant_id: str = "demo",
        topic: Optional[str] = None,
        limit: int = 5,
        score_threshold: float = 0.35
    ) -> Dict[str, List[Dict]]:
        """
        Retrieve agent skills and rules from Qdrant based on semantic similarity.

        Returns:
            Dict with 'skills' and 'rules' lists, each containing dicts with
            'text', 'topic', 'severity' (rules only), and 'score'.
        """
        if not self.enabled:
            return {"skills": [], "rules": []}

        try:
            # Filter for skills/rules — match both new doc_type AND legacy type
            type_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="tenant_id",
                        match=models.MatchValue(value=tenant_id)
                    )
                ],
                should=[
                    # New schema
                    models.FieldCondition(
                        key="doc_type",
                        match=models.MatchValue(value="Agent_Skill")
                    ),
                    models.FieldCondition(
                        key="doc_type",
                        match=models.MatchValue(value="Agent_Rule")
                    ),
                    # Legacy compat
                    models.FieldCondition(
                        key="type",
                        match=models.MatchValue(value="agent_skill")
                    ),
                    models.FieldCondition(
                        key="type",
                        match=models.MatchValue(value="agent_rule")
                    )
                ]
            )

            # Add optional topic filter
            if topic:
                type_filter.must.append(
                    models.FieldCondition(
                        key="topic",
                        match=models.MatchValue(value=topic)
                    )
                )

            response = await self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=type_filter,
                limit=limit,
                score_threshold=score_threshold
            )
            hits = response.points

            skills = []
            rules = []
            for hit in hits:
                p = hit.payload
                entry = {
                    "text": read_text(p),
                    "topic": p.get("topic", "general"),
                    "score": hit.score
                }
                dt = read_doc_type(p)
                if dt == "Agent_Skill" or p.get("type") == "agent_skill":
                    skills.append(entry)
                elif dt == "Agent_Rule" or p.get("type") == "agent_rule":
                    entry["severity"] = p.get("severity", "standard")
                    rules.append(entry)

            if skills or rules:
                logger.info(f"🎯 Skills/Rules retrieved: {len(skills)} skills, {len(rules)} rules (top score: {hits[0].score:.3f})")
            else:
                logger.debug("🎯 No matching skills/rules found above threshold")

            return {"skills": skills, "rules": rules}
        except Exception as e:
            logger.error(f"Skills/rules search failed: {e}")
            return {"skills": [], "rules": []}

    async def search_user_history(self, user_id: str, query_vector: List[float], tenant_id: str = "demo", limit: int = 3) -> List[Dict]:
        """
        Search PERSONAL memory for recall within the same tenant.
        """
        if not self.enabled: return []
        
        try:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="tenant_id",
                        match=models.MatchValue(value=tenant_id)
                    ),
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=user_id)
                    )
                ]
            )

            response = await self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=limit
            )
            hits = response.points
            
            results = []
            for hit in hits:
                p = hit.payload
                ts = read_created_at(p)
                issue_text = read_text(p)
                soln_text = read_summary(p)
                results.append({
                    "suggestion": f"On {time.strftime('%Y-%m-%d', time.localtime(ts))}, you fixed '{issue_text}' by: {soln_text}",
                    "confidence": hit.score
                })
            return results
        except Exception as e:
            logger.error(f"User history search failed: {e}")
            return []

    def get_user_context(self, user_id: str, limit: int = 1) -> List[Dict]:
        """
        Get most recent interaction for this user (for greeting).
        """
        if not self.enabled: return []
        
        try:
            # Scroll/Query by user_id sorted by timestamp desc
            # Note: Qdrant scroll doesn't sort by payload field easily efficiently without index
            # Ideally we would use filter + dummy vector or scroll. 
            # Simplified: Just return empty or implement simple scroll if supported.
            # actually better to use filter
            
            # Using scroll for retrieving latest by filter is tricky.
            # Using search with zero vector works if we just want "any" or we can search with a generic "problem" vector.
            pass 
        except Exception:
            pass
        return []

    async def delete_user_memory(self, user_id: str, tenant_id: str = "demo"):
        """GDPR Compliance: Delete all vectors for a user within a tenant."""
        if not self.enabled: return
        
        try:
            await self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="tenant_id",
                                match=models.MatchValue(value=tenant_id)
                            ),
                            models.FieldCondition(
                                key="user_id",
                                match=models.MatchValue(value=user_id)
                            )
                        ]
                    )
                )
            )
            logger.info(f"🗑️ Deleted memory for User {user_id} (GDPR)")
        except Exception as e:
            logger.error(f"Failed to delete user memory: {e}")

    # ── Batch upsert for population scripts ─────────────────────────────────
    async def batch_upsert(self, points: List[models.PointStruct]) -> int:
        """Upsert a list of PointStruct objects in batches of 50."""
        if not self.enabled:
            return 0
        total = 0
        batch_size = 50
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            try:
                await self.client.upsert(
                    collection_name=self.collection_name,
                    points=batch,
                )
                total += len(batch)
            except Exception as e:
                logger.error(f"Batch upsert failed at offset {i}: {e}")
        return total

import os
import time
import uuid
import logging
import re
import hashlib
from typing import List, Dict, Optional, Any, Union

# Try to import qdrant_client - graceful degradation if missing
try:
    from qdrant_client import QdrantClient, AsyncQdrantClient
    from qdrant_client.http import models
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

# Universal Payload Schema
try:
    from .models.hivemind_schema import (
        case_memory_payload, agent_skill_payload, agent_rule_payload,
        website_map_payload, element_context_payload, general_kb_payload,
        read_text, read_summary, read_doc_type, read_created_at,
        SCHEMA_VERSION,
    )
except ImportError:
    from .models.hivemind_schema import (
        case_memory_payload, agent_skill_payload, agent_rule_payload,
        website_map_payload, element_context_payload, general_kb_payload,
        read_text, read_summary, read_doc_type, read_created_at,
        SCHEMA_VERSION,
    )

logger = logging.getLogger(__name__)

class LexicalSparseEncoder:
    """
    Very lightweight tokenizer-based sparse vector generator.
    Enables BM25-like behavior in Qdrant without needing heavy SPLADE models.
    """
    def __init__(self, max_indices: int = 1000000):
        self.max_indices = max_indices

    def encode(self, text: str) -> Dict[str, Union[List[int], List[float]]]:
        if not text:
            return {"indices": [], "values": []}
            
        # Tokenize (lowercase, remove non-alphanumeric except underscore)
        tokens = re.findall(r'\w+', text.lower())
        if not tokens:
            return {"indices": [], "values": []}

        # Count frequencies
        counts = {}
        for t in tokens:
            # Hash token to a stable index within range
            idx = int(hashlib.md5(t.encode()).hexdigest(), 16) % self.max_indices
            counts[idx] = counts.get(idx, 0) + 1

        # Sort by index for Qdrant compatibility (though not strictly required for dict format)
        sorted_indices = sorted(counts.keys())
        return {
            "indices": sorted_indices,
            "values": [float(counts[idx]) for idx in sorted_indices]
        }

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
        self._ensured_collections = set()
        self._sync_clients = {}
        self._async_clients = {}
        self._vector_name_cache = {}
        self.sparse_encoder = LexicalSparseEncoder()
        
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
                timeout=5  # 5s — cloud Qdrant needs headroom
            )
            # Sync check for startup
            sync_check = QdrantClient(url=self.url, api_key=self.api_key, check_compatibility=False)
            if sync_check.collection_exists(self.collection_name):
                self.enabled = True
                self._ensured_collections.add(self.collection_name)
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
                self._create_payload_indexes(sync_check, self.collection_name)
                self.enabled = True
                self._ensured_collections.add(self.collection_name)
            
            if self.enabled:
                logger.info(f"✅ Qdrant Memory initialized (URL: {self.url})")
        except Exception as e:
            logger.error(f"❌ Qdrant connection failed: {e}")
            self.enabled = False

    def _create_payload_indexes(self, client, collection_name: Optional[str] = None):
        """Create keyword indexes for common filter fields."""
        target_collection = collection_name or self.collection_name
        fields = [
            "doc_type", "domain", "tenant_id", "agent_id", "session_type", "session_id",
            "type", "successful", "schema_version", "label"
        ]
        for field in fields:
            try:
                client.create_payload_index(
                    collection_name=target_collection,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                logger.info(f"✅ Created payload index for: {field}")
            except Exception as e:
                # Likely already exists
                pass

    def _get_vector_name_for_collection(self, sync_client, collection_name: str) -> Optional[str]:
        """Return named vector key if collection uses named vectors, else None."""
        cache_key = f"{id(sync_client)}:{collection_name}"
        if cache_key in self._vector_name_cache:
            return self._vector_name_cache[cache_key]
        try:
            info = sync_client.get_collection(collection_name)
            vectors_cfg = getattr(getattr(info, "config", None), "params", None)
            vectors = getattr(vectors_cfg, "vectors", None)

            vector_name = None
            if isinstance(vectors, dict) and vectors:
                for key in vectors.keys():
                    if str(key).strip():
                        vector_name = str(key).strip()
                        break
            elif vectors is not None:
                dumped = None
                if hasattr(vectors, "model_dump"):
                    dumped = vectors.model_dump()
                elif hasattr(vectors, "dict"):
                    dumped = vectors.dict()
                elif hasattr(vectors, "__dict__"):
                    dumped = dict(vars(vectors))

                if isinstance(dumped, dict) and dumped:
                    for key in dumped.keys():
                        if key in {"size", "distance", "on_disk", "datatype", "hnsw_config", "quantization_config"}:
                            continue
                        if str(key).strip():
                            vector_name = str(key).strip()
                            break

            if not vector_name:
                try:
                    collection_payload = info.model_dump() if hasattr(info, "model_dump") else {}
                    params = ((collection_payload.get("config") or {}).get("params") or {})
                    dumped_vectors = params.get("vectors")
                    if isinstance(dumped_vectors, dict):
                        for key, value in dumped_vectors.items():
                            if key in {"size", "distance", "on_disk", "datatype", "hnsw_config", "quantization_config"}:
                                continue
                            if isinstance(value, dict) or value is not None:
                                if str(key).strip():
                                    vector_name = str(key).strip()
                                    break
                except Exception:
                    pass

            self._vector_name_cache[cache_key] = vector_name
            return vector_name
        except Exception:
            self._vector_name_cache[cache_key] = None
            return None

    @staticmethod
    def _sanitize_tenant(tenant_id: str) -> str:
        tenant = (tenant_id or "tara").strip().lower()
        # Remove any trailing paths like /ws that the orchestrator might accidentally pass
        tenant = tenant.split('/')[0]
        return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in tenant)

    def _get_tenant_env(self, tenant_id: str, suffix: str) -> Optional[str]:
        tenant = self._sanitize_tenant(tenant_id)
        # Requested pattern first: <tenant>_<suffix>
        val = os.getenv(f"{tenant}_{suffix}")
        if val:
            return val
        # Compatibility fallback: <TENANT>_<SUFFIX>
        return os.getenv(f"{tenant.upper()}_{suffix.upper()}")

    def _resolve_tenant_qdrant(self, tenant_id: str = "tara") -> Dict[str, Optional[str]]:
        """
        Resolve Qdrant endpoint config per tenant.

        Expected env pattern:
        - <tenant>_qdrant_url
        - <tenant>_apikey
        - <tenant>_collectionname
        """
        url = self._get_tenant_env(tenant_id, "qdrant_url") or self.url
        api_key = self._get_tenant_env(tenant_id, "apikey") or self.api_key
        collection = self._get_tenant_env(tenant_id, "collectionname") or self.collection_name
        return {"url": url, "api_key": api_key, "collection_name": collection}

    def _resolve_collection_name(self, tenant_id: str = "tara") -> str:
        """
        Resolve tenant-specific collection name.

        Priority:
        1) <tenant>_collectionname
        2) <tenant>_qdrant_collection (legacy)
        3) <TENANT>_QDRANT_COLLECTION
        4) default self.collection_name
        """
        tenant = self._sanitize_tenant(tenant_id)
        collection_name = self._get_tenant_env(tenant_id, "collectionname")
        if collection_name:
            return collection_name
        lower_key = f"{tenant}_qdrant_collection"
        upper_key = f"{tenant.upper()}_QDRANT_COLLECTION"
        return os.getenv(lower_key) or os.getenv(upper_key) or self.collection_name

    def _get_clients_for_tenant(self, tenant_id: str = "tara"):
        cfg = self._resolve_tenant_qdrant(tenant_id)
        url = cfg["url"]
        api_key = cfg["api_key"]
        if not url:
            return None, None, None
        key = f"{url}|{api_key or ''}"
        if key not in self._sync_clients:
            self._sync_clients[key] = QdrantClient(url=url, api_key=api_key, check_compatibility=False)
        if key not in self._async_clients:
            self._async_clients[key] = AsyncQdrantClient(
                url=url,
                api_key=api_key,
                check_compatibility=False,
                timeout=5
            )
        return self._sync_clients[key], self._async_clients[key], cfg["collection_name"]

    def _ensure_collection_for(self, sync_client, collection_name: str):
        """Ensure target collection exists and has payload indexes."""
        if sync_client is None or not collection_name:
            return
        client_key = f"{id(sync_client)}:{collection_name}"
        if client_key in self._ensured_collections:
            return
        try:
            if not sync_client.collection_exists(collection_name):
                logger.info(f"Creating tenant collection: {collection_name}")
                sync_client.create_collection(
                    collection_name=collection_name,
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
            self._create_payload_indexes(sync_client, collection_name)
            self._ensured_collections.add(client_key)
        except Exception as e:
            logger.error(f"Failed ensuring tenant collection '{collection_name}': {e}")

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
                self._create_payload_indexes(sync_check, self.collection_name)
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
                    tenant_id: str = "tara",
                    domain: Optional[str] = None,
                    metadata: Optional[Dict] = None):
        """
        Store a resolved case in memory using Universal Schema.
        """
        if not self.enabled: return
        
        try:
            sync_client, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
            if async_client is None:
                logger.warning(f"Qdrant not configured for tenant={tenant_id}; skipping upsert_case")
                return
            self._ensure_collection_for(sync_client, collection_name)
            vector_name = self._get_vector_name_for_collection(sync_client, collection_name)
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
            
            # Generate sparse vector for hybrid search
            sparse_vec = self.sparse_encoder.encode(f"{issue} {solution}")
            dense_name = vector_name or "vector"

            await async_client.upsert(
                collection_name=collection_name,
                points=[
                    models.PointStruct(
                        id=point_id,
                        vector={
                            dense_name: vector,
                            "sparse": models.SparseVector(**sparse_vec)
                        },
                        payload=payload
                    )
                ]
            )
            logger.info(
                f"🧠 Learned new case | tenant={tenant_id} collection={collection_name} "
                f"vector={vector_name or 'default'} user={user_id}"
            )
        except Exception as e:
            logger.error(f"Failed to upsert case: {e}")

    async def search_hive_mind(self, query_vector: List[float], tenant_id: str = "tara", domain: Optional[str] = None, limit: int = 3, score_threshold: float = 0.4) -> List[Dict]:
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
            sync_client, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
            if async_client is None:
                return []
            vector_name = self._get_vector_name_for_collection(sync_client, collection_name)
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

            response = await async_client.query_points(
                collection_name=collection_name,
                query=query_vector,
                using=vector_name,
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
    
    async def check_domain_has_knowledge(self, domain: str, tenant_id: str = "tara") -> bool:
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
            _, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
            if async_client is None:
                return False
            # Count points for this domain
            result = await async_client.count(
                collection_name=collection_name,
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
        tenant_id: str = "tara",
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
            sync_client, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
            if async_client is None:
                return {"skills": [], "rules": []}
            vector_name = self._get_vector_name_for_collection(sync_client, collection_name)
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

            response = await async_client.query_points(
                collection_name=collection_name,
                query=query_vector,
                using=vector_name,
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
                    "id": hit.id,
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

    async def search_user_history(self, user_id: str, query_vector: List[float], tenant_id: str = "tara", limit: int = 3) -> List[Dict]:
        """
        Search PERSONAL memory for recall within the same tenant.
        """
        if not self.enabled: return []
        
        try:
            sync_client, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
            if async_client is None:
                return []
            vector_name = self._get_vector_name_for_collection(sync_client, collection_name)
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

            response = await async_client.query_points(
                collection_name=collection_name,
                query=query_vector,
                using=vector_name,
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

    async def delete_user_memory(self, user_id: str, tenant_id: str = "tara"):
        """GDPR Compliance: Delete all vectors for a user within a tenant."""
        if not self.enabled: return
        
        try:
            _, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
            if async_client is None:
                return
            await async_client.delete(
                collection_name=collection_name,
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

    async def search_unified_memory(
        self,
        query_vector: List[float],
        tenant_id: str = "tara",
        doc_types: Optional[List[str]] = None,
        limit: int = 10,
        score_threshold: float = 0.4,
        query_text: Optional[str] = None
    ) -> List[Dict]:
        """
        Unified search across multiple doc types with filtering.
        Supports Hybrid Search (Vector + Lexical) if query_text is provided.
        """
        if not self.enabled: return []
        
        try:
            sync_client, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
            if async_client is None:
                return []
            vector_name = self._get_vector_name_for_collection(sync_client, collection_name)
            # Multi-tenant filter
            must_conditions = [
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=tenant_id)
                )
            ]
            
            # Should conditions for doc_types (OR logic)
            should_conditions = []
            if doc_types:
                for dt in doc_types:
                    should_conditions.append(
                        models.FieldCondition(
                            key="doc_type",
                            match=models.MatchValue(value=dt)
                        )
                    )
            
            query_filter = models.Filter(
                must=must_conditions,
                should=should_conditions if should_conditions else None
            )

            # NOTE: If should_conditions is provided, we must ensure at least one matches
            # Qdrant: if 'must' is present, 'should' acts as boost unless check is involved.
            # To enforce "match ANY of these types", we need nested filter or min_should_match=1 if feasible,
            # but Qdrant standard filter with 'must' and 'should' usually means 'must AND (maybe should)'.
            # To filter by types, we strictly need them in 'must' with a 'checklist' or nested Filter is easier.
            # But Qdrant Python client models.Filter has 'should'.
            # Actually, standard way to do "IN LIST" is:
            # must=[tenant, Filter(should=[type=A, type=B])]
            
            if should_conditions:
                 # Wrap the OR condition in a nested Filter inside MUST
                 # to enforce that it MUST match one of the types.
                 nested_or = models.Filter(should=should_conditions)
                 must_conditions.append(nested_or)
                 # Clear top-level should to avoid confusion, passing nested_or in must is correct for logic:
                 # tenant_id=X AND (doc_type=A OR doc_type=B)
                 final_filter = models.Filter(must=must_conditions)
            else:
                 final_filter = models.Filter(must=must_conditions)

            # Hybrid Search Logic:
            # We use Prefetches to combine Dense and Sparse search results via RRF
            if query_text:
                sparse_vec = self.sparse_encoder.encode(query_text)
                # Note: We assume the collection has a sparse vector named 'sparse'
                # If it doesn't, this will gracefully fail or we can catch it.
                
                # We also need the dense vector name (usually 'dense' or 'vector')
                dense_name = vector_name or "vector"
                
                try:
                    response = await async_client.query_points(
                        collection_name=collection_name,
                        prefetch=[
                            models.Prefetch(
                                query=query_vector,
                                using=dense_name,
                                filter=final_filter,
                                limit=limit * 2,
                                score_threshold=score_threshold
                            ),
                            models.Prefetch(
                                query=models.SparseVector(**sparse_vec),
                                using="sparse",
                                filter=final_filter,
                                limit=limit * 2
                            )
                        ],
                        query=models.FusionQuery(fusion=models.Fusion.RRF),
                        limit=limit
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Hybrid search failed (fallback to dense): {e}")
                    response = await async_client.query_points(
                        collection_name=collection_name,
                        query=query_vector,
                        using=vector_name,
                        query_filter=final_filter,
                        limit=limit,
                        score_threshold=score_threshold
                    )
            else:
                response = await async_client.query_points(
                    collection_name=collection_name,
                    query=query_vector,
                    using=vector_name,
                    query_filter=final_filter,
                    limit=limit,
                    score_threshold=score_threshold
                )
            
            hits = response.points
            
            results = []
            for hit in hits:
                p = hit.payload
                # Normalize entry
                entry = {
                    "id": hit.id,
                    "text": read_text(p),
                    "summary": read_summary(p),
                    "doc_type": read_doc_type(p),
                    "score": hit.score,
                    "payload": p
                }
                results.append(entry)
                
            if results:
                logger.info(f"🧠 Unified Search: {len(results)} matches for types {doc_types}")
                
            return results
        except Exception as e:
            logger.error(f"Unified search failed: {e}")
            return []

    async def browse_tenant_memory(
        self,
        tenant_id: str = "tara",
        doc_types: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Browse latest tenant memories without semantic query matching.
        Useful for HiveMind inventory-style questions such as
        "what do you have" or "what happened last week".
        """
        if not self.enabled:
            return []

        try:
            _, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
            if async_client is None:
                return []

            must_conditions = [
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=tenant_id)
                )
            ]

            if doc_types:
                type_conditions = [
                    models.FieldCondition(
                        key="doc_type",
                        match=models.MatchValue(value=dt)
                    )
                    for dt in doc_types
                ]
                must_conditions.append(models.Filter(should=type_conditions))

            records, _ = await async_client.scroll(
                collection_name=collection_name,
                scroll_filter=models.Filter(must=must_conditions),
                limit=min(max(limit * 3, limit), 100),
                with_payload=True,
                with_vectors=False,
            )

            normalized = []
            for point in records or []:
                payload = point.payload or {}
                normalized.append({
                    "id": point.id,
                    "text": read_text(payload),
                    "summary": read_summary(payload),
                    "doc_type": read_doc_type(payload),
                    "score": 1.0,
                    "payload": payload,
                    "created_at": read_created_at(payload),
                })

            normalized.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
            results = normalized[:limit]

            if results:
                logger.info(f"🧠 HiveMind browse: {len(results)} latest entries for tenant={tenant_id} types={doc_types or 'all'}")

            return results
        except Exception as e:
            logger.error(f"HiveMind browse failed: {e}")
            return []

    # ── Batch upsert for population scripts ─────────────────────────────────
    async def batch_upsert(self, points: List[models.PointStruct], tenant_id: str = "tara") -> int:
        """Upsert a list of PointStruct objects in batches of 50."""
        if not self.enabled:
            return 0
        sync_client, async_client, collection_name = self._get_clients_for_tenant(tenant_id)
        if async_client is None:
            return 0
        self._ensure_collection_for(sync_client, collection_name)
        total = 0
        batch_size = 50
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            
            # Hybrid Search Enrichment:
            # Ensure every point has a sparse vector for lexical search coverage
            enriched_batch = []
            for p in batch:
                vectors = p.vector
                # If vectors is already a dict with 'sparse', keep it as is
                if isinstance(vectors, dict) and "sparse" in vectors:
                    enriched_batch.append(p)
                    continue
                
                # Otherwise, try to generate sparse from payload
                try:
                    payload = p.payload or {}
                    # Collect all searchable text fields
                    searchable_text = " ".join([
                        str(payload.get(f, "")) 
                        for f in ["text", "issue", "solution", "summary", "topic", "filename", "question"] 
                        if payload.get(f)
                    ])
                    
                    if searchable_text:
                        sparse_vec = self.sparse_encoder.encode(searchable_text)
                        
                        # Handle dense vector name
                        # Most batches use named vectors or just 'vector' for default
                        if isinstance(vectors, dict):
                            # It's a dict but missing 'sparse'
                            new_vectors = dict(vectors)
                            new_vectors["sparse"] = models.SparseVector(**sparse_vec)
                        else:
                            # It's a single vector (list/array)
                            new_vectors = {
                                "dense": vectors,
                                "sparse": models.SparseVector(**sparse_vec)
                            }
                        
                        enriched_batch.append(models.PointStruct(
                            id=p.id,
                            vector=new_vectors,
                            payload=payload
                        ))
                    else:
                        enriched_batch.append(p)
                except Exception as e:
                    logger.warning(f"Failed to enrich point {p.id} with sparse vector: {e}")
                    enriched_batch.append(p)

            try:
                await async_client.upsert(
                    collection_name=collection_name,
                    points=enriched_batch,
                )
                total += len(enriched_batch)
            except Exception as e:
                logger.error(f"Batch upsert failed at offset {i}: {e}")
        return total

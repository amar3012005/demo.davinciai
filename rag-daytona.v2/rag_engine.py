from .context_architecture import ContextArchitect
"""
RAG Engine Core Logic - Daytona V2 Optimized
"""

import os
import json
import time
import logging
import hashlib
import re
import asyncio
from typing import Dict, Any, Optional, List, Callable, Union
import numpy as np
import faiss
from .llm_providers import create_provider, create_fallback_provider
from .config import RAGConfig
from .prompts import prompt_manager
from .optimized_embeddings import OptimizedEmbeddings
from .xml_prompts import XMLPromptManager
from .qdrant_addon import QdrantAddon
from .distillprompt_hivemind_savecase import CaseDistiller

try:
    from groq import Groq
except ImportError:
    Groq = None

logger = logging.getLogger(__name__)


class RAGEngine:
    """
    Core RAG engine with FAISS retrieval and LLM generation.
    """
    
    def __init__(self, config: RAGConfig):
        """
        Initialize RAG engine with configuration.
        """
        self.config = config
        
        # Initialize Embeddings if Local Retrieval OR Hive Mind is enabled
        if self.config.enable_local_retrieval or self.config.enable_hive_mind:
            # Initialize Optimized ONNX Embeddings (Target: <500ms cold start)
            try:
                # Use model from config (can be local path or HuggingFace ID)
                model_path = self.config.embedding_model_name
                
                # Special check: if using default path but config says otherwise, respect config
                if not model_path:
                    model_path = "/app/model_onnx"
                    
                if os.path.exists(model_path):
                    logger.info(f"📂 Loading model from path: {model_path}")
                else:
                    logger.info(f"🌐 Loading model from Hub/Cache: {model_path}")
                
                self.embeddings = OptimizedEmbeddings(
                    model_path=model_path, 
                    device="cpu"
                )
                logger.info(f"✅ Optimized Embeddings initialized: {model_path}")
            except Exception as e:
                logger.error(f"❌ Failed to init OptimizedEmbeddings: {e}")
                raise e
        else:
            logger.info("ℹ️ Retrieval is DISABLED (Local & HiveMind off) - skipping embedding model initialization")
            self.embeddings = None

        # Initialize LLM with fallback support
        self.llm = None
        if config.enable_llm_fallback and config.fallback_llm_provider and config.fallback_llm_api_key:
            logger.info("🛡️ Initializing LLM with fallback support...")
            try:
                self.llm = create_fallback_provider(
                    primary_provider=config.llm_provider,
                    primary_api_key=config.llm_api_key,
                    primary_model=config.llm_model,
                    fallback_provider=config.fallback_llm_provider,
                    fallback_api_key=config.fallback_llm_api_key,
                    fallback_model=config.fallback_llm_model,
                    site_url=getattr(config, 'openrouter_site_url', 'https://tara.task.gov.in'),
                    app_name=getattr(config, 'openrouter_app_name', 'TARA'),
                    enable_reasoning=getattr(config, 'openrouter_enable_reasoning', True)
                )
            except Exception as e:
                logger.error(f"❌ Fallback provider init failed: {e}")
        elif config.llm_api_key:
            logger.info(f"🤖 Single LLM provider: {config.llm_provider}")
            try:
                kwargs = {}
                if config.llm_provider == "openrouter":
                    kwargs['site_url'] = getattr(config, 'openrouter_site_url', 'https://tara.task.gov.in')
                    kwargs['app_name'] = getattr(config, 'openrouter_app_name', 'TARA')
                
                self.llm = create_provider(
                    provider_name=config.llm_provider,
                    api_key=config.llm_api_key,
                    model_name=config.llm_model,
                    **kwargs
                )
            except Exception as e:
                logger.error(f"❌ LLM provider init failed: {e}")
        
        # Initialize Qdrant Memory Add-on (Hive Mind)
        self.qdrant = None
        if self.config.enable_hive_mind and self.config.qdrant_url:
            try:
                self.qdrant = QdrantAddon(
                    embedding_dim=self.config.embedding_dimension,
                    url=self.config.qdrant_url,
                    api_key=self.config.qdrant_api_key,
                    collection_name=self.config.qdrant_collection
                )
                if self.qdrant.enabled:
                    logger.info(f"🧠 ✅ Qdrant Hive Mind ENABLED - Collection: {self.qdrant.collection_name}")
                else:
                    logger.warning("🧠 ⚠️ Qdrant Hive Mind available but not enabled.")
            except Exception as e:
                logger.error(f"❌ Qdrant Hive Mind init failed: {e}")

        # Initialize Groq Client for fast translation
        self.groq_client = None
        groq_key = os.getenv("GROQ_API_KEY")
        if Groq and groq_key:
            try:
                self.groq_client = Groq(api_key=groq_key)
                logger.info("🚀 Groq client initialized for fast translation")
            except Exception as e:
                logger.error(f"❌ Groq initialization failed: {e}")
        
        # Initialize XML Prompt Manager for context-rich structured prompts
        self.xml_prompts = XMLPromptManager()
        self.cache_manager = None # Caching handled by providers if enabled

        # Storage
        self.vector_store = None
        self.documents: List[str] = []
        self.doc_metadata: List[Dict[str, Any]] = []
        
        # Performance tracking
        self.query_count = 0
        self.total_query_time = 0.0
        
        # Patterns for hybrid retrieval
        self.patterns = {
            "installation": {
                "keywords": ["install", "setup", "how to install", "getting started", "deploy", "download", "run", "start"],
                "response_template": "Here's how to get started...",
                "faiss_boost": ["installation", "setup", "getting_started"],
                "max_context_chars": 1500,
                "priority": 3
            },
            "pricing": {
                "keywords": ["price", "cost", "free", "open source", "license", "payment", "subscription", "enterprise"],
                "response_template": "Here are the pricing details...",
                "faiss_boost": ["pricing", "license", "open_source"],
                "max_context_chars": 1000,
                "priority": 3
            },
            "features": {
                "keywords": ["feature", "capability", "what can it do", "support", "language", "provider", "backend"],
                "response_template": "Here are the available features and capabilities...",
                "faiss_boost": ["features", "capabilities", "providers"],
                "max_context_chars": 2000,
                "priority": 2
            },
            "contact_info": {
                "keywords": ["contact", "email", "phone", "support", "reach out", "help", "discord", "slack", "community"],
                "response_template": "You can reach us on Discord or Slack...",
                "faiss_boost": ["contact", "support", "community"],
                "max_context_chars": 800,
                "priority": 2
            },
            "ambiguous_question": {
                "keywords": ["what does that mean", "what do you mean", "i don't understand", "huh", "was bedeutet das", "ich verstehe nicht"],
                "response_template": {
                    "en": "I'm not sure I understand your question. Could you please rephrase it?",
                    "de": "Ich bin mir nicht sicher, ob ich Ihre Frage verstehe. Könnten Sie sie bitte umformulieren?"
                },
                "faiss_boost": [],
                "max_context_chars": 0,
                "priority": 0
            }
        }
        
        # Always load the index data (texts/metadata) so we can do rule-based retrieval even if vector search is off
        self.load_index()
    
    def load_index(self) -> bool:
        """Load pre-built FAISS index and document data from disk."""
        try:
            index_path = os.path.join(self.config.vector_store_path, "index.faiss")
            metadata_path = os.path.join(self.config.vector_store_path, "metadata.json")
            texts_path = os.path.join(self.config.vector_store_path, "texts.json")
            
            # 1. ALWAYS load JSON data (these are small and used for rule-based retrieval)
            if os.path.exists(metadata_path) and os.path.exists(texts_path):
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    self.doc_metadata = json.load(f)
                with open(texts_path, 'r', encoding='utf-8') as f:
                    self.documents = json.load(f)
                logger.info(f"📄 Loaded document data: {len(self.documents)} documents")
            else:
                logger.error(f"❌ Document files not found at {self.config.vector_store_path}")
                return False

            # 2. OPTIONALLY load FAISS index (only if local retrieval is enabled)
            if self.config.enable_local_retrieval:
                if os.path.exists(index_path):
                    self.vector_store = faiss.read_index(index_path)
                    
                    # Validate dimension match
                    if self.embeddings:
                        test_embedding = self.embeddings.embed_query("dimension validation test")
                        index_dim = self.vector_store.d
                        embedding_dim = len(test_embedding)
                        
                        if index_dim != embedding_dim:
                            logger.error(f"❌ DIMENSION MISMATCH: Index {index_dim} vs Model {embedding_dim}")
                            self.vector_store = None
                            return False
                        logger.info(f"✅ FAISS index loaded and validated ({embedding_dim}-D)")
                else:
                    logger.warning(f"⚠️ FAISS index file not found at {index_path}")
            else:
                logger.info("ℹ️ Skipping FAISS vector store loading (local retrieval disabled)")
            
            return True
        except Exception as e:
            logger.error(f" Error loading index: {e}")
            return False

    def _detect_query_pattern(self, query: str) -> Optional[Dict]:
        """Detect if query matches known patterns."""
        query_lower = query.lower()
        best_match = None
        best_priority = -1
        
        for name, config in self.patterns.items():
            if any(keyword in query_lower for keyword in config["keywords"]):
                if config["priority"] > best_priority:
                    best_match = {"name": name, **config}
                    best_priority = config["priority"]
        
        return best_match

    @staticmethod
    def _apply_prompt_budget(prompt: str, llm_model_name: str) -> str:
        """Reduce prompt size for latency-sensitive GPT-OSS calls."""
        model = (llm_model_name or "").lower()
        if "gpt-oss" not in model:
            return prompt

        reduced = prompt
        # Highest-cost section in current architecture.
        reduced = re.sub(
            r"<golden_examples\b[^>]*>.*?</golden_examples>",
            "<golden_examples>omitted_for_latency_budget</golden_examples>",
            reduced,
            flags=re.DOTALL
        )
        # Keep only the latest part of history block if present.
        history_match = re.search(r"(<conversation_history>)(.*?)(</conversation_history>)", reduced, flags=re.DOTALL)
        if history_match:
            body = history_match.group(2)
            if len(body) > 1200:
                body = "...[history trimmed for latency]...\n" + body[-1200:]
            reduced = reduced[:history_match.start(2)] + body + reduced[history_match.end(2):]

        return reduced

    async def _do_local_rag(self, query: str, context: Dict, boost_categories: list, max_chars: int, precomputed_embedding: Optional[np.ndarray] = None) -> tuple:
        """Helper for parallel execution of FAISS retrieval."""
        return self._retrieve_with_boosting(query, context, boost_categories, max_chars, precomputed_embedding=precomputed_embedding)

    def _retrieve_with_boosting(self, query: str, context: Dict, boost_categories: list, max_context_chars: int, precomputed_embedding: Optional[np.ndarray] = None) -> tuple:
        """FAISS retrieval with category boosting."""
        # 1. Check if local retrieval is explicitly disabled
        if not self.config.enable_local_retrieval:
            logger.info("🚫 Local retrieval is DISABLED (config.enable_local_retrieval=False). Returning 0 docs.")
            return [], {"embedding_ms": 0, "search_ms": 0}

        # 1b. Fallback to rule-based retrieval if vector store is missing but retrieval is ENABLED
        if self.vector_store is None:
            return self._retrieve_rule_based(query, context, boost_categories, max_context_chars)
        
        timing = {}
        t_start = time.time()
        
        # 2. Use precomputed embedding or compute it now
        if precomputed_embedding is not None:
            query_embedding = precomputed_embedding
            timing['embedding_ms'] = 0 # Already accounted for or shared
        else:
            query_embedding = self.embeddings.embed_query(query)
            query_embedding = np.array(query_embedding, dtype=np.float32).reshape(1, -1)
            timing['embedding_ms'] = (time.time() - t_start) * 1000
        
        # FAISS search
        s_start = time.time()
        distances, indices = self.vector_store.search(query_embedding, k=self.config.top_k + 5)
        timing['search_ms'] = (time.time() - s_start) * 1000
        
        candidates = []
        for i, idx in enumerate(indices[0]):
            if idx < len(self.documents):
                distance = float(distances[0][i])
                similarity = 1.0 - min(1.0, distance / 2.0)
                
                if similarity < self.config.similarity_threshold:
                    continue
                
                doc_meta = self.doc_metadata[idx] if idx < len(self.doc_metadata) else {}
                boosted_similarity = similarity
                
                # Check for category boost
                doc_cat = doc_meta.get('category', '').lower()
                if any(bc.lower() in doc_cat for bc in boost_categories):
                    boosted_similarity *= 1.2
                
                candidates.append({
                    'text': self.documents[idx],
                    'metadata': doc_meta,
                    'similarity': similarity,
                    'boosted_similarity': boosted_similarity
                })
        
        candidates.sort(key=lambda x: x['boosted_similarity'], reverse=True)
        
        # Truncate to max_chars
        final_docs = []
        current_chars = 0
        for doc in candidates:
            if current_chars + len(doc['text']) > max_context_chars:
                break
            final_docs.append(doc)
            current_chars += len(doc['text'])
        
        logger.info(f"📚 FAISS retrieval: {len(final_docs)} chunks ({current_chars} chars)")
        return final_docs, timing

    def _retrieve_rule_based(self, query: str, context: Dict, categories: list, max_context_chars: int) -> tuple:
        """Lightweight keyword and category based retrieval (no embeddings)."""
        timing = {"embedding_ms": 0, "search_ms": 0}
        start = time.time()
        
        query_words = set(re.findall(r'\w+', query.lower()))
        candidates = []
        
        # Scan documents for keyword matches and category matches
        for idx, text in enumerate(self.documents):
            meta = self.doc_metadata[idx] if idx < len(self.doc_metadata) else {}
            score = 0
            
            # Category match (high weight)
            doc_cat = meta.get('category', '').lower()
            if any(c.lower() in doc_cat for c in categories):
                score += 10
                
            # Keyword match in text (count unique word overlaps)
            doc_words = set(re.findall(r'\w+', text.lower()))
            overlap = len(query_words.intersection(doc_words))
            score += overlap
            
            if score > 0:
                candidates.append({
                    'text': text,
                    'metadata': meta,
                    'similarity': min(0.9, score / 20.0), # Mock similarity
                    'boosted_similarity': score
                })
        
        # Sort and truncate
        candidates.sort(key=lambda x: x['boosted_similarity'], reverse=True)
        final_docs = []
        current_chars = 0
        for doc in candidates[:10]: # Top 10 rule-based
            if current_chars + len(doc['text']) > max_context_chars:
                break
            final_docs.append(doc)
            current_chars += len(doc['text'])
            
        timing['search_ms'] = (time.time() - start) * 1000
        logger.info(f"📏 Rule-based retrieval: {len(final_docs)} chunks ({current_chars} chars)")
        return final_docs, timing

    async def retrieve_context(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        history_context: Optional[str] = None,
        tenant_id: str = "tara"
    ) -> Dict[str, Any]:
        """
        Retrieve all relevant context (Docs, Hive Mind, Web) without generating an answer.
        Returns a dictionary with query_english, relevant_docs, hive_mind_context, web_results, and timing.
        """
        start_time = time.time()
        timing = {}
        
        # 1. High-Speed Detection & Translation Layer
        query_english = query
        original_language = "en"
        if context and 'language' in context:
            original_language = context['language'].lower()
        
        # Fast local language detection (avoid LLM for simple cases)
        if original_language not in ['de', 'german', 'deutsch', 'te', 'telugu']:
            german_indicators = ['wie ', 'was ', 'ist ', 'der ', 'die ', 'das ', 'mit ']
            telugu_indicators = ['ఎక్కడ', 'ఎలా', 'ఏమిటి', 'చెప్పు', 'ఉంది', 'చేయాలి']
            if any(ind in query.lower() for ind in german_indicators):
                original_language = 'german'
            elif any(ind in query.lower() for ind in telugu_indicators):
                original_language = 'telugu'
        
        # Async Translation if needed (skip for obvious English)
        if original_language in ['de', 'german', 'deutsch'] and self.groq_client:
            try:
                # Only translate if it doesn't look like English
                if not any(e in query.lower() for e in ['what', 'how', 'who', 'pricing', 'install', 'tara']):
                    t_start = time.time()
                    loop = asyncio.get_event_loop()
                    chat = await loop.run_in_executor(None, lambda: self.groq_client.chat.completions.create(
                        messages=[{"role": "system", "content": "Translate to English. Output ONLY translation."}, {"role": "user", "content": query}],
                        model="llama-3.1-8b-instant",
                        temperature=0.1
                    ))
                    query_english = chat.choices[0].message.content.strip()
                    timing['translation_ms'] = (time.time() - t_start) * 1000
                    logger.info(f"✅ Translated: {query} -> {query_english}")
            except Exception as e:
                logger.error(f"Translation failed: {e}")

        # 2. ULTRA FAST-PATH: Static/Conversational queries
        query_clean = query_english.lower().strip()
        
        # Identity Fast-Path logic handled in process_query, but we return metadata here
        fast_path_type = None
        identity_keywords = ["who are you", "your name", "what are you", "who built you", "who created you", "built by", "created by", "work for", "your employer", "who is your boss"]
        if any(x in query_clean for x in identity_keywords):
            fast_path_type = "identity"
        elif "pricing" in query_clean or "cost" in query_clean:
            fast_path_type = "pricing"
        elif re.match(r'^(hi|hello|hey|thanks|thank you|morning|evening|greetings)', query_clean):
            fast_path_type = "conversational"

        # 2.5 Context-dependent query detection
        CONTEXT_DEPENDENT_PATTERNS = [
            r"what did i (just )?ask",
            r"what was (i|my) (previous )?question",
            r"what were we (talking|discussing) about",
            r"remember when i (said|asked|mentioned)",
            r"what did i (just )?say",
            r"go back to what i asked",
            r"referring to my previous",
            r"as i (said|mentioned|asked)",
            r"like i (said|mentioned|asked)",
        ]
        
        # Normalize history_context to string
        if isinstance(history_context, list):
            history_context = "\n".join([str(h) for h in history_context])
        history_context = str(history_context) if history_context else ""
        
        # SLIDING WINDOW: Limit context
        MAX_HISTORY_CHARS = 4000
        if len(history_context) > MAX_HISTORY_CHARS:
            cutoff = len(history_context) - MAX_HISTORY_CHARS
            trunc_index = history_context.find('\n', cutoff)
            if trunc_index != -1:
                history_context = "...[older context trimmed]...\n" + history_context[trunc_index+1:]
            else:
                history_context = "...[older context trimmed]...\n" + history_context[cutoff:]
            logger.info(f"✂️ History context trimmed to {len(history_context)} chars")

        is_context_dependent = any(re.search(pattern, query_clean) for pattern in CONTEXT_DEPENDENT_PATTERNS)
        
        # 2.6 Contextual Query Rewriting (The "Follow-up" Fix)
        # If the user asks "what about that?", we MUST rewrite it using history before retrieval.
        if is_context_dependent and self.groq_client and history_context:
            try:
                rw_start = time.time()
                # Use a very fast model for rewriting
                rewrite_prompt = (
                    f"Conversation History:\n{history_context[-2000:]}\n\n"
                    f"User's Last Input: {query_english}\n\n"
                    f"Task: Rewrite the User's Last Input to be a standalone search query that includes necessary context from the history. "
                    f"Do NOT answer the question. Just output the rewritten query string."
                )
                
                chat = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, lambda: self.groq_client.chat.completions.create(
                        messages=[{"role": "system", "content": "You are a Query Rewriter. Output ONLY the rewritten query. No preamble."}, {"role": "user", "content": rewrite_prompt}],
                        model="llama-3.1-8b-instant",
                        temperature=0.1,
                        max_tokens=60
                    )),
                    timeout=0.6
                )
                
                rewritten = chat.choices[0].message.content.strip()
                # Sanity check: don't use if it's too long or empty
                if rewritten and len(rewritten) < 200:
                    logger.info(f"🔄 Rewrote Query: '{query_english}' -> '{rewritten}' ({int((time.time()-rw_start)*1000)}ms)")
                    query_english = rewritten
                
            except Exception as e:
                logger.warning(f"⚠️ Query rewriting failed: {e}")
        
        # 3. PRIORITY: Skills & Rules retrieval FIRST (Sequential)
        # This ensures we know what capabilities TARA has before doing other retrievals
        retrieval_start = time.time()
        
        # PRE-COMPUTE EMBEDDING ONCE (Optimization: save CPU and prevent redundant work)
        shared_vector = None
        if self.embeddings and (self.config.enable_local_retrieval or (self.qdrant and self.qdrant.enabled)):
            v_start = time.time()
            v_list = self.embeddings.embed_query(query_english)
            shared_vector = np.array(v_list, dtype=np.float32).reshape(1, -1)
            timing['shared_embedding_ms'] = (time.time() - v_start) * 1000
        
        # STEP 1: Retrieve Skills & Rules FIRST (Sequential - Priority)
        # Detected pattern
        pattern = None if is_context_dependent else self._detect_query_pattern(query_english)
        boost_cats = pattern.get("faiss_boost", []) if pattern else []
        max_chars = pattern.get("max_context_chars", 2500) if pattern else 2500
        
        # STEP 2: Parallel Retrieval (Local RAG, Hive Mind, Web Search, Skills & Rules)
        tasks = []
        
        # Task 1: Local RAG (Now uses shared vector)
        tasks.append(self._do_local_rag(query_english, context or {}, boost_cats, max_chars, precomputed_embedding=shared_vector))
        
        # Task 2: Unified Qdrant Search (Case Memory, Skills, Rules, General KB)
        async def do_unified_search():
            if self.qdrant and self.qdrant.enabled:
                if shared_vector is not None:
                    try:
                        # Extract domain from context (URL)
                        domain = None
                        if context and "url" in context:
                            try:
                                from urllib.parse import urlparse
                                parsed = urlparse(context["url"])
                                domain = parsed.netloc.replace("www.", "")
                            except:
                                pass
                        
                        # Use unified search with doc_type filter
                        # "in case of a general wss connection... filter of doc_type in all ..."
                        target_types = ["Case_Memory", "Agent_Skill", "Agent_Rule", "General_KB"]
                        
                        v_raw = shared_vector.flatten().tolist()
                        unified_start = time.time()
                        
                        # Perform unified search
                        results = await asyncio.wait_for(
                            self.qdrant.search_unified_memory(
                                query_vector=v_raw,
                                tenant_id=tenant_id,
                                doc_types=target_types,
                                limit=8, # Slightly higher limit to get mix of types
                                score_threshold=0.35
                            ),
                            timeout=1.2 # Allow slightly more time for complex filter
                        )
                        
                        search_time = (time.time() - unified_start) * 1000
                        
                        # Post-process results into categories
                        processed = {
                            "hive_mind_text": "",
                            "skills": [],
                            "rules": [],
                            "general_kb": []
                        }
                        
                        hm_entries = []
                        for r in results:
                            dt = r.get("doc_type")
                            payload = r.get("payload", {})
                            
                            if dt == "Agent_Skill":
                                processed["skills"].append({"text": r["text"], "topic": payload.get("topic", "general"), "score": r["score"]})
                            elif dt == "Agent_Rule":
                                processed["rules"].append({"text": r["text"], "topic": payload.get("topic", "general"), "severity": payload.get("severity", "standard"), "score": r["score"]})
                            elif dt == "Case_Memory":
                                # Format Case Memory as Issue/Solution pairs
                                issue = payload.get("issue", r["text"])
                                solution = payload.get("solution", r["summary"])
                                hm_entries.append(f"Issue: {issue}\nSolution: {solution}")
                            elif dt == "General_KB":
                                # Format General KB as context chunks
                                source = payload.get("filename", "Internal Doc")
                                text = r["text"]
                                hm_entries.append(f"[{source}]: {text}")
                        
                        if hm_entries:
                            processed["hive_mind_text"] = "\n---\n".join(hm_entries)
                            
                        # Logging
                        total_chunks = len(hm_entries) + len(processed["skills"]) + len(processed["rules"]) + len(processed.get("general_kb", []))
                        if total_chunks > 0:
                            total_chars = sum(len(e) for e in hm_entries) + sum(len(s["text"]) for s in processed["skills"]) + sum(len(r["text"]) for r in processed["rules"])
                            logger.info(f"🧠 HiveMind retrieval: {total_chunks} chunks ({total_chars} chars)")


                        return processed, search_time
                        
                    except asyncio.TimeoutError:
                        logger.warning("⏱️ Unified Qdrant search TIMEOUT (>1.2s) - skipping")
                    except Exception as e:
                        logger.error(f"❌ Unified Qdrant search error: {e}")
                else:
                    logger.warning("🧠 Qdrant enabled but embeddings not initialized")
            
            return {"hive_mind_text": "", "skills": [], "rules": [], "general_kb": []}, 0

        tasks.append(do_unified_search())
        
        # Task 3: Web Search
        async def do_web():
            if self.config.enable_web_search and any(k in query_clean for k in ['news', 'latest', 'recent', 'price', 'cost']):
                logger.info(f"🌐 Web Search triggered for: {query_clean[:30]}...")
                return await self._perform_web_search(query_english)
            return ""
        tasks.append(do_web())
        
        # Execute parallel tasks
        retrieval_results = await asyncio.gather(*tasks)
        
        relevant_docs, doc_timing = retrieval_results[0]
        
        # Unpack Unified Results
        unified_data, unified_timing = retrieval_results[1]
        hive_mind_context = unified_data["hive_mind_text"]
        
        # Unpack Web Results
        web_results = retrieval_results[2]
        
        # Pack final skills/rules/timing
        skills_rules = {
            "skills": unified_data["skills"],
            "rules": unified_data["rules"]
        }
        timing['qdrant_unified_ms'] = unified_timing


        timing.update(doc_timing)
        timing['retrieval_ms'] = (time.time() - retrieval_start) * 1000

        return {
            "query_original": query,
            "query_english": query_english,
            "original_language": original_language,
            "relevant_docs": relevant_docs,
            "hive_mind_context": hive_mind_context,
            "web_results": web_results,
            "agent_skills": skills_rules.get("skills", []),
            "agent_rules": skills_rules.get("rules", []),
            "general_kb": unified_data.get("general_kb", []),
            "timing": timing,
            "fast_path_type": fast_path_type,
            "history_context": history_context
        }

    async def process_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        streaming_callback: Optional[Callable[[str, bool], None]] = None,
        history_context: Optional[str] = None,
        tenant_id: str = "tara",
        force_non_stream: bool = False,
        generation_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """High-performance RAG pipeline with parallel retrieval and FAST-PATH."""
        start_time = time.time()
        
        # 1-3. Retrieve Context
        retrieval_data = await self.retrieve_context(query, context, history_context, tenant_id=tenant_id)
        
        query_english = retrieval_data['query_english']
        original_language = retrieval_data['original_language']
        relevant_docs = retrieval_data['relevant_docs']
        hive_mind_context = retrieval_data['hive_mind_context']
        web_results = retrieval_data['web_results']
        agent_skills = retrieval_data.get('agent_skills', [])
        agent_rules = retrieval_data.get('agent_rules', [])
        general_kb = retrieval_data.get('general_kb', [])
        timing = retrieval_data['timing']
        fast_path_type = retrieval_data['fast_path_type']
        
        # 3.5 Language Context Strategy
        # Detect explicit intent to switch response language
        combined_context_text = (str(history_context) + " " + query).lower()
        if any(x in combined_context_text for x in ["speak in telugu", "speak telugu", "తెలుగులో మాట్లాడు", "telugu"]):
            logger.info("🌐 User requested Telugu response (explicit intent detected)")
            original_language = "telugu"
        elif any(x in combined_context_text for x in ["speak in english", "speak english"]):
            logger.info("🌐 User requested English response (explicit intent detected)")
            original_language = "english"
        
        # 2. ULTRA FAST-PATH (Execution)
        query_clean = query_english.lower().strip()
        
        if fast_path_type == "identity":
            org = self.config.organization_name
            response = (
                f"I am TARA, an advanced AI Assistant built by Davinci AI for {org}. "
                "Just to clarify: I work for the organization TASK (Telangana Academy for Skill and Knowledge), not to perform generic tasks. "
                "I'm here to help you with skilling, employment, and career development opportunities!"
            )
            if original_language == "telugu":
                response = f"నేను TARA, {org} కోసం Davinci AI ద్వారా నిర్మించబడిన అధునాతన AI సహాయకుడిని. మీకు నైపుణ్యాభివృద్ధి, ఉపాధి మరియు కెరీర్ అవకాశాలలో సహాయం చేయడానికి నేను ఇక్కడ ఉన్నాను!"
            if streaming_callback: streaming_callback(response, True)
            duration = (time.time() - start_time) * 1000
            return {
                "answer": response,
                "sources": [],
                "confidence": 1.0,
                "timing_breakdown": {"total_ms": duration},
                "metadata": {"method": "fast-path", "type": "identity"}
            }

        if fast_path_type == "pricing":
            response = (
                "TASK services are primarily government-funded and focused on skilling and employment enhancement. "
                "Registration fees are nominal: ₹500 + GST for SC/ST students and ₹1000 + GST for others (valid for 4 years). "
                "You can access various skilling courses, placement drives, and internships through this membership."
            )
            if streaming_callback: streaming_callback(response, True)
            duration = (time.time() - start_time) * 1000
            return {
                "answer": response,
                "sources": [],
                "confidence": 1.0,
                "timing_breakdown": {"total_ms": duration},
                "metadata": {"method": "fast-path", "type": "pricing"}
            }

        if fast_path_type == "conversational":
            return await self._handle_conversational_fast_path(query_clean, timing, streaming_callback, original_language)


        # Prepare Context Data
        # Build Hive Mind state with team knowledge and web results
        hive_mind_state = {"insights": {}, "variables": {}}
        if hive_mind_context:
            hive_mind_state["insights"]["team_solutions"] = str(hive_mind_context)
        if web_results:
            hive_mind_state["insights"]["live_web_data"] = str(web_results)
        
        # User profile for personalization
        user_profile = {
            "language": original_language,
            "session_type": "technical_support"
        }

        # Build history list
        history_list = []
        if history_context and isinstance(history_context, str) and history_context.strip():
            # Naive parsing of history context string into list of turns if needed
            # For now, we'll wrap the whole block as a single turn if it's unstructured
            history_list.append({"role": "user", "content": history_context})
        elif isinstance(history_context, list):
            history_list = history_context

        # 4. Context Architecture (Zoned XML Assembly)
        # Using the new ContextArchitect for <500ms TTFT and robust context boundaries
        # Zone D: Dynamic skills/rules retrieved from Qdrant HiveMind
        skill_texts = [s.get("text", "") for s in agent_skills if s.get("text")]
        rule_texts = [r.get("text", "") for r in agent_rules if r.get("text")]

        prompt = ContextArchitect.assemble_prompt(
            query=query_english,
            raw_query=query,
            retrieved_docs=relevant_docs,
            history=history_list,
            hive_mind=hive_mind_state,
            user_profile=user_profile,
            agent_skills=skill_texts,
            agent_rules=rule_texts
        )
        prompt = self._apply_prompt_budget(prompt, str(getattr(self.config, "llm_model", "")))

        # 4.5 Generation controls (endpoint-tunable)
        gen_cfg = generation_config or {}
        generation_temperature = float(gen_cfg.get("temperature", 0.65))
        generation_max_tokens = int(gen_cfg.get("max_tokens", 320))
        generation_stop = gen_cfg.get("stop")
        if isinstance(generation_stop, str):
            generation_stop = [generation_stop]
        if not isinstance(generation_stop, list):
            generation_stop = None
        
        # 5. Generation
        gen_start = time.time()
        accumulated = ""
        first_chunk_at = None
        
        try:
            # Log the full prompt for debugging 0-token issues
            logger.debug(f"DEBUG PROMPT: {prompt}")
            
            # Force non-stream for GPT-OSS to avoid empty delta-content behavior.
            llm_model_name = str(getattr(self.config, "llm_model", "")).lower()
            use_stream = (not force_non_stream) and ("gpt-oss" not in llm_model_name)
            result = self.llm.generate(
                prompt=prompt,
                stream=use_stream,
                temperature=generation_temperature,
                max_tokens=generation_max_tokens,
                include_reasoning=False,
                reasoning_effort="low",
                stop=generation_stop
            )
            llm_usage = {}  # Capture token usage from LLM provider
            
            # Handle Async Generator (Groq Async Streaming)
            if hasattr(result, '__aiter__'):
                async for chunk in result:
                    # Check for usage sentinel (dict with __usage__ key)
                    if isinstance(chunk, dict) and "__usage__" in chunk:
                        llm_usage = chunk["__usage__"]
                        continue
                    if first_chunk_at is None: first_chunk_at = time.time()
                    text = chunk if isinstance(chunk, str) else getattr(chunk, 'text', str(chunk))
                    accumulated += text
                    if streaming_callback: streaming_callback(text, False)
            
            # Handle Sync Generator (Gemini/OpenAI Streaming)
            elif hasattr(result, '__iter__'):
                for chunk in result:
                    if isinstance(chunk, dict) and "__usage__" in chunk:
                        llm_usage = chunk["__usage__"]
                        continue
                    if first_chunk_at is None: first_chunk_at = time.time()
                    text = chunk if isinstance(chunk, str) else getattr(chunk, 'text', str(chunk))
                    accumulated += text
                    if streaming_callback: streaming_callback(text, False)
            
            # Handle Coroutine (Non-streaming Async)
            elif asyncio.iscoroutine(result):
                accumulated = await result
                if first_chunk_at is None: first_chunk_at = time.time()
                if streaming_callback: streaming_callback(accumulated, False)
            
            # Handle direct string
            else:
                accumulated = str(result)
                if first_chunk_at is None: first_chunk_at = time.time()
                if streaming_callback: streaming_callback(accumulated, False)
            
            # Check for empty response and provide a fallback to prevent TTS errors
            if not accumulated.strip():
                logger.warning(f"⚠️ LLM returned empty response for query: '{query}'")
                # Provide a natural fallback response
                accumulated = "I'm sorry, I couldn't process that. Could you please repeat or rephrase your question?"
                if streaming_callback: streaming_callback(accumulated, False)

            # Non-streaming providers may store usage out-of-band.
            if not llm_usage and hasattr(self.llm, "get_last_usage"):
                try:
                    llm_usage = self.llm.get_last_usage() or {}
                except Exception:
                    llm_usage = {}

            if streaming_callback: streaming_callback("", True)
            
        except Exception as e:
            logger.error(f"Generation failed: {e}")
            # Fallback (handle both sync and async)
            fallback_result = self.llm.generate(
                prompt=prompt,
                stream=False,
                temperature=generation_temperature,
                max_tokens=generation_max_tokens,
                include_reasoning=False,
                reasoning_effort="low",
                stop=generation_stop
            )
            if asyncio.iscoroutine(fallback_result):
                accumulated = await fallback_result
            else:
                accumulated = str(fallback_result)

            if not llm_usage and hasattr(self.llm, "get_last_usage"):
                try:
                    llm_usage = self.llm.get_last_usage() or {}
                except Exception:
                    llm_usage = {}
                
            if streaming_callback: streaming_callback(accumulated, True)
            
        timing['generation_ms'] = (time.time() - gen_start) * 1000
        if first_chunk_at: timing['ttfc_ms'] = (first_chunk_at - gen_start) * 1000
        
        # 6. Post-process (strip XML artifacts if model outputs them)
        answer = accumulated.strip()
        # Remove any XML closing tags the model might output (matching minified tags)
        answer = re.sub(r'</resp>.*', '', answer, flags=re.DOTALL).strip()
        answer = re.sub(r'</turn>.*', '', answer, flags=re.DOTALL).strip()
        answer = re.sub(r'</ctxt>.*', '', answer, flags=re.DOTALL).strip()
        
        timing['total_ms'] = (time.time() - start_time) * 1000
        self.query_count += 1
        self.total_query_time += timing['total_ms']
        
        # Log the complete response for visibility
        logger.info(f"📤 RAG Response for query '{query[:50]}...':")
        logger.info(f"   Answer: {answer[:200]}{'...' if len(answer) > 200 else ''}")
        logger.info(f"   Timing: {timing['total_ms']:.1f}ms (TTFC: {timing.get('ttfc_ms', 0):.1f}ms)")
        logger.info(f"   Language: {original_language} | Sources: {len(relevant_docs)} docs")
        if llm_usage:
            prompt_tokens = llm_usage.get("prompt_tokens", 0) or 0
            cached_tokens = llm_usage.get("cached_tokens", 0) or 0
            if prompt_tokens > 0:
                cache_hit_pct = (cached_tokens / prompt_tokens) * 100.0
                logger.info(f"   Prompt Cache: {cached_tokens}/{prompt_tokens} ({cache_hit_pct:.1f}%)")
        
        return {
            'answer': answer,
            'sources': list(set(
                [d['metadata'].get('source', 'Unknown') for d in relevant_docs] + 
                (["Hive Mind (Team Knowledge)"] if hive_mind_context else []) +
                (["Web Search"] if web_results else [])
            )),
            'confidence': 0.8,
            'timing_breakdown': timing,
            'llm_usage': llm_usage,
            'metadata': {
                'method': 'xml-rag',
                'language': original_language,
                'zones_used': ['A', 'B', 'C'] + (['D'] if skill_texts or rule_texts else []),
                'hive_mind_used': bool(hive_mind_context),
                'web_search_used': bool(web_results),
                'active_skills': len(skill_texts),
                'active_rules': len(rule_texts),
                'llm_usage': llm_usage,
                'raw_chunks': [
                   {"id": d.get("id", "unknown"), "text": d.get("text", ""), "score": d.get("score", 0), "doc_type": d.get("doc_type", "Skill/Rule")} 
                   for d in (agent_skills + agent_rules + general_kb) if d.get("id")
                ] + [
                   {"id": d.get("metadata", {}).get("id", "unknown"), "text": d.get("text", ""), "score": d.get("boosted_similarity", 0), "doc_type": "Static_Doc"}
                   for d in relevant_docs if d.get("metadata", {}).get("id")
                ]
            }
        }

    async def _handle_conversational_fast_path(self, query: str, timing: Dict, callback: Optional[Callable], language: str) -> Dict:
        """Handle greetings and thanks without RAG."""
        start = time.time()
        
        org = self.config.organization_name
        system_prompt = (
            f"You are TARA, an empathetic AI Colleague at {org}. "
            f"You are speaking in {language}. Be warm, professional, and helpful. "
            f"Acknowledge the user's greeting or thanks naturally. "
            f"Keep it to 1-2 sentences."
        )
        
        # Handle async/sync LLM
        result = self.llm.generate(prompt=system_prompt + "\n\nUser: " + query, stream=False)
        if asyncio.iscoroutine(result):
            answer = await result
        else:
            answer = str(result)
            
        if callback:
            callback(answer, True)
        
        timing['total_ms'] = (time.time() - start) * 1000
        return {
            'answer': answer,
            'sources': [],
            'confidence': 1.0,
            'timing_breakdown': timing,
            'metadata': {'method': 'fast-path', 'type': 'conversational'}
        }

    async def _perform_web_search(self, query: str) -> str:
        """Simple web search wrapper."""
        if not self.config.google_search_api_key: return ""
        try:
            # Import here to avoid global dependency issues
            from googleapiclient.discovery import build
            service = build("customsearch", "v1", developerKey=self.config.google_search_api_key)
            res = service.cse().list(q=query, cx=self.config.google_cse_id, num=3).execute()
            results = []
            for item in res.get('items', []):
                results.append(f"Source: {item['link']}\nSnippet: {item.get('snippet', '')}")
            return "\n\n".join(results)
        except Exception as e:
            logger.warning(f"Web search failed: {e}")
            return ""

    async def distill_history_to_case(self, history: str) -> List[Dict[str, str]]:
        """
        Distill conversation history into professional support cases for the Organization.
        Supports multi-issue segmentation and GDPR/PII anonymization.
        Uses the CaseDistiller for robust prompt handling.
        """
        try:
            prompt = CaseDistiller.get_prompt(history)
            
            import inspect
            # Call generate - may return string, coroutine, or generator
            result = self.llm.generate(prompt=prompt, stream=False)
            
            # If it's a coroutine (async provider like Groq), await it
            if inspect.iscoroutine(result):
                response = await result
            else:
                response = result
            
            # Use CaseDistiller to extract list of cases from response
            cases = CaseDistiller.clean_json_response(str(response))
            
            if cases:
                logger.info(f"✅ Distilled {len(cases)} cases from history")
                return cases
            
            logger.warning(f"No valid cases found in distillation response: {str(response)[:100]}")
            return []
        except Exception as e:
            logger.error(f"Distillation failed: {e}")
            return []

    async def generate_dynamic_exit(self, history: str, language: str = "english") -> str:
        """Generate a dynamic summary and closing query for the session."""
        try:
            # Map shorthand language codes
            lang_full = language
            if language.lower() in ("en", "eng"): lang_full = "english"
            elif language.lower() in ("de", "deu", "ger"): lang_full = "german"
            elif language.lower() in ("te", "tel"): lang_full = "telugu"

            org = self.config.organization_name
            prompt = (
                f"<system_configuration>\n"
                f"You are TARA, a caring and professional AI Colleague for {org}. "
                f"The user is ending the session. Your task is to briefly summarize what happened (if useful) "
                f"and then ask if there's anything else the user needs help with.\n"
                f"RULES:\n"
                f"1. Be warm, human-like, and professional.\n"
                f"2. Summarize the LAST few actions briefly if detectable.\n"
                f"3. End with a polite closing question.\n"
                f"4. TOTAL LENGTH: MAX 2-3 SENTENCES.\n"
                f"5. Language: {lang_full}.\n"
                f"</system_configuration>\n\n"
                f"Conversation History:\n{history[-3000:]}\n\n"
                f"Generate the closing statement (speech only, no tags):"
            )
            
            import inspect
            result = self.llm.generate(prompt=prompt, stream=False)
            if inspect.iscoroutine(result):
                response = await result
            else:
                response = result
            
            return str(response).strip()
        except Exception as e:
            logger.error(f"Dynamic exit generation failed: {e}")
            org = self.config.organization_name
            if language.lower().startswith("tel"):
                return f"{org} సేవలను ఉపయోగించినందుకు ధన్యవాదాలు. ఈ రోజు నేను మీకు ఇంకా ఏమైనా సహాయం చేయగలనా?" 
            return f"Thank you for using {org}. Is there anything else I can help you with today?"

    def validate_response_quality(self, response: str) -> Dict[str, Any]:

        """Human-readable evaluation of response."""
        return {'quality_score': 1.0, 'issues': []}

    def get_performance_stats(self) -> Dict[str, Any]:
        """Metrics monitoring."""
        return {
            'total_queries': self.query_count,
            'avg_latency_ms': self.total_query_time / self.query_count if self.query_count > 0 else 0,
            'docs_count': len(self.documents)
        }
    
    def _extract_template_fields(self, docs: list, pattern: Dict) -> Dict:
         # Simplified extraction for Daytona
         return {}

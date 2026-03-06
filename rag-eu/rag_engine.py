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
import importlib
from typing import Dict, Any, Optional, List, Callable, Union
import numpy as np
faiss = None
def _missing_dep(*args, **kwargs):
    raise RuntimeError("Required RAG dependency is not installed in this environment")


try:
    from .context_architecture import ContextArchitect
except Exception:
    try:
        from context_architecture import ContextArchitect
    except Exception:
        ContextArchitect = None

try:
    from .llm_providers import create_provider, create_fallback_provider
except Exception:
    try:
        from llm_providers import create_provider, create_fallback_provider
    except Exception:
        create_provider = _missing_dep
        create_fallback_provider = _missing_dep

try:
    from .config import RAGConfig
except Exception:
    try:
        from config import RAGConfig
    except Exception:
        RAGConfig = Any

try:
    from .prompts import prompt_manager
except Exception:
    try:
        from prompts import prompt_manager
    except Exception:
        prompt_manager = None

try:
    from .optimized_embeddings import OptimizedEmbeddings
except Exception:
    try:
        from optimized_embeddings import OptimizedEmbeddings
    except Exception:
        OptimizedEmbeddings = None

try:
    from .xml_prompts import XMLPromptManager
except Exception:
    try:
        from xml_prompts import XMLPromptManager
    except Exception:
        XMLPromptManager = None

try:
    from .qdrant_addon import QdrantAddon
except Exception:
    try:
        from qdrant_addon import QdrantAddon
    except Exception:
        QdrantAddon = None

try:
    from .distillprompt_hivemind_savecase import CaseDistiller
except Exception:
    try:
        from distillprompt_hivemind_savecase import CaseDistiller
    except Exception:
        CaseDistiller = None

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
        self._tenant_architect_cache: Dict[str, Any] = {}
        
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

        # Storage - Local Indexing Disabled (Using Qdrant Hivemind exclusively)
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
    
    def load_index(self) -> bool:
        """Deprecated: Local FAISS index loading removed. Relying on Qdrant Hivemind."""
        return True

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

    def _build_org_safe_prompt(
        self,
        query: str,
        language: str,
        history: List[Dict[str, Any]],
        relevant_docs: List[Dict[str, Any]],
        agent_skills: List[str],
        agent_rules: List[str]
    ) -> str:
        """Build a tenant/org-safe prompt when local retrieval is disabled or sparse."""
        org = (self.config.organization_name or "the organization").strip()
        location = (self.config.organization_location or "").strip()
        lang = (language or "english").strip().lower()

        history_lines = []
        for turn in history[-6:]:
            role = str(turn.get("role", "user")).strip()
            content = str(turn.get("content", "")).strip()
            if content:
                history_lines.append(f"{role}: {content}")
        history_block = "\n".join(history_lines) if history_lines else "(none)"

        docs_block = "\n".join(
            f"- {str(d.get('text', '')).strip()[:300]}"
            for d in relevant_docs[:6]
            if str(d.get("text", "")).strip()
        ) or "(none)"
        skills_block = "\n".join(f"- {s[:220]}" for s in agent_skills[:10]) or "(none)"
        rules_block = "\n".join(f"- {r[:220]}" for r in agent_rules[:10]) or "(none)"

        return (
            f"You are TARA, the AI assistant for {org}"
            f"{f' ({location})' if location else ''}.\n"
            f"Language for this reply: {lang}.\n"
            "Identity guardrails:\n"
            f"- You must identify ONLY as assistant for {org}.\n"
            "- Never claim to belong to TASK, B&B, or any other organization unless explicitly present in provided context documents.\n"
            "- If information is missing, say so briefly and ask a clarifying question.\n"
            "- Keep answers concise, accurate, and helpful.\n\n"
            f"Conversation history:\n{history_block}\n\n"
            f"Retrieved context docs:\n{docs_block}\n\n"
            f"Agent skills:\n{skills_block}\n\n"
            f"Agent rules:\n{rules_block}\n\n"
            f"User query: {query}\n"
            "Answer:"
        )

    @staticmethod
    def _sanitize_tenant(tenant_id: Optional[str]) -> str:
        tenant = (tenant_id or "default").strip().lower()
        return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in tenant)

    def _resolve_context_architect(self, tenant_id: Optional[str]):
        """
        Resolve tenant-specific context architecture module/class.

        Env override keys:
        - <tenant>_context_architecture_module (e.g. context_architecture_davinci)
        - <TENANT>_CONTEXT_ARCHITECTURE_MODULE
        - <tenant>_context_architecture_class (optional, default ContextArchitect)
        """
        tenant = self._sanitize_tenant(tenant_id)
        if tenant in self._tenant_architect_cache:
            return self._tenant_architect_cache[tenant]

        module_env = (
            os.getenv(f"{tenant}_context_architecture_module")
            or os.getenv(f"{tenant.upper()}_CONTEXT_ARCHITECTURE_MODULE")
        )
        class_name = (
            os.getenv(f"{tenant}_context_architecture_class")
            or os.getenv(f"{tenant.upper()}_CONTEXT_ARCHITECTURE_CLASS")
            or "ContextArchitect"
        )

        candidates: List[str] = []
        if module_env:
            candidates.append(module_env)
        candidates.extend([
            f"context_architecture.context_architecture_{tenant}",
            f"context_architecture_{tenant}",
            "context_architecture.context_architecture",
            "context_architecture",
        ])

        architect_cls = None
        for module_name in candidates:
            try:
                mod = None
                if module_name.startswith("."):
                    mod = importlib.import_module(module_name, package=__package__)
                elif "." in module_name:
                    mod = importlib.import_module(module_name)
                else:
                    # Try package-relative first, then top-level import.
                    if __package__:
                        try:
                            mod = importlib.import_module(f".{module_name}", package=__package__)
                        except Exception:
                            mod = importlib.import_module(module_name)
                    else:
                        mod = importlib.import_module(module_name)

                if hasattr(mod, class_name):
                    architect_cls = getattr(mod, class_name)
                    logger.info(f"🧩 Context architect tenant={tenant} -> {module_name}.{class_name}")
                    break
            except Exception:
                continue

        if architect_cls is None:
            architect_cls = ContextArchitect
            logger.warning(f"⚠️ Using default ContextArchitect for tenant={tenant}")

        self._tenant_architect_cache[tenant] = architect_cls
        return architect_cls

    async def _do_local_rag(self, query: str, context: Dict, boost_categories: list, max_chars: int, precomputed_embedding: Optional[np.ndarray] = None) -> tuple:
        """Deprecated: Local FARSS index retrieval removed. Always returns empty docs."""
        return [], {"embedding_ms": 0, "search_ms": 0}

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

        # No fast-path bypasses: all responses must flow through full context architecture.
        query_clean = query_english.lower().strip()
        fast_path_type = None

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

        # Fail fast with a clear response if LLM initialization failed at startup.
        if self.llm is None:
            logger.error("LLM provider is not initialized (self.llm is None). Check LLM_API_KEY/GROQ_API_KEY and provider config.")
            msg = "I am currently unavailable due to an LLM configuration issue. Please try again in a moment."
            if streaming_callback:
                streaming_callback(msg, True)
            return {
                "answer": msg,
                "sources": [],
                "confidence": 0.0,
                "timing_breakdown": {"total_ms": (time.time() - start_time) * 1000},
                "llm_usage": {},
                "metadata": {"method": "error", "reason": "llm_not_initialized"},
            }
        
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

        architect_cls = self._resolve_context_architect(tenant_id)
        prompt = architect_cls.assemble_prompt(
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
            if self.llm is None:
                msg = "I am currently unavailable due to an LLM configuration issue. Please try again in a moment."
                if streaming_callback:
                    streaming_callback(msg, True)
                timing['generation_ms'] = (time.time() - gen_start) * 1000
                timing['total_ms'] = (time.time() - start_time) * 1000
                return {
                    "answer": msg,
                    "sources": list(set([d['metadata'].get('source', 'Unknown') for d in relevant_docs])),
                    "confidence": 0.0,
                    "timing_breakdown": timing,
                    "llm_usage": {},
                    "metadata": {"method": "error", "reason": "llm_not_initialized_post_generation"},
                }
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

    # ═══════════════════════════════════════════════════════════════════════════════
    # FSM Routing Logic (Schema-Driven Appointment Booking)
    # ═══════════════════════════════════════════════════════════════════════════════

    async def route_fsm_turn(
        self,
        user_text: str,
        session_id: str,
        tenant_id: str,
        language: str,
        fsm_context: Dict[str, Any],
        history_context: Optional[Union[str, List[Dict[str, Any]]]] = None
    ) -> Dict[str, Any]:
        """
        Route FSM turn during appointment booking flow.
        
        Decision order:
        1. Cancel detection (strong lexical + confidence gate) -> cancel
        2. Field-answer classification for pending field -> collect_field/confirm_field
        3. Detour classification (general RAG query) -> detour_rag
        4. Fallback -> invalid_retry
        
        Args:
            user_text: User input text
            session_id: Session identifier
            tenant_id: Tenant identifier
            language: Response language
            fsm_context: Current FSM state (active, pending_field, collected_data, retry_counts, schema)
            history_context: Optional conversation history
            
        Returns:
            Dict with: action, field, normalized_value, confidence, reason, resume_prompt, cancelled
        """
        import re
        import time
        start_time = time.time()
        
        # Extract FSM context
        active = fsm_context.get('active', False)
        pending_field = fsm_context.get('pending_field')
        collected_data = fsm_context.get('collected_data', {})
        retry_counts = fsm_context.get('retry_counts', {})
        schema = fsm_context.get('schema', {})
        
        # Get schema configuration
        fields_schema = schema.get('fields', {}) if isinstance(schema, dict) else {}
        cancel_keywords = schema.get('cancel_keywords', ['cancel', 'stop', 'nevermind', 'forget it', 'quit', 'exit']) if isinstance(schema, dict) else []
        max_retries = schema.get('max_retries', 3) if isinstance(schema, dict) else 3
        
        user_text_lower = user_text.lower().strip()
        user_text_clean = user_text.strip()
        
        logger.info(f"🔀 FSM Route | Pending field: {pending_field} | Text: '{user_text[:50]}...'")
        
        # =========================================================================
        # 1. CANCEL DETECTION (Strong lexical gate)
        # =========================================================================
        cancel_confidence = 0.0
        for keyword in cancel_keywords:
            if keyword.lower() in user_text_lower:
                cancel_confidence = 0.95
                break
        
        # Additional cancel patterns
        cancel_patterns = [
            r"\b(cancel|stop|quit|exit|abort|end)\b",
            r"\b(nevermind|never mind|forget it|forget about it)\b",
            r"\b(i don't want|i do not want|i don't need|i do not need)\b",
            r"\b(no thanks|no thank you|not interested)\b",
        ]
        for pattern in cancel_patterns:
            if re.search(pattern, user_text_lower):
                cancel_confidence = max(cancel_confidence, 0.9)
        
        if cancel_confidence >= 0.85:
            logger.info(f"✅ FSM Route | CANCEL detected (confidence={cancel_confidence:.2f})")
            return {
                'action': 'cancel',
                'field': None,
                'normalized_value': None,
                'confidence': cancel_confidence,
                'reason': 'Cancel keyword detected',
                'resume_prompt': None,
                'cancelled': True
            }
        
        # =========================================================================
        # 2. FIELD-ANSWER CLASSIFICATION (Pending field validation)
        # =========================================================================
        if pending_field and pending_field in fields_schema:
            field_config = fields_schema[pending_field]
            field_result = self._classify_field_answer(
                user_text_clean,
                pending_field,
                field_config,
                collected_data
            )
            
            if field_result['is_valid']:
                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(f"✅ FSM Route | FIELD ANSWER | Field: {pending_field} | Value: '{field_result['normalized_value'][:30]}...' (confidence={field_result['confidence']:.2f}, {elapsed_ms:.0f}ms)")
                return {
                    'action': 'collect_field' if field_result.get('requires_confirm', True) else 'confirm_field',
                    'field': pending_field,
                    'normalized_value': field_result['normalized_value'],
                    'confidence': field_result['confidence'],
                    'reason': field_result['reason'],
                    'resume_prompt': None,
                    'cancelled': False
                }
        
        # =========================================================================
        # 3. DETOUR CLASSIFICATION (General RAG query)
        # =========================================================================
        # Check if this looks like a general question (not a field answer)
        is_question = any([
            user_text_clean.endswith('?'),
            user_text_lower.startswith(('what', 'how', 'why', 'when', 'where', 'who', 'can', 'could', 'would', 'is', 'are', 'do', 'does')),
            re.search(r'\b(help|information|explain|tell me about|what about)\b', user_text_lower),
        ])
        
        # Check if this is NOT a field answer (too long, no field-like patterns)
        is_not_field_answer = (
            len(user_text_clean) > 50 or  # Field answers are usually short
            (pending_field == 'name' and not re.match(r'^[A-Za-z\s\-\'\.\,]+$', user_text_clean)) or
            (pending_field == 'email' and '@' not in user_text_clean and not re.match(r'^[A-Za-z\s\-]+$', user_text_clean)) or
            (pending_field == 'topic' and len(user_text_clean) < 5)  # Too short for topic
        )
        
        if is_question and is_not_field_answer:
            elapsed_ms = (time.time() - start_time) * 1000
            resume_prompt = self._build_resume_prompt(pending_field, fields_schema, collected_data)
            
            logger.info(f"🔄 FSM Route | DETOUR RAG | Resume prompt: '{resume_prompt[:50]}...' ({elapsed_ms:.0f}ms)")
            return {
                'action': 'detour_rag',
                'field': pending_field,
                'normalized_value': None,
                'confidence': 0.85,
                'reason': 'General question detected during FSM flow',
                'resume_prompt': resume_prompt,
                'cancelled': False
            }
        
        # =========================================================================
        # 4. FALLBACK: Invalid retry
        # =========================================================================
        elapsed_ms = (time.time() - start_time) * 1000
        logger.warning(f"⚠️ FSM Route | INVALID RETRY | No pattern matched ({elapsed_ms:.0f}ms)")
        return {
            'action': 'invalid_retry',
            'field': pending_field,
            'normalized_value': None,
            'confidence': 0.5,
            'reason': 'Input does not match expected patterns',
            'resume_prompt': self._build_resume_prompt(pending_field, fields_schema, collected_data),
            'cancelled': False
        }
    
    def _classify_field_answer(
        self,
        user_text: str,
        field_name: str,
        field_config: Dict[str, Any],
        collected_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Classify and validate a field answer.
        
        Returns:
            Dict with: is_valid, normalized_value, confidence, reason, requires_confirm
        """
        import re
        
        user_text_lower = user_text.lower().strip()
        user_text_clean = user_text.strip()
        
        # Strip common prefixes
        prefixes = ["my name is", "i'm", "this is", "i am", "call me", "it's", "its", "name is", "my email is", "email:", "it is"]
        for prefix in prefixes:
            if user_text_lower.startswith(prefix):
                user_text_clean = user_text[len(prefix):].strip()
                user_text_lower = user_text_clean.lower()
                break
        
        # =======================================================================
        # NAME field validation
        # =======================================================================
        if field_name == 'name':
            # Handle spelled-out names like "A-M-A-R" or "A M A R"
            name_tokens = re.findall(r"[A-Za-z]+", user_text_clean)
            if name_tokens and len(name_tokens) >= 2 and all(len(token) == 1 for token in name_tokens):
                user_text_clean = ''.join(name_tokens).capitalize()
                user_text_lower = user_text_clean.lower()
            
            # Validate: at least 2 characters, only letters and spaces
            min_len = field_config.get('min_length', 2)
            max_len = field_config.get('max_length', 50)
            validation_regex = field_config.get('validation_regex', r'^[a-zA-Z\s\-\']+$')
            
            if len(user_text_clean) >= min_len and len(user_text_clean) <= max_len and re.match(validation_regex, user_text_clean):
                normalized = user_text_clean.title()
                return {
                    'is_valid': True,
                    'normalized_value': normalized,
                    'confidence': 0.9,
                    'reason': 'Valid name format',
                    'requires_confirm': True
                }
            
            return {
                'is_valid': False,
                'normalized_value': None,
                'confidence': 0.7,
                'reason': 'Invalid name format',
                'requires_confirm': False
            }
        
        # =======================================================================
        # EMAIL field validation
        # =======================================================================
        if field_name == 'email':
            # Parse spelled-out email
            email = self._parse_spelled_email(user_text_clean)
            
            # Email validation regex
            validation_regex = field_config.get('validation_regex', r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
            
            if re.match(validation_regex, email):
                return {
                    'is_valid': True,
                    'normalized_value': email.lower(),
                    'confidence': 0.95,
                    'reason': 'Valid email format',
                    'requires_confirm': True
                }
            
            return {
                'is_valid': False,
                'normalized_value': None,
                'confidence': 0.8,
                'reason': 'Invalid email format',
                'requires_confirm': False
            }
        
        # =======================================================================
        # TOPIC field validation
        # =======================================================================
        if field_name == 'topic':
            min_len = field_config.get('min_length', 5)
            max_len = field_config.get('max_length', 500)
            
            if len(user_text_clean) >= min_len and len(user_text_clean) <= max_len:
                return {
                    'is_valid': True,
                    'normalized_value': user_text_clean.strip(),
                    'confidence': 0.9,
                    'reason': 'Valid topic description',
                    'requires_confirm': True
                }
            
            return {
                'is_valid': False,
                'normalized_value': None,
                'confidence': 0.6,
                'reason': 'Topic too short or too long',
                'requires_confirm': False
            }
        
        # =======================================================================
        # Unknown field - accept as-is
        # =======================================================================
        return {
            'is_valid': True,
            'normalized_value': user_text_clean,
            'confidence': 0.5,
            'reason': f'Unknown field "{field_name}", accepting as-is',
            'requires_confirm': False
        }
    
    def _parse_spelled_email(self, email_input: str) -> str:
        """
        Parse spelled-out email like 'J-O-H-N at G-M-A-I-L dot C-O-M' to 'john@gmail.com'
        Also handles: 'john at gmail dot com', 'j o h n at gmail dot com'
        """
        import re
        
        text = email_input.lower().strip()
        
        # Replace spoken words with symbols
        text = re.sub(r'\s+at\s+', '@', text)
        text = re.sub(r'\s+dot\s+', '.', text)
        
        # Remove spaces, dashes, and other separators from spelled letters
        parts = text.split('@')
        if len(parts) == 2:
            # Clean user part (before @)
            user_part = re.sub(r'[\s\-,\.]+', '', parts[0])
            # Clean domain part (after @)
            domain_parts = parts[1].split('.')
            cleaned_domain = '.'.join(re.sub(r'[\s\-,]+', '', p) for p in domain_parts)
            text = f"{user_part}@{cleaned_domain}"
        else:
            # No @ found, try to clean the whole thing
            text = re.sub(r'[\s\-,]+', '', text)
        
        return text
    
    def _build_resume_prompt(
        self,
        pending_field: Optional[str],
        fields_schema: Dict[str, Any],
        collected_data: Dict[str, Any]
    ) -> Optional[str]:
        """
        Build the resume prompt for returning to FSM after detour.
        """
        if not pending_field or not fields_schema:
            return None
        
        field_config = fields_schema.get(pending_field, {})
        collect_prompt = field_config.get('collect_prompt', '')
        
        # Customize prompt based on field
        if pending_field == 'name':
            return "Back to booking - please spell out your name letter by letter."
        elif pending_field == 'email':
            return "Back to booking - please spell out your email address."
        elif pending_field == 'topic':
            return "Back to booking - what would you like to discuss with our expert?"
        elif collect_prompt:
            return f"Back to booking - {collect_prompt}"
        else:
            return f"Back to booking - please provide your {pending_field}."

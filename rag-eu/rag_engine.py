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
import inspect
from typing import Dict, Any, Optional, List, Callable, Union, Set
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
        self._warmed_tenants: Set[str] = set()
        
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

    def _prime_prompt_path(self, tenant_id: Optional[str]) -> None:
        tenant = self._sanitize_tenant(tenant_id)
        if tenant in self._warmed_tenants:
            return

        architect_cls = self._resolve_context_architect(tenant)
        modes = ["sales"]
        default_mode = str(getattr(self.config, "policy_mode_default", "sales") or "sales").strip().lower()
        if default_mode and default_mode not in modes:
            modes.append(default_mode)

        for mode in modes:
            architect_cls.assemble_prompt(
                query="warmup",
                raw_query="warmup",
                retrieved_docs=[],
                history=[],
                hive_mind={"insights": {}, "variables": {"policy": {"policy_mode": mode}}},
                user_profile={"language": "english", "tenant_id": tenant},
                agent_skills=[],
                agent_rules=[],
                interrupted_text=None,
                interruption_transcripts=None,
                interruption_type=None,
                user_id=None,
            )

        self._warmed_tenants.add(tenant)
    
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
    def _is_hivemind_browse_query(query: str) -> bool:
        query_lower = (query or "").strip().lower()
        patterns = [
            "what information do you have",
            "what information do u have",
            "what do you have",
            "what do u have",
            "what is in hivemind",
            "what's in hivemind",
            "show me the hivemind",
            "show me hivemind",
            "what all happened",
            "what happened in the last",
            "what happened last",
            "last 7 days",
            "last week",
            "past week",
            "recent conversations",
            "recent insights",
            "recent memories",
        ]
        return any(pattern in query_lower for pattern in patterns)

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
        lang = (language or "german").strip().lower()

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
            f"Mandatory language rule: write the full answer only in {lang}. Do not answer in English unless the user explicitly asks for English.\n"
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
    def _build_hivemind_dashboard_prompt(
        query: str,
        tenant_id: str,
        language: str,
        history: List[Dict[str, Any]],
        relevant_docs: List[Dict[str, Any]],
        case_memories: List[Dict[str, Any]],
        agent_skills: List[Dict[str, Any]],
        agent_rules: List[Dict[str, Any]],
        general_kb: List[Dict[str, Any]],
        system_prompt: Optional[str],
    ) -> str:
        """Dedicated system prompt for enterprise HiveMind dashboard chats."""
        lang = (language or "german").strip().lower()
        normalized_system_prompt = ""
        if system_prompt:
            logger.warning("Ignoring request-scoped HiveMind dashboard system_prompt to preserve tenant-safe prompt boundaries")

        history_lines = []
        for turn in history[-8:]:
            role = str(turn.get("role", "user")).strip()
            content = str(turn.get("content", "")).strip()
            if content:
                history_lines.append(f"{role}: {content[:500]}")
        history_block = "\n".join(history_lines) if history_lines else "(none)"

        def _format_entries(entries: List[Dict[str, Any]], limit: int, label: str) -> str:
            lines = []
            for entry in entries[:limit]:
                text = str(entry.get("text", "")).strip()
                score = entry.get("score")
                meta = []
                if entry.get("topic"):
                    meta.append(f"topic={entry.get('topic')}")
                if entry.get("doc_type"):
                    meta.append(f"type={entry.get('doc_type')}")
                if score is not None:
                    try:
                        meta.append(f"score={float(score):.2f}")
                    except (TypeError, ValueError):
                        pass
                prefix = f"- [{label}"
                if meta:
                    prefix += " | " + ", ".join(meta)
                prefix += "] "
                if text:
                    lines.append(prefix + text[:500])
            return "\n".join(lines) if lines else "(none)"

        docs_block = _format_entries(relevant_docs, 10, "insight")
        cases_block = _format_entries(case_memories, 10, "case_memory")
        skills_block = _format_entries(agent_skills, 10, "skill")
        rules_block = _format_entries(agent_rules, 10, "rule")
        kb_block = _format_entries(general_kb, 10, "kb")

        return (
            f"You are HiveMind of {tenant_id}, the enterprise memory engine for this tenant.\n"
            f"Reply language: {lang}.\n"
            f"Mandatory language rule: write the full answer only in {lang}. Do not answer in English unless the user explicitly asks for English.\n"
            "Identity:\n"
            f"- Identify as HiveMind for {tenant_id}, not as the website voice agent.\n"
            "- You hold conversation-derived customer insights, reusable rules, skills, and knowledge captured for this tenant.\n"
            "- Your job is to help internal teams inspect what customers said, what patterns emerged, and what new memory should be saved.\n"
            "Operating rules:\n"
            "- Prioritize real customer insights collected by TARA during conversations: pains, objections, intent, desires, concerns, wording patterns, blockers, and trends.\n"
            "- Answer the user's question directly in 2-6 sentences first. Do not default to headings, dashboards, or management-summary sections.\n"
            "- Ground every answer in the retrieved HiveMind items below. Mention the actual signals you found, not generic advice.\n"
            "- When the user asks for a time window like 'last week' or 'last 7 days', summarize only matching retrieved memories if timestamps exist. If no recent memories were retrieved, say that plainly in one sentence.\n"
            "- After the direct answer, include a short 'HiveMind retrievals:' list with up to 5 concrete bullets only when retrievals exist.\n"
            "- Do not output generic sections such as 'Business Implications', 'Customer Insights (Current State)', or 'Recommended Next Steps' unless the user explicitly asks for a report.\n"
            "- Do not invent sources or confidence.\n"
            "- When the user proposes a new skill, rule, or knowledge item, rewrite it into a clean reusable entry suitable for saving into HiveMind.\n"
            "- Keep answers concise, operational, and enterprise-facing.\n"
            "- If evidence is weak, say that briefly and stop. Do not pad the answer with process advice unless the user asks what to do next.\n"
            f"Conversation history:\n{history_block}\n\n"
            f"Retrieved customer insight memory:\n{cases_block}\n\n"
            f"Retrieved static docs:\n{docs_block}\n\n"
            f"Retrieved skills:\n{skills_block}\n\n"
            f"Retrieved rules:\n{rules_block}\n\n"
            f"Retrieved knowledge base entries:\n{kb_block}\n\n"
            f"User query: {query}\n"
            "Answer:"
        )

    @staticmethod
    def _sanitize_tenant(tenant_id: Optional[str]) -> str:
        tenant = (tenant_id or "default").strip().lower()
        return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in tenant)

    @staticmethod
    def _wants_german(language: Optional[str]) -> bool:
        return (language or "").strip().lower() in {"de", "deu", "ger", "german", "deutsch"}

    @staticmethod
    def _looks_english(text: str) -> bool:
        sample = (text or "").strip().lower()
        if not sample:
            return False
        english_markers = [
            "the ", "this ", "that ", "you ", "your ", "what ", "how ", "why ",
            "sounds like", "i'm ", "i am ", "here's ", "here is ", "could you",
            "please", "help", "brand", "goal", "biggest hurdle"
        ]
        german_markers = [
            " der ", " die ", " das ", " und ", " nicht ", " mit ", " für ",
            " klingt", " sie ", " ihre ", " dein ", " warum ", " wie ", " was "
        ]
        english_hits = sum(1 for marker in english_markers if marker in f" {sample} ")
        german_hits = sum(1 for marker in german_markers if marker in f" {sample} ")
        return english_hits > 0 and english_hits >= german_hits

    @staticmethod
    def _cap_answer_sentences(answer: str, max_sentences: int = 3, max_chars: int = 420) -> str:
        text = (answer or "").strip()
        if not text:
            return text

        parts = re.split(r'(?<=[.!?])\s+', text)
        kept = []
        for part in parts:
            cleaned = (part or "").strip()
            if not cleaned:
                continue
            kept.append(cleaned)
            if len(kept) >= max_sentences:
                break

        capped = " ".join(kept).strip()
        if len(capped) > max_chars:
            capped = capped[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
            if capped and capped[-1] not in ".!?":
                capped += "."
        return capped or text

    def _apply_policy_retrieval_profile(
        self,
        context: Optional[Dict[str, Any]],
        case_memories: List[Dict[str, Any]],
        agent_skills: List[Dict[str, Any]],
        agent_rules: List[Dict[str, Any]],
        general_kb: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        stage_aware_enabled = bool(getattr(self.config, "enable_stage_aware_retrieval", False))
        if isinstance(context, dict) and "enable_stage_aware_retrieval" in context:
            stage_aware_enabled = bool(context.get("enable_stage_aware_retrieval"))
        if not isinstance(context, dict) or not stage_aware_enabled:
            return case_memories, agent_skills, agent_rules, general_kb

        profile = str(context.get("retrieval_profile") or "").strip().lower()
        if not profile:
            return case_memories, agent_skills, agent_rules, general_kb

        if profile == "sales_objection":
            return case_memories[:6], agent_skills[:4], agent_rules[:4], general_kb[:2]
        if profile == "sales_close":
            return case_memories[:3], agent_skills[:5], agent_rules[:5], general_kb[:3]
        if profile == "clinical_red_flag":
            return case_memories[:5], agent_skills[:2], agent_rules[:6], general_kb[:4]
        if profile == "clinical_summary":
            return case_memories[:6], agent_skills[:2], agent_rules[:3], general_kb[:2]
        if profile == "clinical_intake":
            return case_memories[:5], agent_skills[:4], agent_rules[:4], general_kb[:3]
        if profile == "sales_discovery":
            return case_memories[:4], agent_skills[:4], agent_rules[:3], general_kb[:4]
        return case_memories, agent_skills, agent_rules, general_kb

    @staticmethod
    def _build_policy_rule_texts(context: Optional[Dict[str, Any]]) -> List[str]:
        if not isinstance(context, dict) or not context.get("policy_mode"):
            return []

        rules = [
            f"policy_mode={context.get('policy_mode', 'sales')}",
            f"conversation_stage={context.get('conversation_stage', 'general')}",
            f"response_act={context.get('response_act', 'answer')}",
        ]
        hypotheses = context.get("hypotheses") or []
        if hypotheses:
            rules.append("hypotheses=" + " | ".join(str(item)[:120] for item in hypotheses[:3]))
        missing_slots = context.get("missing_slots") or []
        if missing_slots:
            rules.append("missing_slots=" + " | ".join(str(item)[:80] for item in missing_slots[:4]))
        policy_metadata = context.get("policy_metadata") or {}
        if isinstance(policy_metadata, dict):
            if policy_metadata.get("active_listening_summary"):
                rules.append("active_listening_summary=" + str(policy_metadata.get("active_listening_summary"))[:240])
            if policy_metadata.get("next_question_focus"):
                rules.append("next_question_focus=" + str(policy_metadata.get("next_question_focus"))[:120])
        return rules

    async def _enforce_response_language(self, answer: str, language: Optional[str]) -> str:
        text = (answer or "").strip()
        if not text:
            return text
        if not self._wants_german(language):
            return text
        if not self._looks_english(text):
            return text
        if not self.groq_client:
            logger.warning("German reply requested but Groq translation client is unavailable; returning original answer")
            return text

        try:
            loop = asyncio.get_running_loop()
            translated = await loop.run_in_executor(
                None,
                lambda: self.groq_client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    temperature=0.1,
                    messages=[
                        {
                            "role": "system",
                            "content": "Rewrite the assistant answer into natural German. Preserve meaning, keep it concise, and output only the final German answer."
                        },
                        {"role": "user", "content": text},
                    ],
                )
            )
            german_text = translated.choices[0].message.content.strip()
            if german_text:
                logger.info("🌐 Rewrote English draft into German to honor requested response language")
                return german_text
        except Exception as e:
            logger.error(f"German response enforcement failed: {e}")
        return text

    def _compose_policy_answer(self, answer: str, context: Optional[Dict[str, Any]]) -> str:
        text = (answer or "").strip()
        if not text or not isinstance(context, dict):
            return text

        policy_mode = str(context.get("policy_mode") or "").strip().lower()
        response_act = str(context.get("response_act") or "").strip().lower()
        micro_reasoning_enabled = bool(getattr(self.config, "enable_micro_reasoning", False))
        if "enable_micro_reasoning" in context:
            micro_reasoning_enabled = bool(context.get("enable_micro_reasoning"))
        policy_metadata = context.get("policy_metadata") or {}
        next_question_focus = ""
        if isinstance(policy_metadata, dict):
            next_question_focus = str(policy_metadata.get("next_question_focus") or "").strip().lower()

        if policy_mode == "clinical" and response_act in {"probe", "escalate"}:
            # Ensure the answer contains a question — append one from the policy focus if missing
            if "?" not in text:
                follow_up_question = self._build_clinical_follow_up_question(next_question_focus)
                if follow_up_question:
                    text = f"{text} {follow_up_question}".strip()
            # If there is an active listening summary but the response is just a bare question,
            # prepend a brief reflection to match the "reflect then ask" format
            active_summary = ""
            if isinstance(policy_metadata, dict):
                active_summary = str(policy_metadata.get("active_listening_summary") or "").strip()
            bare_question_starters = (
                "Haben", "Wie", "Wann", "Wo", "Was", "Gibt", "Können", "Wer",
                "Warum", "Welche", "Welchen", "Welches", "Würden", "Ist", "Do ", "What ", "Who ",
            )
            if active_summary and text.lstrip().startswith(bare_question_starters):
                # Bare question without reflection — extract problem area and build reflection
                problem_area = active_summary.split(";")[0].replace("brand problem area:", "").strip()
                if problem_area:
                    text = f"Das klingt nach einem Thema rund um {problem_area}. {text}".strip()
        if response_act in {"confirm", "probe", "clarify"}:
            max_chars = 320 if micro_reasoning_enabled else 360
            return RAGEngine._cap_answer_sentences(text, max_sentences=2, max_chars=max_chars)
        if response_act == "escalate":
            return RAGEngine._cap_answer_sentences(text, max_sentences=2, max_chars=300)
        if response_act == "summarize":
            max_chars = 360 if micro_reasoning_enabled else 420
            return RAGEngine._cap_answer_sentences(text, max_sentences=3, max_chars=max_chars)
        return text

    @staticmethod
    def _build_clinical_follow_up_question(next_question_focus: str) -> str:
        """Build the next strategic probing question for brand consulting intake.

        Uses a structured dx-based approach: the policy layer identifies the most
        likely brand problem hypothesis and the most discriminating missing fact.
        This method maps that to a natural German spoken question.
        """
        # Slot-based fallbacks (when no differential is available)
        _slot_map = {
            "red_flag_screen": "Gibt es gerade einen konkreten Anlass — eine Präsentation, einen Launch oder eine Deadline?",
            "brand_trigger": "Was hat dieses Gespräch jetzt ausgelöst — gibt es einen konkreten Anlass?",
            "timeline": "Haben Sie eine bestimmte Deadline oder ein Zeitfenster im Kopf?",
            "urgency_level": "Wie dringend ist das für Sie gerade — gibt es einen festen Termin?",
            "context_trigger": "Was passiert gerade bei Ihnen — Wachstum, Neuausrichtung oder etwas anderes?",
            "existing_assets": "Was haben Sie im Bereich Marke oder Kommunikation schon?",
            "clarify_pattern": "Was genau meinen Sie damit — können Sie das kurz konkretisieren?",
            "summary": "Gibt es noch einen Aspekt, der Ihnen besonders wichtig ist?",
        }

        # Brand-strategy dx-driven question map
        # Key format: {dx}:{feature}
        _dx_question_map = {
            # Brand positioning — unclear USP (danger 9)
            "unclear_USP:no_audience_defined": "Wer ist Ihre konkrete Zielgruppe — wen wollen Sie mit der Marke ansprechen?",
            "unclear_USP:generic_claims": "Was würden Sie sagen, macht Ihr Angebot für genau diese Zielgruppe einzigartig?",
            "unclear_USP:everyone_is_target": "Wenn Sie nur einen einzigen Kundentyp beschreiben könnten — wer wäre das?",
            "unclear_USP:low_conversion": "Woran scheitern Interessenten meistens — was bremst die Entscheidung?",
            # Brand positioning — wrong target audience (danger 8)
            "wrong_target_audience:message_resonates_nobody": "Bei wem zieht Ihre aktuelle Kommunikation am stärksten — und bei wem gar nicht?",
            "wrong_target_audience:high_churn": "Welche Kunden bleiben langfristig, und welche gehen schnell wieder?",
            "wrong_target_audience:wrong_inquiries": "Was für Anfragen kommen rein — passen die zu dem, was Sie eigentlich anbieten?",
            # Brand positioning — me-too (danger 7)
            "me_too_positioning:competitors_say_same_thing": "Was sagen Ihre drei direkten Wettbewerber über sich — klingt das ähnlich wie bei Ihnen?",
            "me_too_positioning:price_only_differentiator": "Wenn Preis keine Rolle spielen würde — warum würde jemand trotzdem zu Ihnen kommen?",
            "me_too_positioning:commodity_market": "Was können Sie, das andere in Ihrem Markt nicht können oder nicht so gut?",
            # Brand messaging — inconsistent voice (danger 8)
            "inconsistent_voice:multiple_authors": "Wer schreibt aktuell Ihre Texte — gibt es eine Person, die den Ton definiert?",
            "inconsistent_voice:no_guidelines": "Haben Sie schriftlich festgehalten, wie Ihre Marke kommuniziert und klingt?",
            "inconsistent_voice:different_tone_everywhere": "Klingt Ihre Website anders als Ihre Social-Media-Posts oder Ihre Angebote?",
            # Brand messaging — too complex (danger 7)
            "too_complex_message:confused_customer_responses": "Wenn jemand Ihre Website liest — versteht er sofort, was Sie anbieten und für wen?",
            "too_complex_message:long_explanation_needed": "Wie lange brauchen Sie normalerweise, um jemandem zu erklären, was Sie machen?",
            # Brand messaging — too generic (danger 6)
            "too_generic_message:no_emotional_hook": "Was ist der eine Satz, den Ihre Kunden über Sie sagen, wenn sie Sie weiterempfehlen?",
            "too_generic_message:feature_list_only": "Welche Emotion oder welches Ergebnis steht im Mittelpunkt Ihrer Kommunikation?",
            # Brand identity — no coherent identity (danger 8)
            "no_coherent_identity:no_guidelines": "Haben Sie eine Brandguide oder festgelegte Designvorgaben — oder entscheidet das jedes Mal neu jemand?",
            "no_coherent_identity:inconsistent_across_channels": "Sieht Ihre Marke auf der Website, in Social Media und in Dokumenten gleich aus?",
            "no_coherent_identity:multiple_logos": "Wie viele verschiedene Logo-Varianten oder Versionen sind gerade im Einsatz?",
            # Brand identity — misaligned with positioning (danger 7)
            "identity_misaligned_with_positioning:premium_brand_with_cheap_design": "Wie soll Ihre Marke wirken — eher hochwertig, zugänglich, modern oder etwas anderes?",
            # Brand identity — outdated (danger 5)
            "outdated_identity:created_over_five_years_ago": "Wann wurde die aktuelle Markenidentität das letzte Mal überarbeitet?",
            "outdated_identity:competitors_look_more_modern": "Haben Sie das Gefühl, dass Ihre Wettbewerber visuell moderner oder professioneller wirken?",
            # Brand awareness — wrong channels (danger 8)
            "wrong_channels:audience_active_elsewhere": "Wo sind Ihre Kunden wirklich aktiv — und sind Sie dort auch präsent?",
            "wrong_channels:low_engagement_all_channels": "Auf welchem Kanal haben Sie die stärkste Resonanz — und auf welchem kaum?",
            # Brand awareness — inconsistent presence (danger 7)
            "inconsistent_presence:no_content_plan": "Gibt es einen festen Rhythmus für Ihre Kommunikation — oder geht es eher nach Gefühl?",
            "inconsistent_presence:sporadic_posts": "Wie regelmäßig kommuniziert Ihre Marke gerade nach außen?",
            # Brand trust — no proof of expertise (danger 9)
            "no_proof_of_expertise:no_case_studies": "Haben Sie konkrete Beispiele oder Referenzen, die zeigen, was Sie leisten?",
            "no_proof_of_expertise:generic_claims_only": "Was würde ein potenzieller Kunde auf Ihrer Website finden, das Vertrauen aufbaut?",
            # Brand trust — negative reputation (danger 8)
            "negative_online_reputation:bad_reviews_visible": "Wie ist Ihr aktuelles Bild online — gibt es Bewertungen oder Feedback, das Sie beschäftigt?",
            # Brand trust — authority mismatch (danger 6)
            "authority_mismatch:positioned_as_expert_but_shallow_content": "Welche Inhalte teilen Sie, die Ihre Expertise konkret zeigen?",
            # Growth strategy — funnel disconnected (danger 8)
            "brand_funnel_disconnected:ads_not_matching_brand": "Fühlt sich Ihre Werbung und Ihre Marke wie ein kohärentes Bild an — oder klaffen die auseinander?",
            # Growth strategy — unclear value proposition (danger 8)
            "value_proposition_unclear_to_buyer:long_sales_cycle": "An welcher Stelle im Gespräch verlieren Sie Interessenten meistens?",
            "value_proposition_unclear_to_buyer:frequent_price_objections": "Was ist der häufigste Einwand, den Sie von Interessenten hören?",
            # Growth strategy — wrong segment (danger 7)
            "wrong_segment_targeted:lots_of_leads_no_conversion": "Was passiert mit den Anfragen, die reinkommen — konvertieren die oder nicht?",
            # Competitive pressure — undifferentiated (danger 9)
            "undifferentiated_in_crowded_market:no_clear_reason_to_choose_us": "Warum sollte jemand Sie gegenüber einem direkten Wettbewerber wählen — was ist der entscheidende Unterschied?",
            "undifferentiated_in_crowded_market:price_only_differentiator": "Haben Sie schon mal einen Auftrag verloren, weil jemand günstiger war — wie oft passiert das?",
            # Competitive pressure — being copied (danger 7)
            "being_copied:competitors_copying_positioning": "Haben Sie das Gefühl, dass Wettbewerber Ihre Positionierung oder Sprache übernehmen?",
            # General probes
            "general:existing_assets": "Was haben Sie im Bereich Marke schon — Logo, Texte, Richtlinien oder anderes?",
        }

        focus = (next_question_focus or "").strip()

        # Handle structured keys: dx_confirm:DX:FEATURE, dx_discriminate:AvB:FEATURE, dx_probe:DX:FEATURE
        if ":" in focus:
            parts = focus.split(":", 2)
            if len(parts) >= 3:
                prefix, dx_part, feature = parts[0], parts[1], parts[2]
                if prefix == "dx_discriminate" and "vs" in dx_part:
                    top_dx = dx_part.split("vs")[0]
                    lookup = f"{top_dx}:{feature}"
                else:
                    lookup = f"{dx_part}:{feature}"
                if lookup in _dx_question_map:
                    return _dx_question_map[lookup]
                if feature in _slot_map:
                    return _slot_map[feature]

        return _slot_map.get(focus, "Was ist dabei der wichtigste Aspekt, den Sie angehen möchten?")

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

    def _fast_local_mode_enabled(self) -> bool:
        env_value = os.getenv("RAG_FAST_LOCAL_MODE", "").strip().lower()
        if env_value in {"1", "true", "yes", "on"}:
            return True
        llm_model = str(getattr(self.config, "llm_model", "") or "").strip().lower()
        return llm_model in {"openai/gpt-oss-20b", "openai/gpt-oss-8b", "llama-3.1-8b-instant"}

    async def warmup(self, tenant_ids: Optional[List[str]] = None) -> None:
        """
        Warm up tenant-specific context architecture and the LLM provider.

        This is intentionally tiny: it primes the first-turn code path without
        affecting user-visible output.
        """
        if not getattr(self.config, "enable_startup_warmup", True):
            return

        warmup_tenants: List[str] = []
        for tenant in (tenant_ids or []):
            normalized = self._sanitize_tenant(tenant)
            if normalized and normalized not in warmup_tenants:
                warmup_tenants.append(normalized)

        if not warmup_tenants:
            default_tenant = self._sanitize_tenant(os.getenv("TENANT_ID", "tara"))
            warmup_tenants = [default_tenant]

        try:
            await asyncio.wait_for(
                self._warmup_impl(warmup_tenants),
                timeout=max(1.0, float(getattr(self.config, "startup_warmup_timeout", 8.0) or 8.0)),
            )
            logger.info(f"✅ RAG warmup completed for tenants={','.join(warmup_tenants)}")
        except asyncio.TimeoutError:
            logger.warning("⚠️ RAG warmup timed out")
        except Exception as exc:
            logger.warning(f"⚠️ RAG warmup skipped or failed: {exc}")

    async def _warmup_impl(self, warmup_tenants: List[str]) -> None:
        for tenant in warmup_tenants:
            try:
                self._prime_prompt_path(tenant)
            except Exception as exc:
                logger.debug(f"Warmup prompt cache skipped for tenant={tenant}: {exc}")

        if self.embeddings:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self.embeddings.embed_query("startup warmup"))

        if self.llm is None:
            return

        warmup_prompt = (
            "<zone_a_system_configuration>\n"
            "You are TARA. Reply with exactly OK.\n"
            "</zone_a_system_configuration>\n"
            "User: startup warmup"
        )
        result = self.llm.generate(
            prompt=warmup_prompt,
            stream=False,
            temperature=0.0,
            max_tokens=4,
            include_reasoning=False,
            reasoning_effort="low",
            model=self.config.llm_model,
        )
        if asyncio.iscoroutine(result):
            await result

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
        dashboard_mode = isinstance(context, dict) and context.get("surface") == "hivemind_dashboard"
        fast_local_mode = self._fast_local_mode_enabled()
        unified_limit = 10 if dashboard_mode else (4 if fast_local_mode else 8)
        browse_mode = dashboard_mode and self._is_hivemind_browse_query(query)
        policy_mode = str((context or {}).get("policy_mode") or self.config.policy_mode_default or "").strip().lower()
        conversation_stage = str((context or {}).get("conversation_stage") or "").strip().lower()
        skip_heavy_retrieval = fast_local_mode and policy_mode in {"sales", "clinical"} and conversation_stage in {
            "sales.discovery",
            "sales.qualify",
            "clinical.intake",
            "clinical.triage",
        }
        if skip_heavy_retrieval:
            logger.info(f"⚡ Fast local policy mode: skipping embedding/Qdrant retrieval for stage={conversation_stage}")
        
        # 1. High-Speed Detection & Translation Layer
        query_english = query
        original_language = "german"
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
        # Skip translation for sales/clinical modes: paraphrase-multilingual-MiniLM-L12-v2 handles German natively,
        # so the Groq round-trip (~200ms) is pure latency overhead for those modes.
        _translation_needed = policy_mode not in {"sales", "clinical"}
        if original_language in ['de', 'german', 'deutsch'] and self.groq_client and not fast_local_mode and _translation_needed:
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
        MAX_HISTORY_CHARS = 2000 if fast_local_mode else 4000
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
        if is_context_dependent and self.groq_client and history_context and not fast_local_mode:
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
        if not skip_heavy_retrieval and self.embeddings and (self.config.enable_local_retrieval or (self.qdrant and self.qdrant.enabled)):
            v_start = time.time()
            v_list = self.embeddings.embed_query(query_english)
            shared_vector = np.array(v_list, dtype=np.float32).reshape(1, -1)
            timing['shared_embedding_ms'] = (time.time() - v_start) * 1000
        embeddings_unavailable = shared_vector is None or not np.any(shared_vector)
        if dashboard_mode and embeddings_unavailable:
            browse_mode = True
        
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
                        
                        # Perform semantic search, or browse latest memories for inventory-style dashboard queries
                        # Perform semantic search, or browse latest memories for inventory-style dashboard queries
                        if browse_mode:
                            results = await asyncio.wait_for(
                                self.qdrant.browse_tenant_memory(
                                    tenant_id=tenant_id,
                                    doc_types=target_types,
                                    limit=unified_limit,
                                ),
                                timeout=1.2
                            )
                        else:
                            # Fetch a larger pool to allow for balanced selection (3/2/1/1 strategy)
                            results = await asyncio.wait_for(
                                self.qdrant.search_unified_memory(
                                    query_vector=v_raw,
                                    tenant_id=tenant_id,
                                    doc_types=target_types,
                                    limit=30 if not dashboard_mode else unified_limit,
                                    score_threshold=0.15,
                                    query_text=query
                                ),
                                timeout=1.0 if not dashboard_mode else 2.5  # Tight timeout for chat; dashboard can afford more
                            )
                        
                        search_time = (time.time() - unified_start) * 1000
                        
                        # Post-process results into categories
                        processed = {
                            "hive_mind_text": "",
                            "case_memories": [],
                            "skills": [],
                            "rules": [],
                            "general_kb": []
                        }
                        
                        # Stratified Selection logic (Only for chat, not dashboard)
                        raw_categories = {
                            "Agent_Skill": [],
                            "Agent_Rule": [],
                            "Case_Memory": [],
                            "General_KB": []
                        }

                        for r in results:
                            dt = r.get("doc_type")
                            if dt in raw_categories:
                                raw_categories[dt].append(r)

                        # Filter and slice according to 3/2/1/1 strategy (or 10 for dashboard)
                        if dashboard_mode:
                            filtered_results = results[:unified_limit]
                            logger.info(f"📊 Dashboard retrieval: {len(filtered_results)} total chunks")
                        else:
                            # Reserve slots: top 3 KB, top 2 Case, top 1 Skill, top 1 Rule
                            kb_slice = raw_categories["General_KB"][:3]
                            case_slice = raw_categories["Case_Memory"][:2]
                            skill_slice = raw_categories["Agent_Skill"][:1]
                            rule_slice = raw_categories["Agent_Rule"][:1]
                            
                            filtered_results = kb_slice + case_slice + skill_slice + rule_slice
                            
                            # Detailed logging of the strata
                            logger.info(
                                f"🧠 Stratified retrieval: {len(filtered_results)} final chunks from pool of {len(results)}. "
                                f"Distribution: KB={len(kb_slice)}, Case={len(case_slice)}, "
                                f"Skill={len(skill_slice)}, Rule={len(rule_slice)}"
                            )

                        hm_entries = []
                        for r in filtered_results:
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
                                processed["case_memories"].append({
                                    "id": r.get("id"),
                                    "text": issue,
                                    "summary": solution,
                                    "doc_type": dt,
                                    "score": r["score"],
                                    "created_at": payload.get("created_at") or payload.get("timestamp"),
                                })
                            elif dt == "General_KB":
                                # Format General KB as context chunks
                                source = payload.get("filename", "Internal Doc")
                                text = r["text"]
                                hm_entries.append(f"[{source}]: {text}")
                                processed["general_kb"].append({
                                    "text": r["text"],
                                    "topic": payload.get("topics") or payload.get("doc_type_detail") or "general",
                                    "doc_type": dt,
                                    "score": r["score"],
                                })
                        
                        if hm_entries:
                            processed["hive_mind_text"] = "\n---\n".join(hm_entries)

                        if dashboard_mode and not any([
                            processed["case_memories"],
                            processed["skills"],
                            processed["rules"],
                            processed["general_kb"],
                        ]) and not browse_mode:
                            browse_results = await asyncio.wait_for(
                                self.qdrant.browse_tenant_memory(
                                    tenant_id=tenant_id,
                                    doc_types=target_types,
                                    limit=unified_limit,
                                ),
                                timeout=1.2
                            )
                            for r in browse_results:
                                dt = r.get("doc_type")
                                payload = r.get("payload", {})
                                if dt == "Case_Memory":
                                    issue = payload.get("issue", r["text"])
                                    solution = payload.get("solution", r["summary"])
                                    processed["case_memories"].append({
                                        "id": r.get("id"),
                                        "text": issue,
                                        "summary": solution,
                                        "doc_type": dt,
                                        "score": r.get("score", 1.0),
                                        "created_at": payload.get("created_at") or payload.get("timestamp"),
                                    })
                                elif dt == "Agent_Skill":
                                    processed["skills"].append({"id": r.get("id"), "text": r["text"], "topic": payload.get("topic", "general"), "score": r.get("score", 1.0)})
                                elif dt == "Agent_Rule":
                                    processed["rules"].append({"id": r.get("id"), "text": r["text"], "topic": payload.get("topic", "general"), "severity": payload.get("severity", "standard"), "score": r.get("score", 1.0)})
                                elif dt == "General_KB":
                                    processed["general_kb"].append({
                                        "id": r.get("id"),
                                        "text": r["text"],
                                        "topic": payload.get("topics") or payload.get("doc_type_detail") or "general",
                                        "doc_type": dt,
                                        "score": r.get("score", 1.0),
                                    })
                            hm_entries = [f"Issue: {entry['text']}\nSolution: {entry.get('summary', '')}" for entry in processed["case_memories"]]
                            hm_entries += [f"[KB]: {entry['text']}" for entry in processed["general_kb"]]
                            if hm_entries:
                                processed["hive_mind_text"] = "\n---\n".join(hm_entries)
                            
                        # Logging
                        total_chunks = len(processed["case_memories"]) + len(processed["skills"]) + len(processed["rules"]) + len(processed.get("general_kb", []))
                        if total_chunks > 0:
                            total_chars = sum(len(c["text"]) + len(c.get("summary", "")) for c in processed["case_memories"]) + sum(len(s["text"]) for s in processed["skills"]) + sum(len(r["text"]) for r in processed["rules"]) + sum(len(k["text"]) for k in processed.get("general_kb", []))
                            logger.info(f"🧠 HiveMind retrieval: {total_chunks} chunks ({total_chars} chars)")


                        return processed, search_time
                        
                    except asyncio.TimeoutError:
                        logger.warning("⏱️ Unified Qdrant search TIMEOUT (>2.5s) - skipping")
                    except Exception as e:
                        logger.error(f"❌ Unified Qdrant search error: {e}")
                else:
                    logger.warning("🧠 Qdrant enabled but embeddings not initialized")
            
            return {"hive_mind_text": "", "case_memories": [], "skills": [], "rules": [], "general_kb": []}, 0

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
            "case_memories": unified_data.get("case_memories", []),
            "web_results": web_results,
            "agent_skills": skills_rules.get("skills", []),
            "agent_rules": skills_rules.get("rules", []),
            "general_kb": unified_data.get("general_kb", []),
            "timing": timing,
            "fast_path_type": fast_path_type,
            "history_context": history_context
        }

    @staticmethod
    def _normalize_recent_turns(raw_turns: Optional[Union[str, List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
        if not raw_turns:
            return []
        if isinstance(raw_turns, list):
            normalized: List[Dict[str, Any]] = []
            for turn in raw_turns:
                if not isinstance(turn, dict):
                    continue
                role = str(turn.get("role") or "user").strip()
                content = str(turn.get("content") or turn.get("text") or "").strip()
                if not content:
                    continue
                normalized.append({"role": role, "content": content})
            return normalized
        text = str(raw_turns).strip()
        return [{"role": "user", "content": text}] if text else []

    @classmethod
    def _format_recent_turns_for_summary(cls, recent_turns: Optional[List[Dict[str, Any]]]) -> str:
        turns = cls._normalize_recent_turns(recent_turns)
        if not turns:
            return ""
        lines: List[str] = []
        for turn in turns:
            role = "User" if turn.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {str(turn.get('content') or '').strip()}")
        return "\n".join(lines)

    @classmethod
    def _build_retrieval_history_context(
        cls,
        *,
        session_summary_window: Optional[str],
        recent_turns: Optional[List[Dict[str, Any]]],
        history_context: Optional[Union[str, List[Dict[str, Any]]]],
    ) -> str:
        parts: List[str] = []
        summary = str(session_summary_window or "").strip()
        if summary:
            parts.append(f"Session Summary:\n{summary}")
        recent = cls._format_recent_turns_for_summary(recent_turns)
        if recent:
            parts.append(f"Recent Turns:\n{recent}")
        if not parts and history_context:
            if isinstance(history_context, list):
                fallback = cls._format_recent_turns_for_summary(history_context)
            else:
                fallback = str(history_context).strip()
            if fallback:
                parts.append(fallback)
        return "\n\n".join(parts).strip()

    async def generate_session_summary_window(
        self,
        *,
        previous_summary: str,
        user_text: str,
        assistant_text: str,
        language: str,
        tenant_id: str,
    ) -> str:
        lang = "en" if str(language).lower().startswith("en") else "de"
        system_prompt = (
            "Update a compact layered session summary.\n"
            "Use plain text only, no JSON.\n"
            "Keep the headings in this order: Current Context, Entity Summary, Confirmed Facts And Values, Goals And Constraints, Unresolved Questions, Latest Delta.\n"
            "Preserve names, brand names, companies, dates, numbers, budgets, URLs, preferences, constraints, and unresolved intent.\n"
            "Omit filler, repeated greetings, uncertainty, and transcript noise.\n"
            "Keep older stable facts in the upper sections and put the newest change only in Latest Delta.\n"
            "Make the summary small enough for prompt reuse and future policy extraction.\n"
        )
        if lang == "de":
            system_prompt += "Write in German unless the session is explicitly English-first.\n"
        else:
            system_prompt += "Write in English.\n"

        user_prompt = (
            f"Tenant: {tenant_id}\n\n"
            f"Previous Summary:\n{previous_summary.strip() or '[none]'}\n\n"
            f"Latest User Turn:\n{user_text.strip()}\n\n"
            f"Latest Assistant Turn:\n{assistant_text.strip()}\n\n"
            "Rewrite the full summary window now."
        )

        result = self.llm.generate(
            prompt=f"{system_prompt}\n\n{user_prompt}",
            stream=False,
            temperature=0.2,
            max_tokens=260,
            include_reasoning=False,
            reasoning_effort="low",
            model="openai/gpt-oss-20b",
        )
        if asyncio.iscoroutine(result):
            summary = await result
        else:
            summary = str(result)
        return str(summary or previous_summary or "").strip()

    async def process_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        streaming_callback: Optional[Callable[[str, bool], None]] = None,
        history_context: Optional[str] = None,
        session_summary_window: Optional[str] = None,
        session_summary_revision: int = 0,
        recent_turns: Optional[List[Dict[str, Any]]] = None,
        tenant_id: str = "tara",
        force_non_stream: bool = False,
        generation_config: Optional[Dict[str, Any]] = None,
        interrupted_text: Optional[str] = None,
        interruption_transcripts: Optional[List[str]] = None,
        interruption_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """High-performance RAG pipeline with parallel retrieval and FAST-PATH.

        Args:
            query: User query text
            context: Optional context/intent information
            streaming_callback: Optional callback for streaming responses
            history_context: Optional conversation history for context-aware responses
            tenant_id: Tenant/tenant identifier for cache isolation
            force_non_stream: Force non-streaming response
            generation_config: Optional generation configuration
            interrupted_text: Assistant's response text that was interrupted (barge-in)
            interruption_transcripts: User's interruption transcripts collected during interruption
            interruption_type: Type of interruption ('addon', 'topic_change', 'clarification', 'noise')
        """
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

        # Keep the first request for each tenant on the compact prompt path by
        # resolving and priming the architect before retrieval/generation work.
        self._prime_prompt_path(tenant_id)
        
        # 1-3. Retrieve Context
        retrieval_history = self._build_retrieval_history_context(
            session_summary_window=session_summary_window,
            recent_turns=recent_turns,
            history_context=history_context,
        )
        retrieval_data = await self.retrieve_context(query, context, retrieval_history, tenant_id=tenant_id)
        
        query_english = retrieval_data['query_english']
        original_language = retrieval_data['original_language']
        relevant_docs = retrieval_data['relevant_docs']
        hive_mind_context = retrieval_data['hive_mind_context']
        web_results = retrieval_data['web_results']
        agent_skills = retrieval_data.get('agent_skills', [])
        agent_rules = retrieval_data.get('agent_rules', [])
        general_kb = retrieval_data.get('general_kb', [])
        case_memories = retrieval_data.get('case_memories', [])
        case_memories, agent_skills, agent_rules, general_kb = self._apply_policy_retrieval_profile(
            context, case_memories, agent_skills, agent_rules, general_kb
        )
        timing = retrieval_data['timing']
        fast_path_type = retrieval_data['fast_path_type']
        
        # 3.5 Language Context Strategy
        # Detect explicit intent to switch response language
        combined_context_text = (str(retrieval_history) + " " + query).lower()
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
        if case_memories:
            hive_mind_state["insights"]["tenant_memory"] = "\n".join(
                f"Issue: {str(entry.get('text', '')).strip()} | Solution: {str(entry.get('summary', '')).strip()}"
                for entry in case_memories[:8]
                if str(entry.get("text", "")).strip()
            )
        if general_kb:
            hive_mind_state["insights"]["knowledge_base"] = "\n".join(
                str(entry.get("text", "")).strip()
                for entry in general_kb[:8]
                if str(entry.get("text", "")).strip()
            )
        if web_results:
            hive_mind_state["insights"]["live_web_data"] = str(web_results)
        policy_snapshot = {
            "policy_mode": (context.get("policy_mode") if isinstance(context, dict) else None) or self.config.policy_mode_default,
            "conversation_stage": context.get("conversation_stage") if isinstance(context, dict) else None,
            "response_act": context.get("response_act") if isinstance(context, dict) else None,
            "retrieval_profile": context.get("retrieval_profile") if isinstance(context, dict) else None,
            "hypotheses": (context.get("hypotheses") or []) if isinstance(context, dict) else [],
            "missing_slots": (context.get("missing_slots") or []) if isinstance(context, dict) else [],
            "policy_metadata": (context.get("policy_metadata") or {}) if isinstance(context, dict) else {},
        }
        hive_mind_state["variables"]["policy"] = policy_snapshot

        # User profile for personalization
        user_profile = {
            "language": original_language,
            "session_type": "technical_support"
        }
        user_id = None
        if isinstance(context, dict):
            if context.get("surface"):
                user_profile["surface"] = str(context.get("surface"))
            if context.get("dashboard_mode"):
                user_profile["dashboard_mode"] = str(context.get("dashboard_mode"))
            if context.get("tenant_id"):
                user_profile["tenant_id"] = str(context.get("tenant_id"))
            if context.get("user_id"):
                user_id = str(context.get("user_id"))
            if context.get("policy_mode"):
                user_profile["policy_mode"] = str(context.get("policy_mode"))
            if context.get("conversation_stage"):
                user_profile["conversation_stage"] = str(context.get("conversation_stage"))
            if context.get("response_act"):
                user_profile["response_act"] = str(context.get("response_act"))

        # Build history list
        history_list = self._normalize_recent_turns(recent_turns or history_context)

        # 4. Context Architecture (Zoned XML Assembly)
        # Using the new ContextArchitect for <500ms TTFT and robust context boundaries
        # Zone D: Dynamic skills/rules retrieved from Qdrant HiveMind
        skill_texts = [s.get("text", "") for s in agent_skills if s.get("text")]
        rule_texts = [r.get("text", "") for r in agent_rules if r.get("text")]
        rule_texts.extend(self._build_policy_rule_texts(context))

        if isinstance(context, dict) and context.get("surface") == "hivemind_dashboard":
            active_llm_model = (getattr(self.config, "hivemind_llm_model", None) or getattr(self.config, "llm_model", "")).strip()
            prompt = self._build_hivemind_dashboard_prompt(
                query=query,
                tenant_id=tenant_id,
                language=original_language,
                history=history_list,
                relevant_docs=relevant_docs,
                case_memories=case_memories,
                agent_skills=agent_skills,
                agent_rules=agent_rules,
                general_kb=general_kb,
                system_prompt=context.get("system_prompt"),
            )
        else:
            active_llm_model = str(getattr(self.config, "llm_model", "")).strip()
            architect_cls = self._resolve_context_architect(tenant_id)
            architect_kwargs = {
                "query": query_english,
                "raw_query": query,
                "retrieved_docs": relevant_docs,
                "history": history_list,
                "hive_mind": hive_mind_state,
                "user_profile": user_profile,
                "agent_skills": skill_texts,
                "agent_rules": rule_texts,
                "interrupted_text": interrupted_text,
                "interruption_transcripts": interruption_transcripts,
                "interruption_type": interruption_type,
                "user_id": user_id,
            }
            try:
                signature = inspect.signature(architect_cls.assemble_prompt)
                if "session_summary_window" in signature.parameters:
                    architect_kwargs["session_summary_window"] = session_summary_window
            except Exception:
                pass
            prompt = architect_cls.assemble_prompt(**architect_kwargs)
        prompt = self._apply_prompt_budget(prompt, active_llm_model)

        # 4.5 Generation controls (endpoint-tunable)
        gen_cfg = generation_config or {}
        generation_temperature = float(gen_cfg.get("temperature", 0.65))
        generation_max_tokens = int(gen_cfg.get("max_tokens", 400))
        if self._fast_local_mode_enabled():
            generation_max_tokens = min(generation_max_tokens, 220)
        generation_stop = gen_cfg.get("stop")
        if isinstance(generation_stop, str):
            generation_stop = [generation_stop]
        if not isinstance(generation_stop, list):
            generation_stop = None
        
        # 5. Generation
        gen_start = time.time()
        accumulated = ""
        first_chunk_at = None
        streamed_live = False
        
        try:
            # Log the full prompt for debugging 0-token issues
            logger.debug(f"DEBUG PROMPT: {prompt}")
            
            # Allow streaming for all models and languages to ensure TTS quality
            use_stream = not force_non_stream
            result = self.llm.generate(
                prompt=prompt,
                stream=use_stream,
                temperature=generation_temperature,
                max_tokens=generation_max_tokens,
                include_reasoning=False,
                reasoning_effort="low",
                stop=generation_stop,
                model=active_llm_model,
            )
            llm_usage = {}  # Capture token usage from LLM provider
            
            # Handle Async Generator (Groq Async Streaming)
            if hasattr(result, '__aiter__'):
                streamed_live = True
                async for chunk in result:
                    # Check for usage sentinel (dict with __usage__ key)
                    if isinstance(chunk, dict) and "__usage__" in chunk:
                        llm_usage = chunk["__usage__"]
                        continue
                    if first_chunk_at is None: first_chunk_at = time.time()
                    text = chunk if isinstance(chunk, str) else getattr(chunk, 'text', str(chunk))
                    accumulated += text
                    if streaming_callback and text:
                        streaming_callback(text, False)

            # Handle Sync Generator (Gemini/OpenAI Streaming)
            elif hasattr(result, '__iter__'):
                streamed_live = True
                for chunk in result:
                    if isinstance(chunk, dict) and "__usage__" in chunk:
                        llm_usage = chunk["__usage__"]
                        continue
                    if first_chunk_at is None: first_chunk_at = time.time()
                    text = chunk if isinstance(chunk, str) else getattr(chunk, 'text', str(chunk))
                    accumulated += text
                    if streaming_callback and text:
                        streaming_callback(text, False)
            
            # Handle Coroutine (Non-streaming Async)
            elif asyncio.iscoroutine(result):
                accumulated = await result
                if first_chunk_at is None: first_chunk_at = time.time()
            
            # Handle direct string
            else:
                accumulated = str(result)
                if first_chunk_at is None: first_chunk_at = time.time()
            
            # Check for empty response and provide a fallback to prevent TTS errors
            if not accumulated.strip():
                logger.warning(f"⚠️ LLM returned empty response for query: '{query}'")
                # Provide a natural fallback response
                accumulated = "I'm sorry, I couldn't process that. Could you please repeat or rephrase your question?"

            # Non-streaming providers may store usage out-of-band.
            if not llm_usage and hasattr(self.llm, "get_last_usage"):
                try:
                    llm_usage = self.llm.get_last_usage() or {}
                except Exception:
                    llm_usage = {}
            
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
                stop=generation_stop,
                model=active_llm_model,
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
                
        timing['generation_ms'] = (time.time() - gen_start) * 1000
        if first_chunk_at: timing['ttfc_ms'] = (first_chunk_at - gen_start) * 1000
        
        # 6. Post-process (strip XML artifacts if model outputs them)
        answer = accumulated.strip()
        # Remove any XML closing tags the model might output (matching minified tags)
        answer = re.sub(r'</resp>.*', '', answer, flags=re.DOTALL).strip()
        answer = re.sub(r'</turn>.*', '', answer, flags=re.DOTALL).strip()
        answer = re.sub(r'</ctxt>.*', '', answer, flags=re.DOTALL).strip()
        answer = await self._enforce_response_language(answer, original_language)
        answer = self._compose_policy_answer(answer, context)
        answer = self._cap_answer_sentences(answer, max_sentences=3, max_chars=420)

        if streaming_callback and not streamed_live:
            streaming_callback(answer, False)
        if streaming_callback:
            streaming_callback("", True)
        
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
                'model': active_llm_model,
                'session_summary_revision': session_summary_revision,
                'llm_usage': llm_usage,
                'raw_chunks': [
                   {"id": d.get("id", "unknown"), "text": d.get("text", ""), "score": d.get("score", 0), "doc_type": d.get("doc_type", "Skill/Rule")} 
                   for d in (case_memories + agent_skills + agent_rules + general_kb) if d.get("id")
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

    async def generate_dynamic_exit(self, history: str, language: str = "german") -> str:
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

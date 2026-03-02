"""
Pipeline for Orchestra-daytona

Handles STT -> Language Detection -> RAG -> TTS pipeline with multi-language support.
"""

import asyncio
import logging
import time
from typing import Dict, Any, Optional, AsyncGenerator

from core.service_client import RAGClient, IntentClient
from utils.lang_detect import detect_language, detect_language_from_metadata
from config_loader import RAGConfig, IntentConfig

logger = logging.getLogger(__name__)


class ProcessingPipeline:
    """
    Main processing pipeline for user queries.
    
    Handles:
    1. Language detection from STT text/metadata
    2. Intent classification (optional)
    3. RAG query with language context
    4. Streaming response generation
    """
    
    def __init__(self, 
                 rag_config: RAGConfig,
                 intent_config: IntentConfig,
                 supported_languages: list = None,
                 skip_ssl: bool = False):
        """
        Initialize pipeline
        
        Args:
            rag_config: RAG service configuration
            intent_config: Intent service configuration
            supported_languages: List of supported language codes
            skip_ssl: Whether to skip SSL verification
        """
        self.skip_ssl = skip_ssl
        self.rag_client = RAGClient(rag_config, skip_ssl=skip_ssl)
        self.intent_client = IntentClient(intent_config)
        self.supported_languages = supported_languages or ["en", "de"]
    
    async def process_query(self,
                           query: str,
                           session_id: str,
                           user_id: Optional[str] = None,
                           stt_metadata: Optional[Dict[str, Any]] = None,
                           language: Optional[str] = None,
                           history_context: Optional[str] = None,
                           form_data: Optional[Dict[str, Any]] = None,
                           rag_url: Optional[str] = None,
                           tenant_id: Optional[str] = None) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Process user query through the pipeline
        
        Args:
            query: User query text from STT
            session_id: Session identifier
            stt_metadata: Optional STT metadata (may contain language info)
            language: Optional pre-detected language (if None, will detect)
            history_context: Optional conversation history for context-aware responses
            form_data: Optional form data (e.g., appointment booking slots)
        
        Yields:
            Dict with 'token', 'language', 'is_final', etc.
        """
        start_time = time.time()
        
        # Step 1: Detect language
        if language is None:
            # Try metadata first
            detected_lang = detect_language_from_metadata(stt_metadata)
            if detected_lang is None:
                # Fall back to text-based detection
                detected_lang = detect_language(query, self.supported_languages)
            
            # Final fallback to default language if detection fails entirely
            if detected_lang is None:
                detected_lang = "en"  # Safe default
                logger.warning(f"[{session_id}] ⚠️ Language detection failed, falling back to default: {detected_lang}")
                
            language = detected_lang
        
        logger.info(f"[{session_id}] 🔍 Language detected: {language} | Query: {query[:100]}")
        
        # Step 2: Intent classification (if enabled)
        intent_result = None
        if self.intent_client.config.enabled:
            try:
                intent_result = await self.intent_client.classify(query, session_id)
                logger.info(f"[{session_id}] 🎯 Intent: {intent_result.get('intent', 'unknown')} "
                          f"(confidence: {intent_result.get('confidence', 0.0):.2f})")
            except Exception as e:
                logger.error(f"[{session_id}] Intent classification error: {e}")
        
        # Merge form_data into context (intent_result)
        context = intent_result
        if form_data:
            if context is None:
                context = {"form_data": form_data}
            else:
                context = {**context, "form_data": form_data}
        
        # Step 3: RAG query with streaming
        try:
            logger.info(f"[{session_id}] 🔄 Starting RAG streaming query: '{query}'")
            # Use streaming RAG if available
            token_count = 0
            full_answer = []
            llm_usage = {}  # Capture LLM usage metadata
            async for token in self.rag_client.query_streaming(
                query=query,
                session_id=session_id,
                user_id=user_id,
                language=language,
                context=context,
                history_context=history_context,
                base_url=rag_url,
                tenant_id=tenant_id
            ):
                # Check for llm_usage metadata dict from service_client
                if isinstance(token, dict) and "__llm_usage__" in token:
                    llm_usage = token["__llm_usage__"]
                    logger.info(f"[{session_id}] 📊 LLM Usage: {llm_usage.get('prompt_tokens', 0)} prompt / {llm_usage.get('completion_tokens', 0)} completion ({llm_usage.get('model', 'unknown')})")
                    continue
                    
                token_count += 1
                full_answer.append(token)
                logger.debug(f"[{session_id}] 📝 Pipeline received token {token_count}: '{token}'")
                yield {
                    "token": token,
                    "language": language,
                    "is_final": False,
                    "timestamp": time.time()
                }
            
            complete_answer = "".join(full_answer)
            logger.info(f"[{session_id}] ✅ RAG streaming complete: {token_count} tokens | Response: {complete_answer[:100]}...")
            
            # Signal completion with usage metadata
            duration_ms = (time.time() - start_time) * 1000
            logger.info(f"[{session_id}] ✅ Pipeline complete | Language: {language} | Duration: {duration_ms:.0f}ms")
            
            yield {
                "token": "",
                "language": language,
                "is_final": True,
                "timestamp": time.time(),
                "duration_ms": duration_ms,
                "llm_usage": llm_usage
            }
        
        except Exception as e:
            logger.error(f"[{session_id}] ❌ Pipeline error: {e}", exc_info=True)
            # Yield error message
            error_msg = "I'm sorry, I encountered an error processing your request."
            if language == "de":
                error_msg = "Entschuldigung, es ist ein Fehler aufgetreten."
            
            yield {
                "token": error_msg,
                "language": language,
                "is_final": True,
                "error": str(e),
                "timestamp": time.time()
            }
    
    async def process_query_non_streaming(self,
                                         query: str,
                                         session_id: str,
                                         user_id: Optional[str] = None,
                                         stt_metadata: Optional[Dict[str, Any]] = None,
                                         language: Optional[str] = None,
                                         form_data: Optional[Dict[str, Any]] = None,
                                         rag_url: Optional[str] = None,
                                         tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Process user query without streaming (returns complete response)
        
        Args:
            query: User query text from STT
            session_id: Session identifier
            stt_metadata: Optional STT metadata
            language: Optional pre-detected language
            form_data: Optional form data (e.g., appointment booking slots)
        
        Returns:
            Dict with 'answer', 'language', 'sources', etc.
        """
        start_time = time.time()
        
        # Detect language
        if language is None:
            detected_lang = detect_language_from_metadata(stt_metadata)
            if detected_lang is None:
                detected_lang = detect_language(query, self.supported_languages)
            language = detected_lang
        
        logger.info(f"[{session_id}] 🔍 Language detected: {language} | Query: {query[:100]}")
        
        # Intent classification (if enabled)
        intent_result = None
        if self.intent_client.config.enabled:
            try:
                intent_result = await self.intent_client.classify(query, session_id)
            except Exception as e:
                logger.error(f"[{session_id}] Intent classification error: {e}")
        
        # Merge form_data into context (intent_result)
        context = intent_result
        if form_data:
            if context is None:
                context = {"form_data": form_data}
            else:
                context = {**context, "form_data": form_data}
        
        # RAG query
        try:
            rag_result = await self.rag_client.query(
                query=query,
                session_id=session_id,
                user_id=user_id,
                language=language,
                context=context,
                base_url=rag_url,
                tenant_id=tenant_id
            )
            
            duration_ms = (time.time() - start_time) * 1000
            logger.info(f"[{session_id}] ✅ Pipeline complete | Language: {language} | Duration: {duration_ms:.0f}ms")
            
            return {
                "answer": rag_result.get("answer", ""),
                "language": language,
                "sources": rag_result.get("sources", []),
                "confidence": rag_result.get("confidence", 0.0),
                "duration_ms": duration_ms
            }
        
        except Exception as e:
            logger.error(f"[{session_id}] ❌ Pipeline error: {e}", exc_info=True)
            error_msg = "I'm sorry, I encountered an error processing your request."
            if language == "de":
                error_msg = "Entschuldigung, es ist ein Fehler aufgetreten."
            
            return {
                "answer": error_msg,
                "language": language,
                "sources": [],
                "confidence": 0.0,
                "error": str(e)
            }




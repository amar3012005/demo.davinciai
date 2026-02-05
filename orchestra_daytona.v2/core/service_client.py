"""
Generic Service Client for Orchestra-daytona

Handles interactions with STT, TTS, and RAG services via HTTP/WebSocket.
Supports multiple service types and configurations.
"""

import asyncio
import json
import logging
import aiohttp
import ssl
from typing import Optional, Dict, Any, AsyncGenerator, List
from dataclasses import dataclass

from config_loader import STTConfig, TTSConfig, RAGConfig, IntentConfig

logger = logging.getLogger(__name__)


class STTClient:
    """Client for Speech-to-Text service"""
    
    def __init__(self, config: STTConfig, skip_ssl: bool = False):
        self.config = config
        self.skip_ssl = skip_ssl
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
    
    async def connect(self, session_id: str, retries: int = 3, retry_delay: float = 2.0) -> bool:
        """
        Connect to STT service WebSocket with retries.
        """
        # Check if already connected and active
        if self.ws and not self.ws.closed:
            logger.debug(f"STT: Already connected for session {session_id}")
            return True

        for attempt in range(retries):
            try:
                ws_url = self.config.url.replace("http://", "ws://").replace("https://", "wss://")
                ws_url = f"{ws_url}/api/v1/transcribe/stream?session_id={session_id}"
                
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession()
                
                logger.info(f"🔌 STT: Connecting to {ws_url} (Attempt {attempt + 1}/{retries})...")
                
                ssl_context = False if self.skip_ssl else None
                self.ws = await asyncio.wait_for(
                    self.session.ws_connect(ws_url, ssl=ssl_context),
                    timeout=2.0
                )
                
                logger.info(f"✅ STT WebSocket connected: {ws_url}")
                return True
            
            except Exception as e:
                logger.warning(f"⚠️ STT connection attempt {attempt + 1} failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))  # Simple backoff
                else:
                    logger.error(f"❌ STT: All {retries} connection attempts failed")
        
        return False
    
    async def send_audio(self, audio_bytes: bytes):
        """Send audio chunk to STT service with event loop yielding to prevent starvation"""
        if not self.ws or self.ws.closed:
            return

        try:
            # CRITICAL: Yield BEFORE sending to give receive loop a chance to run
            await asyncio.sleep(0)
            await self.ws.send_bytes(audio_bytes)
            # CRITICAL: Yield AFTER sending to ensure fair scheduling
            await asyncio.sleep(0)
            logger.debug(f"STT: Sent {len(audio_bytes)} bytes to STT service")
        except (aiohttp.ClientConnectionError, RuntimeError) as e:
            logger.warning(f"⚠️ STT send failed: {e}")
            # Ensure we don't try to use this dead connection again
            try:
                await self.close()
            except Exception:
                pass
            self.ws = None
        except Exception as e:
            logger.error(f"❌ Unexpected STT send error: {e}")
    
    async def receive_transcript(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Receive transcript messages and events from STT service
        
        Yields:
            Dict with 'type' ('transcript' or 'event'), 'text', 'is_final', 'event_type', etc.
        """
        if not self.ws:
            logger.error("STT WebSocket not connected")
            return
        
        try:
            while True:
                # CRITICAL: Check connection state before waiting for messages
                # This prevents blocking on closed connections and allows other tasks to run
                if not self.ws or self.ws.closed:
                    logger.warning("STT WebSocket closed during receive")
                    break
                
                # CRITICAL: Yield before waiting to ensure fair scheduling
                # This is especially important when waiting for messages while audio is being sent
                await asyncio.sleep(0)
                
                try:
                    msg = await asyncio.wait_for(
                        self.ws.receive(),
                        timeout=5.0
                    )
                    
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        msg_type = data.get("type")
                        
                        if msg_type == "events":
                            # Handle VAD events (speech_start, speech_end)
                            event_data = data.get("data", {})
                            yield {
                                "type": "event",
                                "event_type": event_data.get("event_type"),
                                "signal_type": event_data.get("signal_type"),
                                "timestamp": event_data.get("timestamp"),
                                **event_data
                            }
                        elif msg_type == "data":
                            # Handle transcript messages (existing logic)
                            transcript_data = data.get("data", {})
                            yield {
                                "type": "transcript",
                                "text": transcript_data.get("transcript", ""),
                                "is_final": transcript_data.get("is_final", False),
                                **transcript_data
                            }
                        else:
                            # Fallback: assume it's a transcript message (backward compatibility)
                            yield {
                                "type": "transcript",
                                "text": data.get("text", ""),
                                "is_final": data.get("is_final", False),
                                **data
                            }
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        # Some STT services send binary data
                        logger.debug("Received binary data from STT")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        logger.warning("STT WebSocket closed or error")
                        break
                
                except asyncio.TimeoutError:
                    # Timeout is normal - just continue waiting
                    # CRITICAL: Yield after timeout to ensure other tasks get CPU time
                    # This is especially important when waiting for messages while audio is being sent
                    await asyncio.sleep(0)
                    continue
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse STT message: {e}")
                    continue
        
        except Exception as e:
            logger.error(f"STT receive error: {e}", exc_info=True)
            # Don't silently fail - log full traceback
    
    async def close(self):
        """Close STT connection (WebSocket and session)"""
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self.session:
            await self.session.close()
            self.session = None
        # Allow time for underlying transport to close
        await asyncio.sleep(0.01)


class TTSClient:
    """Client for Text-to-Speech service"""
    
    def __init__(self, config: TTSConfig, skip_ssl: bool = False):
        self.config = config
        self.skip_ssl = skip_ssl
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.session_id: Optional[str] = None  # Track session_id for reconnection
        
        # Chunk batching (to prevent detected_unusual_activity from tiny chunks)
        self._chunk_buffer: str = ""
        self._min_chunk_size: int = 20  # Minimum chars before sending
        self._max_chunk_size: int = 150  # Maximum chars per chunk
        self._flush_task: Optional[asyncio.Task] = None
    
    async def connect(self, session_id: str, retries: int = 3, retry_delay: float = 2.0) -> bool:
        """
        Connect to TTS service WebSocket with retries.
        """
        # Check if already connected and active
        if self.ws and not self.ws.closed:
            logger.debug(f"TTS: Already connected for session {session_id}")
            return True

        for attempt in range(retries):
            try:
                ws_url = self.config.url.replace("http://", "ws://").replace("https://", "wss://")
                ws_url = f"{ws_url}/api/v1/stream?session_id={session_id}"
                
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession()
                
                logger.info(f"🔌 TTS: Connecting to {ws_url} (Attempt {attempt + 1}/{retries})...")
                
                ssl_context = False if self.skip_ssl else None
                self.ws = await asyncio.wait_for(
                    self.session.ws_connect(ws_url, ssl=ssl_context),
                    timeout=10.0
                )
                
                # Prewarm the TTS connection
                await self.ws.send_json({
                    "type": "prewarm"
                })
                
                self.session_id = session_id
                logger.info(f"✅ TTS WebSocket connected: {ws_url}")
                return True
            
            except Exception as e:
                logger.warning(f"⚠️ TTS connection attempt {attempt + 1} failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                else:
                    logger.error(f"❌ TTS: All {retries} connection attempts failed")
        
        return False
    
    async def synthesize(self, text: str, language: str = "en", emotion: str = "helpful", voice_id: Optional[str] = None):
        """
        Send synthesis request to TTS service
        
        Args:
            text: Text to synthesize
            language: Language code (en, de)
            emotion: Emotion/voice style
        """
        # Ensure connection exists and is open
        if not self.ws or self.ws.closed:
            if self.session_id:
                logger.warning("TTS WebSocket closed, reconnecting...")
                connected = await self.connect(self.session_id)
                if not connected:
                    logger.error("Failed to reconnect TTS WebSocket")
                    return
            else:
                logger.error("TTS WebSocket not connected and no session_id available")
                return
        
        # Get voice config for language
        voice_config = self.config.voices.get(language)
        if voice_id is None:
            voice_id = voice_config.voice_id if voice_config else "default"
        voice_lang = voice_config.language if voice_config else f"{language}-{language.upper()}"
        
        # Retry logic if send fails
        try:
            await self.ws.send_json({
                "type": "synthesize",
                "text": text,
                "emotion": emotion,
                "voice": voice_id,
                "language": voice_lang
            })
        except Exception as e:
            logger.error(f"TTS send failed: {e}, attempting reconnection...")
            if self.session_id:
                connected = await self.connect(self.session_id)
                if connected:
                    # Retry send after reconnection
                    await self.ws.send_json({
                        "type": "synthesize",
                        "text": text,
                        "emotion": emotion,
                        "voice": voice_id,
                        "language": voice_lang
                    })
                else:
                    logger.error("Failed to reconnect TTS WebSocket after send error")
            else:
                logger.error("Cannot reconnect: no session_id available")
    
    async def stream_chunk(self, text: str, language: str = "en", emotion: str = "helpful", voice_id: Optional[str] = None):
        """
        Send streaming text chunk to TTS service with intelligent batching.
        
        Small chunks are buffered and combined to prevent detected_unusual_activity errors.
        
        Args:
            text: Text chunk to synthesize
            language: Language code
            emotion: Emotion/voice style
        """
        # Ensure connection exists and is open
        if not self.ws or self.ws.closed:
            if self.session_id:
                logger.warning("TTS WebSocket closed during streaming, reconnecting...")
                connected = await self.connect(self.session_id)
                if not connected:
                    logger.error("Failed to reconnect TTS WebSocket")
                    return
            else:
                logger.error("TTS WebSocket not connected and no session_id available")
                return
        
        # CRITICAL FIX: Batch small chunks to prevent rate limiting
        self._chunk_buffer += text
        
        # If buffer is large enough OR chunk is already large, send immediately
        if len(self._chunk_buffer) >= self._min_chunk_size or len(text) >= self._max_chunk_size:
            await self._flush_chunks(language, emotion, voice_id)
        else:
            # Schedule delayed flush (small chunks accumulate)
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            self._flush_task = asyncio.create_task(self._delayed_flush(language, emotion, voice_id))
    
    async def _delayed_flush(self, language: str, emotion: str, voice_id: Optional[str] = None, delay_ms: float = 50.0):
        """Flush buffered chunks after a short delay."""
        await asyncio.sleep(delay_ms / 1000.0)
        await self._flush_chunks(language, emotion, voice_id)
    
    async def _flush_chunks(self, language: str, emotion: str, voice_id: Optional[str] = None):
        """Send buffered chunks to TTS service."""
        if not self._chunk_buffer:
            return
        
        # Split buffer into optimal-sized chunks
        chunks_to_send = []
        buffer = self._chunk_buffer
        self._chunk_buffer = ""
        
        # Split by sentences if possible, otherwise by max size
        while len(buffer) > self._max_chunk_size:
            # Try to split at sentence boundary
            split_pos = buffer.rfind('. ', 0, self._max_chunk_size)
            if split_pos == -1:
                split_pos = buffer.rfind(' ', 0, self._max_chunk_size)
            if split_pos == -1:
                split_pos = self._max_chunk_size
            
            chunks_to_send.append(buffer[:split_pos + 1].strip())
            buffer = buffer[split_pos + 1:].strip()
        
        if buffer:
            chunks_to_send.append(buffer)
        
        voice_config = self.config.voices.get(language)
        if voice_id is None:
            voice_id = voice_config.voice_id if voice_config else "default"
        voice_lang = voice_config.language if voice_config else f"{language}-{language.upper()}"
        
        # Send each chunk with small delay between them
        for i, chunk in enumerate(chunks_to_send):
            if i > 0:
                # Small delay between chunks (already batched, but still pace them)
                await asyncio.sleep(0.01)  # 10ms between batched chunks
            
            logger.debug(f"TTS stream_chunk: sending '{chunk[:50]}...' ({len(chunk)} chars, lang={language})")
            
            # Retry logic if send fails
            try:
                await self.ws.send_json({
                    "type": "stream_chunk",
                    "text": chunk,
                    "emotion": emotion,
                    "voice": voice_id,
                    "language": voice_lang
                })
            except Exception as e:
                logger.error(f"TTS stream_chunk send failed: {e}, attempting reconnection...")
                if self.session_id:
                    connected = await self.connect(self.session_id)
                    if connected:
                        # Retry send after reconnection
                        await self.ws.send_json({
                            "type": "stream_chunk",
                            "text": chunk,
                            "emotion": emotion,
                            "voice": voice_id,
                            "language": voice_lang
                        })
                    else:
                        logger.error("Failed to reconnect TTS WebSocket after stream_chunk error")
                else:
                    logger.error("Cannot reconnect: no session_id available")

    async def stream_end(self):
        """
        Signal end of text streaming to TTS service.
        This triggers EOS to ElevenLabs to finalize audio generation.
        Flushes any remaining buffered chunks first.
        """
        # CRITICAL FIX: Flush any remaining buffered chunks before ending stream
        if self._chunk_buffer:
            logger.debug(f"Flushing remaining buffer before stream_end: '{self._chunk_buffer[:50]}...'")
            # Cancel delayed flush task if running
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            # Get language/emotion from last chunk (or use defaults)
            await self._flush_chunks("en", "helpful")
        
        if not self.ws or self.ws.closed:
            if self.session_id:
                logger.warning("TTS WebSocket closed, reconnecting for stream_end...")
                connected = await self.connect(self.session_id)
                if not connected:
                    logger.error("Failed to reconnect TTS WebSocket for stream_end")
                    return
            else:
                logger.error("TTS WebSocket not connected and no session_id available")
                return

        logger.info("TTS stream_end: signaling end of text stream")
        try:
            await self.ws.send_json({
                "type": "stream_end"
            })
        except Exception as e:
            logger.error(f"TTS stream_end send failed: {e}")
            # Don't retry stream_end - it's not critical if it fails

    async def receive_audio(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Receive audio chunks from TTS service
        
        Yields:
            Dict with 'type', 'data' (base64 audio), and other metadata
        """
        if not self.ws:
            logger.error("TTS WebSocket not connected")
            return
        
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        self.ws.receive(),
                        timeout=30.0
                    )
                    
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        yield data
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        # Handle raw binary audio chunks
                        yield {
                            "type": "audio",
                            "data": msg.data,
                            "is_binary": True
                        }
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        logger.warning("TTS WebSocket closed or error")
                        break
                
                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse TTS message: {e}")
                    continue
        
        except Exception as e:
            logger.error(f"TTS receive error: {e}")
    
    async def close(self):
        """Close TTS connection (WebSocket and session)"""
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self.session:
            await self.session.close()
            self.session = None
        # Allow time for underlying transport to close
        await asyncio.sleep(0.01)


class RAGClient:
    """Client for RAG (Retrieval-Augmented Generation) service"""
    
    def __init__(self, config: RAGConfig, skip_ssl: bool = False):
        self.config = config
        self.skip_ssl = skip_ssl
    
    async def query(self, 
                   query: str, 
                   session_id: str,
                   user_id: Optional[str] = None,
                   language: str = "en",
                   context: Optional[Dict[str, Any]] = None,
                   base_url: Optional[str] = None,
                   tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Query RAG service
        
        Args:
            query: User query text
            session_id: Session identifier
            language: Language code
            context: Optional context/intent information
        
        Returns:
            Dict with 'answer', 'sources', 'confidence', etc.
        """
        try:
            async with aiohttp.ClientSession() as session:
                ssl_context = False if self.skip_ssl else None
                payload = {
                    "query": query,
                    "session_id": session_id,
                    "user_id": user_id,
                    "language": language,
                    "top_k": self.config.top_k,
                    "similarity_threshold": self.config.similarity_threshold
                }
                
                if context:
                    payload["context"] = context
                if tenant_id:
                    payload["tenant_id"] = tenant_id
                
                url = base_url if base_url else self.config.url
                async with session.post(
                    f"{url}/api/v1/query",
                    json=payload,
                    ssl=ssl_context,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        logger.error(f"RAG service error: HTTP {resp.status}")
                        return {
                            "answer": "",
                            "sources": [],
                            "confidence": 0.0,
                            "error": f"HTTP {resp.status}"
                        }
        
        except asyncio.TimeoutError:
            logger.error("RAG service timeout")
            return {
                "answer": "",
                "sources": [],
                "confidence": 0.0,
                "error": "timeout"
            }
        except Exception as e:
            logger.error(f"RAG service error: {e}")
            return {
                "answer": "",
                "sources": [],
                "confidence": 0.0,
                "error": str(e)
            }
    
    async def query_streaming(self,
                            query: str,
                            session_id: str,
                            user_id: Optional[str] = None,
                            language: str = "en",
                            context: Optional[Dict[str, Any]] = None,
                            history_context: Optional[str] = None,
                            base_url: Optional[str] = None,
                            tenant_id: Optional[str] = None) -> AsyncGenerator[str, None]:
        """
        Query RAG service with streaming response
        
        Args:
            query: User query text
            session_id: Session identifier
            user_id: Optional user identifier for Hive Mind
            language: Language code
            context: Optional context/intent information
            history_context: Optional conversation history for context-aware responses
        
        Yields:
            Text tokens as they arrive
        """
        try:
            async with aiohttp.ClientSession() as session:
                ssl_context = False if self.skip_ssl else None
                payload = {
                    "query": query,
                    "user_id": user_id,
                    "language": language  # Pass language to RAG for correct response language
                }

                if context:
                    payload["context"] = context
                if tenant_id:
                    payload["tenant_id"] = tenant_id
                if history_context:
                    payload["history_context"] = history_context
                    logger.debug(f"Sending to RAG with history_context: {len(history_context)} chars, language: {language}")
                else:
                    logger.debug(f"Sending to RAG without history_context, language: {language}")

                # Use the correct streaming endpoint for Daytona RAG
                url = base_url if base_url else self.config.url
                stream_url = f"{url}{self.config.stream_endpoint}"
                
                async with session.post(
                    stream_url,
                    json=payload,
                    ssl=ssl_context,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        async for line in resp.content:
                            line_str = line.decode('utf-8').strip()
                            if line_str:  # Parse NDJSON (not SSE)
                                try:
                                    data = json.loads(line_str)
                                    token = data.get("text", "")
                                    is_final = data.get("is_final", False)
                                    
                                    logger.debug(f"RAG streaming received: '{token}' (is_final={is_final})")
                                    
                                    # Yield token if it has content, even if it's the final chunk
                                    # (Conversational fast-paths often send only one chunk marked as final)
                                    if token:
                                        logger.debug(f"RAG yielding token: '{token}'")
                                        yield token
                                    
                                    if is_final:
                                        logger.debug(f"RAG streaming marked as final")
                                except json.JSONDecodeError as e:
                                    logger.error(f"RAG JSON parse error: {e} for line: {line_str}")
                                    continue
                    else:
                        logger.error(f"RAG streaming error: HTTP {resp.status}")
        
        except Exception as e:
            logger.error(f"RAG streaming error: {e}")

    async def visual_orchestrate(self,
                               query: str,
                               session_id: str,
                               dom_context: List[Dict[str, Any]],
                               history_context: Optional[str] = None,
                               language: str = "en",
                               base_url: Optional[str] = None,
                               tenant_id: Optional[str] = None) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Query RAG service for Visual Co-Pilot (Dual-Stream).
        Returns NDJSON objects: {"type": "voice"|"action", "content": "..."|"payload": {...}}
        """
        try:
            async with aiohttp.ClientSession() as session:
                ssl_context = False if self.skip_ssl else None
                payload = {
                    "query": query,
                    "session_id": session_id,
                    "dom_context": dom_context,
                    "history_context": history_context,
                    "language": language,
                    "tenant_id": tenant_id
                }

                url = base_url if base_url else self.config.url
                orchestrate_url = f"{url}/api/v1/visual_orchestrate"
                
                async with session.post(
                    orchestrate_url,
                    json=payload,
                    ssl=ssl_context,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        async for line in resp.content:
                            line_str = line.decode('utf-8').strip()
                            if line_str:
                                try:
                                    yield json.loads(line_str)
                                except json.JSONDecodeError:
                                    continue
                    else:
                        logger.error(f"RAG visual_orchestrate error: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"RAG visual_orchestrate error: {e}")


class IntentClient:
    """Client for Intent classification service"""
    
    def __init__(self, config: IntentConfig):
        self.config = config
    
    async def classify(self, text: str, session_id: str) -> Dict[str, Any]:
        """
        Classify user intent
        
        Args:
            text: User input text
            session_id: Session identifier
        
        Returns:
            Dict with 'intent', 'confidence', 'context', etc.
        """
        if not self.config.enabled:
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "context": {}
            }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.config.url}/api/v1/classify",
                    json={"text": text, "session_id": session_id},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        logger.error(f"Intent service error: HTTP {resp.status}")
                        return {
                            "intent": "unknown",
                            "confidence": 0.0,
                            "context": {}
                        }
        
        except asyncio.TimeoutError:
            logger.error("Intent service timeout")
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "context": {}
            }
        except Exception as e:
            logger.error(f"Intent service error: {e}")
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "context": {}
            }




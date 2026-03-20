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
import unicodedata
import time
from typing import Optional, Dict, Any, AsyncGenerator, List
from dataclasses import dataclass
import re

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
        # CRITICAL: Increased buffering to prioritize prosody quality.
        # Sending whole sentences (or ~40 words) ensures Cartesia has enough context for natural intonation.
        self._min_chunk_size: int = 250  # was 40 — wait for a full thought
        self._max_chunk_size: int = 1000 # was 200 — allow much longer blocks
        self._flush_task: Optional[asyncio.Task] = None
        self._last_stream_language: str = "de"
        self._last_stream_emotion: str = "helpful"
        self._last_stream_voice_id: Optional[str] = None
        self._last_stream_pronunciation_dict_id: Optional[str] = None

    @staticmethod
    def _get_tenant_pronunciation_dict_id(tenant_id: Optional[str]) -> Optional[str]:
        """Get tenant-specific pronunciation dictionary ID for brand names."""
        if not tenant_id:
            return None
        # Map tenant IDs to their pronunciation dictionaries
        # Format: {tenant_id}_PRONUNCIATION_DICT_ID environment variable
        PRONUNCIATION_DICTS = {
            "bundb": "pdict_aiA2sefpW2w4nXqFjde8pa",  # BUNDB tenant
            # Add more tenants as needed
        }
        return PRONUNCIATION_DICTS.get(tenant_id.lower())

    # Acronyms Cartesia reads letter-by-letter — expand to spoken German
    # This dictionary is applied in _normalize_tts_text() before sending to TTS
    TTS_EXPAND = {
        "BLAIQ": "Blaiq",
        "KI": "künstliche Intelligenz",
        "DSGVO": "Datenschutz-Grundverordnung",
        "UX": "User Experience",
        "UI": "User Interface",
        "CEO": "Geschäftsführer",
        "HR": "Human Resources",
        "USP": "Alleinstellungsmerkmal",
        "ROI": "Return on Investment",
        "FAQ": "häufig gestellte Fragen",
        "CRM": "Kundenmanagement-System",
        "B2B": "Business-to-Business",
        "B2C": "Business-to-Consumer",
        "AI": "Davinci",  # Brand-specific for DaVinci AI
        "TARA": "Tara",  # Avoid spelling T-A-R-A
    }

    @staticmethod
    def _normalize_tts_text(text: str, language: str = "de", is_final: bool = True) -> str:
        """Normalize unicode/punctuation that causes character-by-character spelling in TTS."""
        if not text:
            return ""
        normalized = unicodedata.normalize("NFKC", text)
        replacements = {
            "\u00a0": " ",   # non-breaking space
            "\u2007": " ",
            "\u202f": " ",
            "\u00ad": "",    # soft hyphen
            "\u2010": "-",   # hyphen
            "\u2011": "-",   # non-breaking hyphen (CRITICAL for spelling J-O-H-N)
            "\u2012": "-",
            "\u2013": "-",   # en dash
            "\u2014": " - ", # em dash (added spaces for prosody)
            "\u2015": "-",   # horizontal bar
            "\u2212": "-",   # minus sign
            "\u2026": "... ", # ellipsis (added space for prosody)
        }
        for old, new in replacements.items():
            normalized = normalized.replace(old, new)

        # German-specific: Handle common abbreviations that TTS might mispronounce
        abbreviation_replacements = {
            r"\bz\.B\.": "zum Beispiel",
            r"\bd\.h\.": "das heißt",
            r"\bu\.a\.": "und andere",
            r"\betc\.": "und so weiter",
            r"\busw\.": "und so weiter",
            r"\bbspw\.": "beispielsweise",
        }
        for pattern, replacement in abbreviation_replacements.items():
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

        # Dot-separated tokens should stay punctuation-like, not be read out literally.
        # Replacing with a soft sentence break helps TTS pause instead of saying "dot".
        normalized = re.sub(r"(?<=\w)\.(?=\w)", ", ", normalized)

        # Avoid clipped acronym-hyphen compounds like "KI-gestützt" getting spelled out.
        normalized = re.sub(r"\b([A-ZÄÖÜ]{2,})-(?=\w)", r"\1 ", normalized)

        # German-specific: Replace slashes with "oder" for better TTS pronunciation
        normalized = re.sub(r"\s*/\s*", " oder ", normalized)

        # CRITICAL: Expand acronyms that Cartesia reads letter-by-letter
        # Only apply for German language
        if language == "de":
            for acronym, spoken in TTSClient.TTS_EXPAND.items():
                # Use word boundary matching to avoid partial replacements
                normalized = re.sub(rf"\b{re.escape(acronym)}\b", spoken, normalized, flags=re.IGNORECASE if acronym.isupper() else 0)

        # Ensure terminal punctuation (Cartesia needs this for prosody cues)
        # ONLY if this is the final chunk of the response.
        if is_final and normalized and not normalized[-1] in '.!?':
            normalized += '.'

        # Clean up multiple spaces
        normalized = re.sub(r"\s+", " ", normalized).strip()

        return normalized
    
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
                    self.session.ws_connect(
                        ws_url,
                        ssl=ssl_context,
                        heartbeat=20.0,
                        autoping=True,
                    ),
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
    
    async def synthesize(self, text: str, language: str = "de", emotion: str = "helpful", voice_id: Optional[str] = None, pronunciation_dict_id: Optional[str] = None, tenant_id: Optional[str] = None):
        """
        Send synthesis request to TTS service

        Args:
            text: Text to synthesize
            language: Language code (en, de)
            emotion: Emotion/voice style
            pronunciation_dict_id: Optional pronunciation dictionary ID override
            tenant_id: Optional tenant ID for auto-applying pronunciation dictionary
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

        # Normalize text with language-aware acronym expansion
        text = self._normalize_tts_text(text, language)
        self._last_stream_language = language or self._last_stream_language
        self._last_stream_emotion = emotion or self._last_stream_emotion

        # Auto-apply tenant-specific pronunciation dictionary if not provided
        if not pronunciation_dict_id and tenant_id:
            pronunciation_dict_id = self._get_tenant_pronunciation_dict_id(tenant_id)

        # Get voice config for language
        voice_config = self.config.voices.get(language)
        if voice_id is None:
            voice_id = voice_config.voice_id if voice_config else "default"
        voice_lang = voice_config.language if voice_config else f"{language}-{language.upper()}"
        self._last_stream_voice_id = voice_id
        self._last_stream_pronunciation_dict_id = pronunciation_dict_id

        # Retry logic if send fails
        try:
            await self.ws.send_json({
                "type": "synthesize",
                "text": text,
                "emotion": emotion,
                "voice": voice_id,
                "language": voice_lang,
                "pronunciation_dict_id": pronunciation_dict_id
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
                        "language": voice_lang,
                        "pronunciation_dict_id": pronunciation_dict_id
                    })
                else:
                    logger.error("Failed to reconnect TTS WebSocket after send error")
            else:
                logger.error("Cannot reconnect: no session_id available")
    
    async def stream_chunk(self, text: str, language: str = "de", emotion: str = "helpful", voice_id: Optional[str] = None, pronunciation_dict_id: Optional[str] = None, tenant_id: Optional[str] = None):
        """
        Send streaming text chunk to TTS service with intelligent batching.

        Small chunks are buffered and combined to prevent detected_unusual_activity errors.

        Args:
            text: Text chunk to synthesize
            language: Language code
            emotion: Emotion/voice style
            tenant_id: Optional tenant ID for auto-applying pronunciation dictionary
        """
        # Store metadata for flushing
        self._last_stream_language = language or self._last_stream_language
        self._last_stream_emotion = emotion or self._last_stream_emotion

        # Auto-apply tenant-specific pronunciation dictionary if not provided
        if not pronunciation_dict_id and tenant_id:
            pronunciation_dict_id = self._get_tenant_pronunciation_dict_id(tenant_id)

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

        voice_config = self.config.voices.get(language)
        if voice_id is None:
            voice_id = voice_config.voice_id if voice_config else "default"
        self._last_stream_voice_id = voice_id
        self._last_stream_pronunciation_dict_id = pronunciation_dict_id

        # CRITICAL FIX: Buffer RAW text. Normalization happens during flush to maintain context.
        self._chunk_buffer += text

        # Strip XML tags to see how much actual text we have for thresholding
        text_without_tags = re.sub(r'<[^>]+>', '', self._chunk_buffer).strip()
        incoming_without_tags = re.sub(r'<[^>]+>', '', text).strip()

        # QUALITY BOOST: Flush immediately if we see a sentence-ender in the incoming text
        # Include ellipses and dashes which are common in agent responses.
        has_punctuation = any(text.endswith(p) for p in ['.', '!', '?', '…']) or \
                         any(p in text for p in ['. ', '.<', '! ', '? ', '… ', '— '])

        # If buffer is large enough OR incoming chunk is already large OR we hit punctuation, send immediately
        if len(text_without_tags) >= self._min_chunk_size or len(incoming_without_tags) >= self._max_chunk_size or has_punctuation:
            await self._flush_chunks(language, emotion, voice_id, pronunciation_dict_id, is_final=False)
        else:
            # Schedule delayed flush (small chunks accumulate)
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            self._schedule_flush(language, emotion, voice_id, pronunciation_dict_id)
    
    def _schedule_flush(self, language: str, emotion: str, voice_id: Optional[str] = None, pronunciation_dict_id: Optional[str] = None, delay_ms: float = 50.0):
        """Schedule buffered chunks to be flushed after a short delay."""
        self._flush_task = asyncio.create_task(self._delayed_flush(language, emotion, voice_id, pronunciation_dict_id, delay_ms))

    async def _delayed_flush(self, language: str, emotion: str, voice_id: Optional[str] = None, pronunciation_dict_id: Optional[str] = None, delay_ms: float = 50.0):
        """Flush buffered chunks after a short delay."""
        await asyncio.sleep(delay_ms / 1000.0)
        await self._flush_chunks(language, emotion, voice_id, pronunciation_dict_id)
    
    async def _flush_chunks(self, language: str, emotion: str, voice_id: Optional[str] = None, pronunciation_dict_id: Optional[str] = None, is_final: bool = False):
        """Send buffered chunks to TTS service.

        CRITICAL: Cartesia requires streamed inputs to form valid transcript when joined.
        "Hello, world!" must be followed by " How are you?" (with leading space),
        not "How are you?" - otherwise they join as "Hello, world!How are you?" (invalid).

        German compound words like "Unternehmenskultur" must not be split mid-word.
        """
        if not self._chunk_buffer:
            return

        # Normalize the whole buffer FIRST to preserve context for abbreviations
        # But only add terminal period if is_final is True
        full_text = self._normalize_tts_text(self._chunk_buffer, language, is_final=is_final)
        self._chunk_buffer = ""

        if not full_text:
            return

        # Split normalized text into optimal-sized chunks for streaming
        chunks_to_send = []
        buffer = full_text

        # 1. SSML Tag safety: Don't split inside <emotion> or <break> tags
        # 2. German compound safety: Prefer splitting at sentence boundaries or spaces
        while len(buffer) > self._max_chunk_size:
            split_pos = buffer.rfind('. ', 0, self._max_chunk_size)
            if split_pos == -1:
                split_pos = buffer.rfind(' ', 0, self._max_chunk_size)
            if split_pos == -1:
                split_pos = self._max_chunk_size

            chunk = buffer[:split_pos].rstrip()
            chunks_to_send.append(chunk)

            # CRITICAL: Keep leading space for Cartesia continuity
            buffer = buffer[split_pos:].lstrip()
            if buffer and not buffer.startswith(' '):
                buffer = ' ' + buffer
        
        if buffer:
            chunks_to_send.append(buffer)
        
        voice_config = self.config.voices.get(language)
        if voice_id is None:
            voice_id = voice_config.voice_id if voice_config else "default"
        voice_lang = voice_config.language if voice_config else f"{language}-{language.upper()}"
        self._last_stream_language = language or self._last_stream_language
        self._last_stream_emotion = emotion or self._last_stream_emotion
        self._last_stream_voice_id = voice_id
        self._last_stream_pronunciation_dict_id = pronunciation_dict_id
        
        # Send each chunk
        for i, chunk in enumerate(chunks_to_send):
            if i > 0:
                await asyncio.sleep(0.01)
            
            logger.debug(f"TTS stream_chunk: sending '{chunk[:50]}...' ({len(chunk)} chars, is_final={is_final and i == len(chunks_to_send)-1})")
            
            try:
                await self.ws.send_json({
                    "type": "stream_chunk",
                    "text": chunk,
                    "emotion": emotion,
                    "voice": voice_id,
                    "language": voice_lang,
                    "pronunciation_dict_id": pronunciation_dict_id
                })
            except Exception as e:
                logger.error(f"TTS stream_chunk send failed: {e}")
                break

    async def stream_end(self, flush_buffer: bool = True):
        """
        Signal end of text streaming to TTS service.
        This triggers EOS to ElevenLabs to finalize audio generation.
        Optionally flushes any remaining buffered chunks first.
        """
        # CRITICAL FIX: Flush any remaining buffered chunks before ending stream
        if flush_buffer and self._chunk_buffer:
            logger.debug(f"Flushing remaining buffer before stream_end: '{self._chunk_buffer[:50]}...'")
            # Cancel delayed flush task if running
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            # Flush with is_final=True to ensure terminal punctuation
            await self._flush_chunks(
                self._last_stream_language,
                self._last_stream_emotion,
                self._last_stream_voice_id,
                self._last_stream_pronunciation_dict_id,
                is_final=True
            )
        
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

    def reset_buffer(self) -> None:
        """Clear buffered text without closing connection.

        Use this between batch-mode turns to avoid reconnect latency.
        Only call abort_stream() for true barge-in/interrupt scenarios.
        """
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._chunk_buffer = ""

    async def abort_stream(self):
        """
        Abort current TTS generation immediately without flushing buffered text.
        This is used for barge-in/interrupt so stale response text is not synthesized.
        """
        # Cancel delayed flush task and drop any buffered text so old turn cannot leak.
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._chunk_buffer = ""

        # Closing the socket forces server-side stream termination for this turn.
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"TTS abort close error: {e}")
            finally:
                self.ws = None

    async def receive_audio(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Receive audio chunks from TTS service
        
        Yields:
            Dict with 'type', 'data' (base64 audio), and other metadata
        """
        if not self.ws:
            # Startup race: the receive loop can start before the initial
            # websocket connect has finished. Wait briefly instead of
            # tearing down the loop and forcing a reconnect cycle.
            wait_started = time.time()
            while not self.ws and self.session and (time.time() - wait_started) < 5.0:
                await asyncio.sleep(0.05)
            if not self.ws:
                logger.error("TTS WebSocket not connected")
                return
        
        try:
            while True:
                current_ws = self.ws
                if not current_ws or current_ws.closed:
                    if not self.session_id:
                        logger.info("TTS WebSocket unavailable during receive loop; stopping audio receive")
                        break

                    logger.warning("TTS WebSocket unavailable during receive loop; attempting to recover connection")
                    try:
                        await self.connect(self.session_id)
                    except Exception as reconnect_error:
                        logger.debug(f"TTS receive reconnect attempt failed: {reconnect_error}")
                        await asyncio.sleep(0.25)
                        continue

                    if not self.ws or self.ws.closed:
                        await asyncio.sleep(0.25)
                        continue
                    current_ws = self.ws
                try:
                    msg = await asyncio.wait_for(
                        current_ws.receive(),
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
                            agent_name: Optional[str] = None,
                            language: str = "en",
                            context: Optional[Dict[str, Any]] = None,
                            history_context: Optional[str] = None,
                            session_summary_window: Optional[str] = None,
                            session_summary_revision: int = 0,
                            recent_turns: Optional[List[Dict[str, Any]]] = None,
                            base_url: Optional[str] = None,
                            tenant_id: Optional[str] = None,
                            interrupted_text: Optional[str] = None,
                            interruption_transcripts: Optional[List[str]] = None,
                            interruption_type: Optional[str] = None) -> AsyncGenerator[str, None]:
        """
        Query RAG service with streaming response
        
        Args:
            query: User query text
            session_id: Session identifier
            user_id: Optional user identifier for Hive Mind
            language: Language code
            context: Optional context/intent information
            history_context: Optional conversation history for context-aware responses
            interrupted_text: Assistant's response text that was interrupted (barge-in)
            interruption_transcripts: User's interruption transcripts collected during interruption
            interruption_type: Type of interruption ('addon', 'topic_change', 'clarification', 'noise')

        Yields:
            Text tokens as they arrive
        """
        try:
            async with aiohttp.ClientSession() as session:
                ssl_context = False if self.skip_ssl else None
                payload = {
                    "query": query,
                    "session_id": session_id,
                    "user_id": user_id,
                    "language": language  # Pass language to RAG for correct response language
                }
                if agent_name:
                    payload["agent_name"] = agent_name

                if context:
                    payload["context"] = context
                if tenant_id:
                    payload["tenant_id"] = tenant_id
                if history_context:
                    payload["history_context"] = history_context
                    logger.debug(f"Sending to RAG with history_context: {len(history_context)} chars, language: {language}")
                else:
                    logger.debug(f"Sending to RAG without history_context, language: {language}")
                if session_summary_window:
                    payload["session_summary_window"] = session_summary_window
                    payload["session_summary_revision"] = session_summary_revision
                if recent_turns:
                    payload["recent_turns"] = recent_turns

                # Add interruption context for barge-in handling
                if interrupted_text:
                    payload["interrupted_text"] = interrupted_text
                if interruption_transcripts:
                    payload["interruption_transcripts"] = interruption_transcripts
                if interruption_type:
                    payload["interruption_type"] = interruption_type

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
                                        # Yield llm_usage metadata if present in the final chunk
                                        llm_usage = data.get("llm_usage")
                                        if llm_usage:
                                            yield {"__llm_usage__": llm_usage}
                                        logger.debug(f"RAG streaming marked as final")
                                except json.JSONDecodeError as e:
                                    logger.error(f"RAG JSON parse error: {e} for line: {line_str}")
                                    continue
                    else:
                        logger.error(f"RAG streaming error: HTTP {resp.status}")
        
        except Exception as e:
            logger.error(f"RAG streaming error: {e}")

    async def summarize_session_window(
        self,
        *,
        session_id: str,
        tenant_id: str,
        language: str,
        previous_summary: str,
        user_text: str,
        assistant_text: str,
        base_url: Optional[str] = None,
    ) -> str:
        try:
            async with aiohttp.ClientSession() as session:
                ssl_context = False if self.skip_ssl else None
                url = base_url if base_url else self.config.url
                summary_url = f"{url}/api/v1/session_summary_window"
                payload = {
                    "session_id": session_id,
                    "tenant_id": tenant_id,
                    "language": language,
                    "previous_summary": previous_summary,
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                }
                async with session.post(
                    summary_url,
                    json=payload,
                    ssl=ssl_context,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Session summary request failed: HTTP {resp.status}")
                        return previous_summary
                    data = await resp.json()
                    return str(data.get("summary_text") or previous_summary or "")
        except Exception as e:
            logger.error(f"Session summary request failed: {e}")
            return previous_summary

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

    async def generate_exit(self, history_context: str, language: str = "en", base_url: Optional[str] = None) -> str:
        """Call RAG service to generate a dynamic exit message."""
        try:
            async with aiohttp.ClientSession() as session:
                ssl_context = False if self.skip_ssl else None
                payload = {
                    "history_context": history_context,
                    "language": language
                }

                url = base_url if base_url else self.config.url
                async with session.post(
                    f"{url}/api/v1/generate_exit",
                    json=payload,
                    ssl=ssl_context,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("exit_speech", "")
                    else:
                        logger.error(f"RAG generate_exit error: HTTP {resp.status}")
                        return ""
        except Exception as e:
            logger.error(f"RAG generate_exit client error: {e}")
            return ""

    async def route_fsm_turn(
        self,
        user_text: str,
        session_id: str,
        tenant_id: str,
        language: str,
        fsm_context: Dict[str, Any],
        base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Route FSM turn during appointment booking via RAG service.
        
        Args:
            user_text: User input text
            session_id: Session identifier
            tenant_id: Tenant identifier
            language: Response language
            fsm_context: Current FSM state (active, pending_field, collected_data, retry_counts, schema)
            base_url: Optional override for RAG service URL
            
        Returns:
            Dict with: action, field, normalized_value, confidence, reason, resume_prompt, cancelled
        """
        try:
            async with aiohttp.ClientSession() as session:
                ssl_context = False if self.skip_ssl else None
                payload = {
                    "user_text": user_text,
                    "session_id": session_id,
                    "tenant_id": tenant_id,
                    "language": language,
                    "fsm_context": fsm_context
                }

                url = base_url if base_url else self.config.url
                route_url = f"{url}/api/v1/fsm/route"

                async with session.post(
                    route_url,
                    json=payload,
                    ssl=ssl_context,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        logger.error(f"RAG FSM route error: HTTP {resp.status}")
                        return {
                            "action": "invalid_retry",
                            "field": None,
                            "normalized_value": None,
                            "confidence": 0.0,
                            "reason": f"HTTP {resp.status}",
                            "resume_prompt": None,
                            "cancelled": False
                        }
        except asyncio.TimeoutError:
            logger.error("RAG FSM route timeout")
            return {
                "action": "invalid_retry",
                "field": None,
                "normalized_value": None,
                "confidence": 0.0,
                "reason": "timeout",
                "resume_prompt": None,
                "cancelled": False
            }
        except Exception as e:
            logger.error(f"RAG FSM route error: {e}")
            return {
                "action": "invalid_retry",
                "field": None,
                "normalized_value": None,
                "confidence": 0.0,
                "reason": str(e),
                "resume_prompt": None,
                "cancelled": False
            }


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

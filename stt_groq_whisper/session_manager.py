"""
Session Manager for Groq Whisper STT

Manages WebSocket sessions, audio micro-chunking, context chaining, and transcript forwarding.
This implements the "micro-chunking loop" strategy for ultra-low latency transcription.
"""

import asyncio
import audioop
import time
import logging
import json
import re
from typing import Dict, Any, Optional, Callable, List
from collections import deque

from config import GroqWhisperConfig
from groq_client import GroqWhisperClient, GroqTranscriptionResult
from orchestrator_client import OrchestratorWSClient

logger = logging.getLogger(__name__)


class GroqWhisperSession:
    """
    Manages a single STT session with Groq Whisper.
    
    Implements the micro-chunking strategy:
    1. Buffer 300ms audio chunks
    2. Send to Groq API with context from previous transcriptions
    3. Stream results immediately to orchestrator and client
    """
    
    def __init__(
        self,
        session_id: str,
        config: GroqWhisperConfig,
        callback: Callable,
        redis_client=None,
        orchestrator_url: str = None
    ):
        """
        Initialize session.
        
        Args:
            session_id: Unique session identifier
            config: GroqWhisperConfig instance
            callback: Async function to call with transcript results
            redis_client: Optional Redis client (deprecated)
            orchestrator_url: Direct WebSocket URL to orchestrator
        """
        self.session_id = session_id
        self.config = config
        self.callback = callback
        self.redis = redis_client  # Deprecated
        
        # Groq client
        self.groq_client: Optional[GroqWhisperClient] = None
        
        # Orchestrator WebSocket
        self.orchestrator_ws: Optional[OrchestratorWSClient] = None
        if orchestrator_url:
            self.orchestrator_ws = OrchestratorWSClient(
                orchestrator_url, 
                session_id,
                skip_ssl=config.skip_ssl_verify
            )
            logger.info(f"✅ [{session_id}] Direct orchestrator WebSocket enabled: {orchestrator_url}")
        
        # Audio buffering for micro-chunking
        self.audio_buffer = bytearray()
        self.overlap_buffer = bytearray()  # Stores overlap from previous chunk
        self.full_audio_buffer = bytearray()  # For full transcription mode
        self.pre_speech_chunks: deque = deque()
        self.pre_speech_bytes: int = 0
        self._just_seeded_from_preroll: bool = False
        self.last_audio_time = 0.0
        
        # Context chaining - sliding window of recent transcripts
        self.context_window: deque = deque(maxlen=config.context_window_segments)
        
        # Speech state tracking
        self.speech_active = False
        self.last_speech_time = 0.0
        self.silence_start_time: Optional[float] = None
        self.speech_candidate_start_time: Optional[float] = None
        self.speech_segment_start_time: Optional[float] = None
        
        # Accumulated transcript for current speech segment
        self.current_utterance = ""
        self.partial_text = ""
        self._active_utterance_id = 0
        
        # Processing state
        self.is_processing = False
        self._process_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Metrics
        self.chunks_received = 0
        self.chunks_processed = 0
        self.transcripts_emitted = 0
        self.start_time = time.time()
        
        # Silence detection
        self.consecutive_silent_chunks = 0

    def _append_pre_speech_chunk(self, audio_chunk: bytes):
        """Keep a rolling pre-speech PCM buffer (for start padding)."""
        if self.config.enable_micro_chunking:
            return
        self.pre_speech_chunks.append(bytes(audio_chunk))
        self.pre_speech_bytes += len(audio_chunk)
        max_pre_bytes = int((self.config.sample_rate * 2 * self.config.pre_speech_padding_ms) / 1000)
        while self.pre_speech_bytes > max_pre_bytes and self.pre_speech_chunks:
            removed = self.pre_speech_chunks.popleft()
            self.pre_speech_bytes -= len(removed)

    def _resolved_language(self) -> str:
        """Use a normalized per-session language, defaulting to German."""
        return self.config.resolve_language(self.config.language)

    def _is_likely_sentence_continuation(self, text: str) -> bool:
        sample = (text or "").strip().lower()
        if not sample:
            return False
        if sample.endswith((
            " and", " or", " but", " because", " with", " for", " to", " the",
            " und", " oder", " aber", " weil", " mit", " fuer", " für", " zu",
        )):
            return True
        return sample.endswith((",", ":", ";", "-", "/"))

    def _is_stale_utterance(self, utterance_id: int) -> bool:
        """Drop finals from interrupted or cleaned-up utterances."""
        return not self._running or utterance_id != self._active_utterance_id
    
    async def start(self) -> bool:
        """Start session and initialize Groq client"""
        try:
            self.groq_client = GroqWhisperClient(self.config)
            self._running = True
            
            logger.info(f"✅ Session {self.session_id} started (Groq Whisper)")
            logger.info(f"   Model: {self.config.model}")
            mode = "MICRO-CHUNKING" if self.config.enable_micro_chunking else "FULL TRANSCRIPTION"
            logger.info(f"   Mode: {mode}")
            if self.config.enable_micro_chunking:
                logger.info(f"   Chunk duration: {self.config.chunk_duration_ms}ms")
            
            # Connect to orchestrator if URL provided
            if self.orchestrator_ws:
                await self.orchestrator_ws.connect()
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Error starting session {self.session_id}: {e}")
            return False
    
    async def process_audio_chunk(self, audio_chunk: bytes):
        """
        Process incoming audio chunk from client.
        
        Implements micro-chunking:
        1. Buffer audio until we have enough for a chunk
        2. Check VAD to avoid processing silence
        3. Send to Groq with context chaining
        4. Stream results immediately
        """
        if not self._running or not self.groq_client:
            return
        
        self.chunks_received += 1
        self.last_audio_time = time.time()
        
        now = time.time()
        self._append_pre_speech_chunk(audio_chunk)

        # Local VAD gate - check energy level
        try:
            energy = audioop.rms(audio_chunk, 2)  # 2 bytes per sample for PCM16
        except Exception:
            energy = 0
        
        is_speech = energy > self.config.vad_energy_threshold
        
        if is_speech:
            self.consecutive_silent_chunks = 0
            self.last_speech_time = now
            self.silence_start_time = None
            if self.speech_candidate_start_time is None:
                self.speech_candidate_start_time = now
            
            # Emit SPEECH_START if not already active
            if not self.speech_active:
                speech_candidate_ms = (now - self.speech_candidate_start_time) * 1000.0
                if speech_candidate_ms >= self.config.speech_start_trigger_ms:
                    self.speech_active = True
                    self.speech_segment_start_time = now
                    self._active_utterance_id += 1
                    # Seed full buffer with a short pre-roll to avoid clipping initial phonemes.
                    if not self.config.enable_micro_chunking and self.pre_speech_chunks:
                        self.full_audio_buffer = bytearray(b"".join(self.pre_speech_chunks))
                        self._just_seeded_from_preroll = True
                    await self._emit_speech_event("SPEECH_START")
                    logger.info(
                        f"🎤 [{self.session_id}] Speech started "
                        f"(energy={energy}, debounce={speech_candidate_ms:.0f}ms)"
                    )
        else:
            self.consecutive_silent_chunks += 1
            self.speech_candidate_start_time = None
            
            if self.silence_start_time is None:
                self.silence_start_time = now
        
        # Add audio to buffer (always, for both modes)
        self.audio_buffer.extend(audio_chunk)
        
        # In full transcription mode, accumulate speech plus a short trailing pad.
        # This keeps endpointing natural while reducing finalization latency.
        if not self.config.enable_micro_chunking and self.speech_active:
            silence_ms = self._current_silence_ms()
            if self.silence_start_time is None or silence_ms <= self.config.final_silence_padding_ms:
                if self._just_seeded_from_preroll:
                    self._just_seeded_from_preroll = False
                else:
                    self.full_audio_buffer.extend(audio_chunk)
        
        # MICRO-CHUNKING MODE: Process chunks in real-time
        if self.config.enable_micro_chunking:
            # Check if we have enough audio for a chunk
            if len(self.audio_buffer) >= self.config.chunk_bytes:
                # Extract chunk with overlap
                chunk_end = self.config.chunk_bytes
                chunk_to_process = bytes(self.overlap_buffer) + bytes(self.audio_buffer[:chunk_end])
                
                # Save overlap for next chunk
                self.overlap_buffer = self.audio_buffer[chunk_end - self.config.overlap_bytes:chunk_end]
                self.audio_buffer = self.audio_buffer[chunk_end:]
                
                # Process chunk if speech is active (or we are within the configurable
                # post-speech silence window before considering the utterance complete).
                silence_ms = self._current_silence_ms()
                if self.speech_active or silence_ms < self._speech_end_threshold_ms():
                    asyncio.create_task(self._process_chunk(chunk_to_process))

        # Check for speech end (both modes)
        if self.speech_active and self._current_silence_ms() >= self._speech_end_threshold_ms():
            if self._is_likely_sentence_continuation(self.current_utterance or self.partial_text):
                # Give the speaker a little more room if the transcript looks incomplete.
                self.silence_start_time = now
                return
            await self._handle_speech_end()

    def _current_silence_ms(self) -> float:
        """Return elapsed silence duration in milliseconds."""
        if self.silence_start_time is None:
            return 0.0
        return (time.time() - self.silence_start_time) * 1000.0

    def _speech_duration_ms(self) -> float:
        """Return the approximate duration of the active speech segment."""
        if self.speech_segment_start_time is None:
            return 0.0
        return max(0.0, (time.time() - self.speech_segment_start_time) * 1000.0)

    def _speech_end_threshold_ms(self) -> float:
        """
        Use a faster baseline silence threshold, then add a small continuation
        buffer for longer utterances so brief internal pauses do not finalize too early.
        """
        threshold = float(self.config.min_silence_duration_ms)
        if not self.config.adaptive_endpointing or not self.speech_active:
            return threshold

        utterance_text = (self.current_utterance or self.partial_text or "").strip()
        utterance_words = re.findall(r"[a-zA-Z0-9']+", utterance_text)
        utterance_text_lower = utterance_text.lower()
        if (
            len(utterance_words) >= self.config.continuation_word_threshold
            or self._speech_duration_ms() >= self.config.continuation_min_duration_ms
        ):
            threshold += float(self.config.continuation_silence_bonus_ms)

        # If the utterance looks syntactically incomplete, wait longer before finalizing.
        if utterance_text_lower.endswith((
            " and", " or", " but", " because", " with", " for", " to", " the",
            " und", " oder", " aber", " weil", " mit", " fuer", " für", " zu",
        )):
            threshold += 300.0
        elif utterance_text and utterance_text[-1] in {",", ":", ";", "-"}:
            threshold += 200.0

        if utterance_words:
            last_word = utterance_words[-1].lower()
            if last_word in {
                "and", "or", "but", "because", "with", "for", "to", "the",
                "und", "oder", "aber", "weil", "mit", "fuer", "für", "zu",
            }:
                threshold += 120.0

        if len(utterance_words) >= 10:
            threshold += 120.0

        return threshold
    
    async def _process_chunk(self, audio_bytes: bytes):
        """
        Process a single audio chunk through Groq API.
        
        Args:
            audio_bytes: Audio chunk to process (with overlap)
        """
        if not self.groq_client:
            return
        
        self.is_processing = True
        self.chunks_processed += 1
        
        try:
            # Build context prompt from previous transcriptions
            context_prompt = self._build_context_prompt()
            
            # Send to Groq API
            result = await self.groq_client.transcribe(
                audio_bytes=audio_bytes,
                prompt=context_prompt,
                language=self._resolved_language()
            )
            
            if result and not result.is_empty:
                # If speech already ended, this chunk is stale; don't emit noisy late partials.
                if not self.speech_active:
                    logger.debug(f"⏸️ [{self.session_id}] Dropping late micro-chunk result after speech end")
                    return

                # Filter prompt hallucination, regular hallucinations, and low-quality results
                is_hallucination = self._is_prompt_hallucination(result.text) or result.is_hallucination or result.is_low_quality
                if is_hallucination and not self._is_meaningful_single_word(result.text):
                    logger.info(f"🚫 [{self.session_id}] Filtered micro-chunk hallucination/noise: '{result.text}' (no_speech_prob={result.no_speech_prob or 0:.3f})")
                    return
                
                text = result.text
                
                # Update context window
                self.context_window.append(text)
                
                # Accumulate into current utterance
                if self.current_utterance:
                    # Avoid duplicating overlapping words
                    self.current_utterance = self._merge_text(self.current_utterance, text)
                else:
                    self.current_utterance = text
                
                self.partial_text = text
                
                # Emit partial transcript immediately for real-time UX
                await self._emit_transcript(
                    text=text,
                    is_final=False,
                    words=result.words,
                    latency_ms=self.groq_client.last_latency_ms,
                    avg_logprob=result.avg_logprob,
                    no_speech_prob=result.no_speech_prob,
                    compression_ratio=result.compression_ratio,
                    start_time=result.start,
                    end_time=result.end,
                    temperature=result.temperature
                )
                
                logger.info(f"📝 [{self.session_id}] Partial: '{text[:50]}...' ({self.groq_client.last_latency_ms:.0f}ms)")
                
        except Exception as e:
            logger.error(f"❌ [{self.session_id}] Error processing chunk: {e}")
        finally:
            self.is_processing = False
    
    def _build_context_prompt(self) -> Optional[str]:
        """Build context prompt from recent transcriptions"""
        if self.config.is_german_language(self._resolved_language()):
            # Context chaining can reinforce an accidental English normalization across chunks.
            # For German-first sessions, prefer clean native-language transcription over chain continuity.
            return None
        if not self.context_window:
            return None
        
        # Join recent transcripts as context
        context = " ".join(self.context_window)
        
        # Limit to last ~200 characters for efficiency
        if len(context) > 200:
            context = context[-200:]
        
        return context
    
    def _merge_text(self, previous: str, new: str) -> str:
        """
        Merge new text with previous, avoiding word duplication from overlap.
        
        Uses simple heuristic: if new text starts with end of previous, skip overlap.
        """
        if not previous:
            return new
        
        # Find potential overlap (last few words of previous)
        prev_words = previous.split()
        new_words = new.split()
        
        if not prev_words or not new_words:
            return previous + " " + new
        
        # Check for word overlap (last 3 words of previous vs first 3 of new)
        overlap_len = min(3, len(prev_words), len(new_words))
        
        for i in range(overlap_len, 0, -1):
            if prev_words[-i:] == new_words[:i]:
                # Found overlap, skip duplicate words
                return previous + " " + " ".join(new_words[i:])
        
        # No overlap found, concatenate
        return previous + " " + new
    
    async def _handle_speech_end(self):
        """Handle end of speech - emit final transcript"""
        if not self.speech_active:
            return
        
        closing_utterance_id = self._active_utterance_id
        self.speech_active = False
        self.speech_candidate_start_time = None
        self.speech_segment_start_time = None
        
        # FULL TRANSCRIPTION MODE: Transcribe entire buffer at once
        if not self.config.enable_micro_chunking and self.full_audio_buffer:
            audio_to_transcribe = bytes(self.full_audio_buffer)
            audio_duration_ms = len(self.full_audio_buffer) / (self.config.sample_rate * 2) * 1000
            
            logger.info(f"📤 [{self.session_id}] Full transcription mode: sending {len(audio_to_transcribe)} bytes ({audio_duration_ms:.0f}ms)")
            
            # In full mode, let the Groq client build the strict language-aware prompt.
            # Passing a prebuilt prompt here would get wrapped again and can exceed Groq's prompt limit.
            forced_language = self._resolved_language()

            # Transcribe full audio buffer
            result = await self.groq_client.transcribe(
                audio_bytes=audio_to_transcribe,
                prompt=None,
                language=forced_language
            )
            
            if self._is_stale_utterance(closing_utterance_id):
                logger.info(f"🧹 [{self.session_id}] Dropping stale full-transcription result after interruption/cleanup")
                return

            if result and not result.is_empty:
                final_text = result.text.strip()
                if self._should_filter_full_result(result, final_text):
                    logger.info(f"🚫 [{self.session_id}] Filtered full hallucination/noise: '{result.text}' (no_speech_prob={result.no_speech_prob or 0:.3f})")
                else:
                    if final_text:
                        await self._emit_transcript(
                            text=final_text,
                            is_final=True,
                            words=result.words,
                            latency_ms=self.groq_client.last_latency_ms,
                            avg_logprob=result.avg_logprob,
                            no_speech_prob=result.no_speech_prob,
                            compression_ratio=result.compression_ratio,
                            start_time=result.start,
                            end_time=result.end,
                            temperature=result.temperature
                        )
                        self.transcripts_emitted += 1
                        logger.info(f"✅ [{self.session_id}] Full transcript ({self.groq_client.last_latency_ms:.0f}ms): '{final_text[:80]}...'")
            
            # Clear full audio buffer
            self.full_audio_buffer.clear()
        
        # MICRO-CHUNKING MODE: Emit accumulated utterance as final
        else:
            # Give the last in-flight chunk a short chance to finish without
            # keeping speech end blocked for a full fixed sleep.
            wait_deadline = time.time() + (self.config.finalization_wait_ms / 1000.0)
            while self.is_processing and time.time() < wait_deadline:
                await asyncio.sleep(0.01)
            
            # Emit final transcript (accumulated utterance from chunks)
            final_text = self.current_utterance.strip() if self.current_utterance else self.partial_text.strip()
            
            if final_text:
                await self._emit_transcript(
                    text=final_text,
                    is_final=True,
                    latency_ms=self.groq_client.last_latency_ms if self.groq_client else None,
                    avg_logprob=getattr(self, 'last_avg_logprob', None),
                    no_speech_prob=getattr(self, 'last_no_speech_prob', None),
                    compression_ratio=getattr(self, 'last_compression_ratio', None),
                    start_time=getattr(self, 'last_start_time', None),
                    end_time=getattr(self, 'last_end_time', None),
                    temperature=getattr(self, 'last_temperature', None)
                )
                self.transcripts_emitted += 1
                logger.info(f"✅ [{self.session_id}] Final: '{final_text[:80]}...'")
        
        if self._is_stale_utterance(closing_utterance_id):
            logger.info(f"🧹 [{self.session_id}] Skipping stale speech_end cleanup after interruption/cleanup")
            return

        # Emit SPEECH_END event
        await self._emit_speech_event("SPEECH_END")
        logger.info(f"🔇 [{self.session_id}] Speech ended")
        
        # Reset state for next utterance
        self.current_utterance = ""
        self.partial_text = ""
        self.context_window.clear()
        self.pre_speech_chunks.clear()
        self.pre_speech_bytes = 0
        self._just_seeded_from_preroll = False

    def _is_meaningful_single_word(self, text: str) -> bool:
        """
        Allow valid one-word user intents (e.g. yes/no/stop/one) that are often
        dropped by aggressive noise filters.
        """
        words = re.findall(r"[a-zA-Z0-9']+", (text or "").lower())
        if len(words) != 1:
            return False
        token = words[0]
        reject = {"you", "thanks", "thank", "bye", "goodbye", "und"}
        allow_common = {
            "yes", "no", "ok", "okay", "stop", "wait", "go", "one", "two", "three", "hi", "hello",
            "ja", "nein", "halt", "warte", "weiter", "stopp", "okay", "okey", "hallo", "bitte"
        }
        if token in reject:
            return False
        return token in allow_common or len(token) >= 2

    def _is_prompt_hallucination(self, text: str) -> bool:
        """
        Check if transcription contains the prompt itself.
        This happens when Groq receives silence/noise and returns the prompt as transcription.
        """
        if not text:
            return False

        # Build the full prompt that would be sent to Groq
        full_prompt = self.config.build_transcription_prompt(language=self._resolved_language())
        if not full_prompt:
            return False

        text_normalized = text.strip().lower()
        prompt_normalized = full_prompt.strip().lower()

        # Check if transcription starts with or contains significant portions of the prompt
        # Check first 50 chars match (prompt usually starts with "Transcribe...")
        if len(text_normalized) >= 50 and len(prompt_normalized) >= 50:
            if text_normalized[:50] == prompt_normalized[:50]:
                return True

        # Check if prompt is contained in transcription (full prompt leak)
        if prompt_normalized in text_normalized:
            return True

        return False

    def _should_filter_full_result(self, result: GroqTranscriptionResult, final_text: str) -> bool:
        """Apply stricter noise filtering without dropping meaningful single-word inputs."""
        if self._is_meaningful_single_word(final_text):
            return False

        # CRITICAL: Filter prompt hallucination - when Groq returns the prompt itself as transcription
        if self._is_prompt_hallucination(final_text):
            logger.warning(f"🚫 [{self.session_id}] Prompt hallucination detected: '{final_text[:80]}...'")
            return True

        if self.config.is_german_language(self._resolved_language()):
            result_language = (result.language or "").strip().lower()
            if result_language.startswith("en"):
                logger.warning(f"🚫 [{self.session_id}] Filtering English result during German-forced session: '{final_text[:80]}...'")
                return True

        return result.is_hallucination or result.is_low_quality
    
    async def _emit_transcript(
        self,
        text: str,
        is_final: bool = False,
        words: Optional[list] = None,
        latency_ms: Optional[float] = None,
        avg_logprob: Optional[float] = None,
        no_speech_prob: Optional[float] = None,
        compression_ratio: Optional[float] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        temperature: Optional[float] = None
    ):
        """Emit transcript to client callback and orchestrator"""
        try:
            message = {
                "type": "data",
                "data": {
                    "transcript": text,
                    "is_final": is_final,
                    "request_id": f"{self.session_id}_{int(time.time() * 1000)}",
                    "timestamp": time.time(),
                    "source": "groq_whisper",
                    "language": self._resolved_language(),
                    "language_code": self._resolved_language(),
                }
            }
            
            if words:
                message["data"]["words"] = words
            if latency_ms:
                message["data"]["latency_ms"] = latency_ms
            
            # Add quality metrics
            if avg_logprob is not None:
                message["data"]["avg_logprob"] = avg_logprob
                self.last_avg_logprob = avg_logprob
            if no_speech_prob is not None:
                message["data"]["no_speech_prob"] = no_speech_prob
                self.last_no_speech_prob = no_speech_prob
            if compression_ratio is not None:
                message["data"]["compression_ratio"] = compression_ratio
                self.last_compression_ratio = compression_ratio
            if start_time is not None:
                message["data"]["start_time"] = start_time
                self.last_start_time = start_time
            if end_time is not None:
                message["data"]["end_time"] = end_time
                self.last_end_time = end_time
            if temperature is not None:
                message["data"]["temperature"] = temperature
                self.last_temperature = temperature
            
            # Send to orchestrator via WebSocket
            if self.orchestrator_ws:
                await self.orchestrator_ws.send_transcript(
                    text=text,
                    is_final=is_final,
                    language=self._resolved_language(),
                    language_code=self._resolved_language(),
                    words=words,
                    latency_ms=latency_ms
                )
            
            # Send to client callback
            try:
                await asyncio.wait_for(self.callback(message), timeout=self.config.callback_timeout_seconds)
            except asyncio.TimeoutError:
                logger.error(f"❌ [{self.session_id}] Timeout delivering transcript to callback")
            except Exception as e:
                logger.error(f"❌ [{self.session_id}] Callback error: {e}")
                
        except Exception as e:
            logger.error(f"❌ Error emitting transcript: {e}")
    
    async def _emit_speech_event(self, signal_type: str):
        """Emit speech event (SPEECH_START/SPEECH_END) to client and orchestrator"""
        try:
            message = {
                "type": "events",
                "data": {
                    "event_type": "vad_event",
                    "signal_type": signal_type,
                    "timestamp": time.time()
                }
            }
            
            # Send to orchestrator
            if self.orchestrator_ws:
                await self.orchestrator_ws.send_event(
                    event_type="vad_event",
                    signal_type=signal_type
                )
            
            # Send to client callback
            try:
                await asyncio.wait_for(self.callback(message), timeout=self.config.callback_timeout_seconds)
            except asyncio.TimeoutError:
                logger.error(f"❌ [{self.session_id}] Timeout delivering speech event")
            except Exception as e:
                logger.error(f"❌ [{self.session_id}] Speech event callback error: {e}")
                
        except Exception as e:
            logger.error(f"❌ Error emitting speech event: {e}")
    
    async def stop(self, flush_pending_audio: bool = True):
        """Stop session and cleanup"""
        self._running = False
        self._active_utterance_id += 1
        
        # Flush any pending audio only for intentional shutdowns.
        if flush_pending_audio and self.speech_active and (self.current_utterance or self.full_audio_buffer):
            await self._handle_speech_end()
        else:
            self.speech_active = False
            self.speech_candidate_start_time = None
            self.speech_segment_start_time = None
            self.current_utterance = ""
            self.partial_text = ""
            self.full_audio_buffer.clear()
            self.context_window.clear()
            self.pre_speech_chunks.clear()
            self.pre_speech_bytes = 0
            self._just_seeded_from_preroll = False
        
        # Close Groq client
        if self.groq_client:
            await self.groq_client.close()
            self.groq_client = None
        
        # Close orchestrator connection
        if self.orchestrator_ws:
            await self.orchestrator_ws.close()
            self.orchestrator_ws = None
        
        duration = time.time() - self.start_time
        logger.info(
            f"🛑 Session {self.session_id} stopped "
            f"(duration: {duration:.1f}s, chunks: {self.chunks_received}, "
            f"processed: {self.chunks_processed}, transcripts: {self.transcripts_emitted})"
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get session statistics"""
        stats = {
            "session_id": self.session_id,
            "duration": time.time() - self.start_time,
            "chunks_received": self.chunks_received,
            "chunks_processed": self.chunks_processed,
            "transcripts_emitted": self.transcripts_emitted,
            "speech_active": self.speech_active,
        }
        
        if self.groq_client:
            stats["groq_client"] = self.groq_client.get_stats()
        
        return stats

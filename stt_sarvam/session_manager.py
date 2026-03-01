"""
Session manager for Sarvam STT.

Each client WebSocket connection gets a SarvamSTTSession that:
  1. Opens a parallel WebSocket to Sarvam's streaming API
  2. Forwards PCM audio chunks as base64
  3. Receives transcripts + VAD events from Sarvam
  4. Uses Sarvam's server-side VAD for speech boundary detection
  5. Sends flush on END_SPEECH for instant final transcription
  6. Falls back to local RMS-based VAD as safety net
  7. Emits results to both client WebSocket and orchestrator
"""

import asyncio
import audioop
import logging
import time
from typing import Optional

from starlette.websockets import WebSocket, WebSocketState

from config import SarvamSTTConfig
from sarvam_client import SarvamWebSocketClient
from orchestrator_client import OrchestratorWSClient

logger = logging.getLogger(__name__)


class SarvamSTTSession:
    """Manages a single STT session lifecycle."""

    def __init__(
        self,
        session_id: str,
        config: SarvamSTTConfig,
        client_ws: WebSocket,
    ):
        self.session_id = session_id
        self.config = config
        self.client_ws = client_ws

        # Sarvam upstream client
        self.sarvam: Optional[SarvamWebSocketClient] = None

        # Orchestrator downstream client
        self.orchestrator: Optional[OrchestratorWSClient] = None

        # ── Speech state ────────────────────────────────────────
        self.speech_active = False
        self.speech_start_time: Optional[float] = None
        self.current_transcript = ""
        self.accumulated_segments: list[str] = []
        self.flush_pending = False
        self.last_transcript_time: Optional[float] = None
        self.last_language_code: Optional[str] = None

        # ── Local VAD state ─────────────────────────────────────
        self.consecutive_silent_chunks = 0
        self.local_speech_detected = False
        self.last_audio_time: float = 0

        # ── Audio buffer ────────────────────────────────────────
        # Buffer small PCM packets from client until we have enough
        # to forward (forward_interval_ms worth of audio).
        self._audio_buffer = bytearray()

        # ── Lifecycle ───────────────────────────────────────────
        self._is_running = False
        self._tasks: list[asyncio.Task] = []
        self._reconnect_lock = asyncio.Lock()

        # ── Metrics ─────────────────────────────────────────────
        self.chunks_received = 0
        self.chunks_forwarded = 0
        self.transcripts_emitted = 0
        self.finals_emitted = 0
        self.start_time = time.time()

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> bool:
        """Initialize Sarvam connection and optional orchestrator link."""
        self._is_running = True

        # Connect to Sarvam
        self.sarvam = SarvamWebSocketClient(
            config=self.config,
            on_transcript=self._on_sarvam_transcript,
            on_event=self._on_sarvam_event,
            on_error=self._on_sarvam_error,
            on_disconnect=self._on_sarvam_disconnect,
        )
        connected = await self.sarvam.connect()
        if not connected:
            logger.error(f"[{self.session_id}] Failed to connect to Sarvam")
            return False

        # Connect to orchestrator (optional, non-blocking)
        if self.config.orchestrator_ws_url:
            self.orchestrator = OrchestratorWSClient(
                orchestrator_ws_url=self.config.orchestrator_ws_url,
                session_id=self.session_id,
                skip_ssl=self.config.skip_ssl_verify,
            )
            self._tasks.append(asyncio.create_task(self.orchestrator.connect()))

        # Start local VAD watchdog
        self._tasks.append(asyncio.create_task(self._local_vad_watchdog()))

        logger.info(f"[{self.session_id}] Session started")
        return True

    async def stop(self):
        """Tear down session cleanly."""
        self._is_running = False

        # Cancel background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Disconnect Sarvam
        if self.sarvam:
            await self.sarvam.disconnect()

        # Disconnect orchestrator
        if self.orchestrator:
            await self.orchestrator.disconnect()

        duration = time.time() - self.start_time
        logger.info(
            f"[{self.session_id}] Session stopped — "
            f"{self.chunks_received} chunks, {self.transcripts_emitted} transcripts, "
            f"{self.finals_emitted} finals in {duration:.1f}s"
        )

    # ── Audio ingestion ─────────────────────────────────────────

    async def process_audio(self, pcm_data: bytes):
        """Process incoming PCM audio from client WebSocket."""
        if not self._is_running:
            return

        self.chunks_received += 1
        self.last_audio_time = time.time()

        # ── Local VAD check ─────────────────────────────────────
        try:
            energy = audioop.rms(pcm_data, self.config.bytes_per_sample)
        except audioop.error:
            energy = 0

        if energy > self.config.local_vad_energy_threshold:
            self.consecutive_silent_chunks = 0
            if not self.local_speech_detected:
                self.local_speech_detected = True
                logger.debug(f"[{self.session_id}] Local VAD: speech start (energy={energy})")
        else:
            self.consecutive_silent_chunks += 1

        # ── Buffer and forward ──────────────────────────────────
        self._audio_buffer.extend(pcm_data)
        threshold = self.config.forward_chunk_bytes

        while len(self._audio_buffer) >= threshold:
            chunk = bytes(self._audio_buffer[:threshold])
            del self._audio_buffer[:threshold]
            await self._forward_audio(chunk)

    async def _forward_audio(self, pcm_chunk: bytes):
        """Forward a buffered PCM chunk to Sarvam."""
        if not self.sarvam:
            return
        if self.sarvam.is_connected:
            sent = await self.sarvam.send_audio(pcm_chunk)
            if sent:
                self.chunks_forwarded += 1
                return
        # Connection lost — try reconnect (with lock to prevent races)
        if self._reconnect_lock.locked():
            return  # Another coroutine is already reconnecting
        async with self._reconnect_lock:
            if self.sarvam.is_connected:
                # Reconnected by the time we got the lock
                await self.sarvam.send_audio(pcm_chunk)
                return
            logger.warning(f"[{self.session_id}] Sarvam send failed, reconnecting...")
            reconnected = await self.sarvam.reconnect()
            if reconnected:
                await self.sarvam.send_audio(pcm_chunk)

    async def flush_remaining_audio(self):
        """Flush any remaining buffered audio to Sarvam."""
        if self._audio_buffer and self.sarvam and self.sarvam.is_connected:
            remaining = bytes(self._audio_buffer)
            self._audio_buffer.clear()
            if len(remaining) >= self.config.min_audio_bytes:
                await self.sarvam.send_audio(remaining)

    # ── Sarvam callbacks ────────────────────────────────────────

    async def _on_sarvam_transcript(self, data: dict):
        """Handle transcript from Sarvam."""
        transcript = (data.get("transcript") or "").strip()
        if not transcript:
            return

        request_id = data.get("request_id", "")
        language_code = data.get("language_code")
        metrics = data.get("metrics", {})
        processing_latency = metrics.get("processing_latency", 0)

        if language_code:
            self.last_language_code = language_code

        self.last_transcript_time = time.time()

        # Determine if this is a final transcript
        is_final = self.flush_pending

        if is_final:
            # This transcript arrived after our flush — it's the final
            self.flush_pending = False
            self.accumulated_segments.append(transcript)
            final_text = " ".join(self.accumulated_segments)
            self.accumulated_segments.clear()
            self.current_transcript = ""

            await self._emit_transcript(
                text=final_text,
                is_final=True,
                request_id=request_id,
                language_code=language_code,
                latency_ms=processing_latency * 1000 if processing_latency else None,
            )
            self.finals_emitted += 1
        else:
            # Partial transcript — accumulate and emit
            self.current_transcript = transcript
            await self._emit_transcript(
                text=transcript,
                is_final=False,
                request_id=request_id,
                language_code=language_code or self.last_language_code,
                latency_ms=processing_latency * 1000 if processing_latency else None,
            )

    async def _on_sarvam_event(self, data: dict):
        """Handle VAD event from Sarvam server."""
        signal_type = data.get("signal_type", "")
        event_type = data.get("event_type", "")

        if signal_type == "START_SPEECH":
            self.speech_active = True
            self.speech_start_time = time.time()
            self.current_transcript = ""
            self.accumulated_segments.clear()
            self.last_language_code = None
            self.consecutive_silent_chunks = 0
            logger.info(f"[{self.session_id}] Sarvam VAD: SPEECH_START")
            await self._emit_vad_event("SPEECH_START")

        elif signal_type == "END_SPEECH":
            self.speech_active = False
            logger.info(f"[{self.session_id}] Sarvam VAD: SPEECH_END")
            await self._emit_vad_event("SPEECH_END")

            # Flush remaining audio buffer then signal Sarvam to finalize
            await self.flush_remaining_audio()
            self.flush_pending = True
            if self.sarvam:
                await self.sarvam.send_flush()

            # Safety: if no transcript arrives after flush within timeout,
            # emit what we have as final
            asyncio.create_task(self._flush_timeout_guard())

        else:
            logger.debug(f"[{self.session_id}] Sarvam event: {event_type}/{signal_type}")

    async def _on_sarvam_error(self, data: dict):
        """Handle error from Sarvam."""
        error_msg = data.get("message") or data.get("error", "unknown")
        error_code = data.get("code", "")
        logger.error(f"[{self.session_id}] Sarvam error [{error_code}]: {error_msg}")
        await self._emit_to_client({
            "type": "error",
            "message": f"Sarvam STT error: {error_msg}",
            "code": error_code,
        })

    async def _on_sarvam_disconnect(self):
        """Handle Sarvam connection loss.

        Does NOT auto-reconnect here to avoid racing with _forward_audio's
        reconnect path. The next audio send will trigger reconnection.
        """
        if not self._is_running:
            return
        logger.warning(f"[{self.session_id}] Sarvam disconnected — will reconnect on next audio")

    # ── Local VAD watchdog ──────────────────────────────────────

    async def _local_vad_watchdog(self):
        """
        Safety net: if Sarvam's VAD doesn't fire END_SPEECH but we detect
        sustained silence locally, force a flush to get the final transcript.
        """
        try:
            while self._is_running:
                await asyncio.sleep(0.2)  # Check every 200ms

                if not self.local_speech_detected:
                    continue

                # Check if silence threshold exceeded
                if self.consecutive_silent_chunks >= self.config.silence_chunks_threshold:
                    if self.speech_active and not self.flush_pending:
                        elapsed_silence = (
                            self.consecutive_silent_chunks * self.config.forward_interval_ms
                        )
                        logger.info(
                            f"[{self.session_id}] Local VAD: forcing flush after "
                            f"{elapsed_silence}ms silence"
                        )
                        self.speech_active = False
                        await self._emit_vad_event("SPEECH_END")
                        await self.flush_remaining_audio()
                        self.flush_pending = True
                        if self.sarvam:
                            await self.sarvam.send_flush()
                        asyncio.create_task(self._flush_timeout_guard())

                    # Reset local speech detection
                    self.local_speech_detected = False
                    self.consecutive_silent_chunks = 0

        except asyncio.CancelledError:
            pass

    async def _flush_timeout_guard(self):
        """
        If no final transcript arrives within configured timeout after flush,
        emit whatever we have as the final.
        """
        await asyncio.sleep(self.config.flush_timeout_ms / 1000.0)
        if self.flush_pending:
            logger.warning(f"[{self.session_id}] Flush timeout — emitting accumulated as final")
            self.flush_pending = False
            text = self.current_transcript or " ".join(self.accumulated_segments)
            fallback_language = self.last_language_code
            if not fallback_language and self.config.language_code.lower() != "unknown":
                fallback_language = self.config.language_code
            if text.strip():
                await self._emit_transcript(
                    text=text.strip(),
                    is_final=True,
                    request_id="flush_timeout",
                    language_code=fallback_language,
                    latency_ms=None,
                )
                self.finals_emitted += 1
            self.current_transcript = ""
            self.accumulated_segments.clear()

    # ── Emission helpers ────────────────────────────────────────

    async def _emit_transcript(
        self,
        text: str,
        is_final: bool,
        request_id: str = "",
        language_code: Optional[str] = None,
        latency_ms: Optional[float] = None,
    ):
        """Send transcript to client and orchestrator."""
        if not text:
            return

        self.transcripts_emitted += 1
        now = time.time()

        payload = {
            "type": "data",
            "data": {
                "transcript": text,
                "is_final": is_final,
                "request_id": request_id,
                "timestamp": now,
                "source": "sarvam_stt",
                "language_code": language_code,
            },
        }
        if latency_ms is not None:
            payload["data"]["latency_ms"] = round(latency_ms, 2)

        level = "INFO" if is_final else "DEBUG"
        logger.log(
            logging.getLevelName(level),
            f"[{self.session_id}] {'FINAL' if is_final else 'partial'}: \"{text}\"",
        )

        # Send to client
        await self._emit_to_client(payload)

        # Send to orchestrator
        if self.orchestrator:
            await self.orchestrator.send_transcript(
                text=text,
                is_final=is_final,
                language_code=language_code or "",
                latency_ms=latency_ms,
            )

    async def _emit_vad_event(self, signal_type: str):
        """Send VAD event to client and orchestrator."""
        now = time.time()

        payload = {
            "type": "events",
            "data": {
                "event_type": "vad_event",
                "signal_type": signal_type,
                "timestamp": now,
            },
        }
        await self._emit_to_client(payload)

        if self.orchestrator:
            await self.orchestrator.send_vad_event(signal_type)

    async def _emit_to_client(self, payload: dict):
        """Send JSON payload to client WebSocket."""
        try:
            if self.client_ws.client_state == WebSocketState.CONNECTED:
                await self.client_ws.send_json(payload)
        except Exception as e:
            logger.warning(f"[{self.session_id}] Client send error: {e}")

    # ── Metrics ─────────────────────────────────────────────────

    def get_metrics(self) -> dict:
        duration = time.time() - self.start_time
        return {
            "session_id": self.session_id,
            "duration_seconds": round(duration, 1),
            "chunks_received": self.chunks_received,
            "chunks_forwarded": self.chunks_forwarded,
            "transcripts_emitted": self.transcripts_emitted,
            "finals_emitted": self.finals_emitted,
            "speech_active": self.speech_active,
            "flush_pending": self.flush_pending,
            "sarvam": self.sarvam.get_metrics() if self.sarvam else None,
        }

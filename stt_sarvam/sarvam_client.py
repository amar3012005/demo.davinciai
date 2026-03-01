"""
Sarvam AI WebSocket client for real-time Speech-to-Text-Translate.

This client manages a persistent WebSocket connection to Sarvam's streaming
API, handling:
  - Base64-encoded PCM audio forwarding
  - Transcript and VAD event reception
  - Flush signals for instant final transcription
  - Automatic reconnection with exponential backoff
"""

import asyncio
import base64
import io
import wave
import json
import logging
import time
from typing import Optional, Callable, Awaitable

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatusCode,
)

from config import SarvamSTTConfig

logger = logging.getLogger(__name__)

# Callback signatures
TranscriptCallback = Callable[[dict], Awaitable[None]]   # {"transcript":..., "request_id":..., ...}
EventCallback = Callable[[dict], Awaitable[None]]         # {"signal_type":..., "event_type":..., ...}
ErrorCallback = Callable[[dict], Awaitable[None]]         # {"error":..., "code":...}


class SarvamWebSocketClient:
    """Manages a single WebSocket connection to Sarvam STT API."""

    def __init__(
        self,
        config: SarvamSTTConfig,
        on_transcript: Optional[TranscriptCallback] = None,
        on_event: Optional[EventCallback] = None,
        on_error: Optional[ErrorCallback] = None,
        on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.config = config
        self._on_transcript = on_transcript
        self._on_event = on_event
        self._on_error = on_error
        self._on_disconnect = on_disconnect

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._is_connected = False
        self._is_running = False
        self._connect_lock = asyncio.Lock()

        # Metrics
        self.bytes_sent = 0
        self.messages_received = 0
        self.transcripts_received = 0
        self.events_received = 0
        self.errors_received = 0
        self.connect_time: Optional[float] = None
        self._reconnect_count = 0

    # ── Connection lifecycle ────────────────────────────────────

    async def connect(self) -> bool:
        """Establish WebSocket connection to Sarvam API."""
        async with self._connect_lock:
            if self._is_connected:
                return True

            url = self.config.build_ws_url()
            headers = {"Api-Subscription-Key": self.config.api_key}

            try:
                logger.info(f"  Connecting to Sarvam: {url}...")
                self._ws = await asyncio.wait_for(
                    websockets.connect(
                        url,
                        additional_headers=headers,
                        ping_interval=self.config.ping_interval,
                        ping_timeout=self.config.ping_timeout,
                        max_size=2**20,  # 1 MB max message
                        close_timeout=5,
                    ),
                    timeout=self.config.connection_timeout,
                )
                self._is_connected = True
                self._is_running = True
                self.connect_time = time.time()
                self._reconnect_count = 0
                logger.info("  Connected to Sarvam STT")

                # Send initial config/prompt if set
                if self.config.context_prompt:
                    await self._send_config(self.config.context_prompt)

                # Start listener
                self._listener_task = asyncio.create_task(self._listen_loop())
                return True

            except asyncio.TimeoutError:
                logger.error(f"  Sarvam connection timed out ({self.config.connection_timeout}s)")
                return False
            except InvalidStatusCode as e:
                logger.error(f"  Sarvam rejected connection: HTTP {e.status_code}")
                return False
            except Exception as e:
                logger.error(f"  Sarvam connection failed: {e}")
                return False

    async def disconnect(self):
        """Gracefully close the connection."""
        self._is_running = False
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._is_connected = False
        logger.info("  Sarvam connection closed")

    async def reconnect(self) -> bool:
        """Reconnect with exponential backoff."""
        await self.disconnect()
        delay = self.config.reconnect_delay_initial
        for attempt in range(1, self.config.reconnect_max_attempts + 1):
            logger.info(f"  Sarvam reconnect attempt {attempt}/{self.config.reconnect_max_attempts} in {delay:.1f}s")
            await asyncio.sleep(delay)
            if await self.connect():
                self._reconnect_count += 1
                return True
            delay = min(delay * 2, self.config.reconnect_delay_max)
        logger.error("  Sarvam reconnect failed — max attempts reached")
        return False

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._ws is not None


    def _convert_pcm_to_wav(self, pcm_bytes: bytes) -> bytes:
        """Convert raw PCM bytes to WAV format."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)  # Mono
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(self.config.sample_rate)
            wav_file.writeframes(pcm_bytes)
        buffer.seek(0)
        return buffer.read()

    async def send_audio(self, pcm_bytes: bytes) -> bool:
        """Send PCM audio chunk to Sarvam as base64-encoded WAV."""
        if not self.is_connected:
            return False
        try:
            # Convert PCM to WAV
            wav_bytes = self._convert_pcm_to_wav(pcm_bytes)
            b64_data = base64.b64encode(wav_bytes).decode("ascii")
            
            message = json.dumps({
                "audio": {
                    "data": b64_data,
                    "encoding": "audio/wav",  # Required by API schema
                    "sample_rate": str(self.config.sample_rate)
                }
            })
            await self._ws.send(message)
            self.bytes_sent += len(pcm_bytes)
            return True
        except ConnectionClosed:
            logger.warning("  Send failed — connection closed")
            self._is_connected = False
            return False
        except Exception as e:
            logger.error(f"  Send audio error: {e}")
            return False

    async def send_flush(self) -> bool:
        """Send flush signal to finalize current transcription."""
        if not self.is_connected:
            return False
        try:
            await self._ws.send(json.dumps({"type": "flush"}))
            logger.debug("  Flush signal sent")
            return True
        except ConnectionClosed:
            logger.warning("  Flush failed — connection closed")
            self._is_connected = False
            return False
        except Exception as e:
            logger.error(f"  Flush error: {e}")
            return False

    async def _send_config(self, prompt: str) -> bool:
        """Send config message with prompt for ASR context."""
        if not self.is_connected:
            return False
        try:
            await self._ws.send(json.dumps({
                "type": "config",
                "prompt": prompt,
            }))
            logger.debug(f"  Config sent: prompt='{prompt[:60]}...'")
            return True
        except Exception as e:
            logger.error(f"  Config send error: {e}")
            return False

    # ── Receiving ───────────────────────────────────────────────

    async def _listen_loop(self):
        """Main loop receiving messages from Sarvam."""
        try:
            async for raw_message in self._ws:
                self.messages_received += 1
                try:
                    msg = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning(f"  Non-JSON message from Sarvam: {raw_message[:100]}")
                    continue

                msg_type = msg.get("type", "")
                data = msg.get("data", {})

                if msg_type == "data":
                    self.transcripts_received += 1
                    if self._on_transcript:
                        await self._on_transcript(data)

                elif msg_type == "events":
                    self.events_received += 1
                    if self._on_event:
                        await self._on_event(data)

                elif msg_type == "error":
                    self.errors_received += 1
                    logger.error(f"  Sarvam error: {data}")
                    if self._on_error:
                        await self._on_error(data)

                else:
                    logger.debug(f"  Unknown Sarvam message type: {msg_type}")

        except ConnectionClosedOK:
            logger.info("  Sarvam connection closed normally")
        except ConnectionClosedError as e:
            logger.warning(f"  Sarvam connection lost: {e}")
        except asyncio.CancelledError:
            logger.debug("  Sarvam listener cancelled")
            raise
        except Exception as e:
            logger.error(f"  Sarvam listener error: {e}")
        finally:
            self._is_connected = False
            if self._on_disconnect:
                try:
                    await self._on_disconnect()
                except Exception:
                    pass

    # ── Metrics ─────────────────────────────────────────────────

    def get_metrics(self) -> dict:
        uptime = time.time() - self.connect_time if self.connect_time else 0
        return {
            "connected": self.is_connected,
            "uptime_seconds": round(uptime, 1),
            "bytes_sent": self.bytes_sent,
            "messages_received": self.messages_received,
            "transcripts_received": self.transcripts_received,
            "events_received": self.events_received,
            "errors_received": self.errors_received,
            "reconnect_count": self._reconnect_count,
        }

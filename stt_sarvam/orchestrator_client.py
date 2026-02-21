"""
Persistent WebSocket client to the TARA Orchestrator.

Sends STT transcripts and VAD events, matching the contract
established by stt_groq_whisper so this service is a drop-in replacement.
"""

import asyncio
import json
import logging
import ssl
import time
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class OrchestratorWSClient:
    """Maintains a persistent connection to the orchestrator."""

    def __init__(
        self,
        orchestrator_ws_url: str,
        session_id: str,
        skip_ssl: bool = True,
    ):
        self._base_url = orchestrator_ws_url.rstrip("/")
        self.session_id = session_id
        self._skip_ssl = skip_ssl

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._is_connected = False
        self._is_running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Reconnect settings
        self._reconnect_delay = 0.5
        self._reconnect_max_delay = 10.0

    # ── Connection ──────────────────────────────────────────────

    async def connect(self):
        """Connect to orchestrator with auto-reconnection."""
        self._is_running = True
        await self._establish()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def disconnect(self):
        """Gracefully disconnect."""
        self._is_running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None
                self._is_connected = False

    async def _establish(self) -> bool:
        """Single connection attempt."""
        async with self._lock:
            if self._is_connected:
                return True

            url = self._base_url
            if "/ws" not in url:
                url += "/ws"
            url += f"?session_id={self.session_id}&source=stt_sarvam"

            ssl_ctx = None
            if self._skip_ssl and url.startswith("wss"):
                ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(
                        url,
                        ssl=ssl_ctx,
                        ping_interval=20,
                        ping_timeout=10,
                        close_timeout=5,
                    ),
                    timeout=10,
                )
                self._is_connected = True
                self._reconnect_delay = 0.5
                logger.info(f"  Connected to orchestrator: {url.split('?')[0]}")
                return True
            except Exception as e:
                logger.warning(f"  Orchestrator connect failed: {e}")
                return False

    async def _monitor_loop(self):
        """Background loop: reconnect if connection drops."""
        try:
            while self._is_running:
                await asyncio.sleep(5)
                if not self._is_connected and self._is_running:
                    logger.info("  Orchestrator reconnecting...")
                    await asyncio.sleep(self._reconnect_delay)
                    if await self._establish():
                        continue
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2,
                        self._reconnect_max_delay,
                    )
        except asyncio.CancelledError:
            pass

    # ── Sending ─────────────────────────────────────────────────

    async def _send(self, payload: dict) -> bool:
        if not self._is_connected or not self._ws:
            return False
        try:
            await self._ws.send(json.dumps(payload))
            return True
        except ConnectionClosed:
            self._is_connected = False
            return False
        except Exception as e:
            logger.warning(f"  Orchestrator send error: {e}")
            return False

    async def send_transcript(
        self,
        text: str,
        is_final: bool,
        language_code: str = "",
        latency_ms: Optional[float] = None,
    ):
        """Send transcript matching stt_groq_whisper's contract."""
        payload = {
            "type": "stt_transcript",
            "text": text,
            "is_final": is_final,
            "language": "en",
            "timestamp": time.time(),
            "session_id": self.session_id,
            "source": "sarvam_stt",
            "language_code": language_code,
        }
        if latency_ms is not None:
            payload["latency_ms"] = round(latency_ms, 2)
        await self._send(payload)

    async def send_vad_event(self, signal_type: str):
        """Send VAD event matching stt_groq_whisper's contract."""
        await self._send({
            "type": "stt_event",
            "event_type": "vad_event",
            "signal_type": signal_type,
            "timestamp": time.time(),
            "session_id": self.session_id,
            "source": "sarvam_stt",
        })

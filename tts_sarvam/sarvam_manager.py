"""
Sarvam WebSocket Manager

Robust, persistent WebSocket connection manager for Sarvam AI TTS API.
Features:
- Connection pooling with pre-warming
- Automatic reconnection with exponential backoff
- context_id management for prosody continuity
- Never disconnects mid-session
"""

import asyncio
import json
import base64
import logging
import time
import uuid
import numpy as np
from typing import Optional, Callable, Dict, Any, AsyncGenerator, Union, List
from dataclasses import dataclass, field
from enum import Enum

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from config import SarvamConfig

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """WebSocket connection states"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    STREAMING = "streaming"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class ConnectionMetrics:
    """Track connection health and performance"""
    connect_time: Optional[float] = None
    last_activity: float = field(default_factory=time.time)
    messages_sent: int = 0
    messages_received: int = 0
    reconnect_count: int = 0
    total_audio_bytes: int = 0
    first_chunk_latency_ms: Optional[float] = None


class SarvamConnection:
    """
    Single managed persistent WebSocket connection to Sarvam with dispatcher.
    
    Handles:
    - Persistent background connection monitor
    - Automatic reconnection with exponential backoff
    - context_id multiplexing/dispatching
    - Health monitoring
    """
    
    def __init__(self, config: SarvamConfig, connection_id: str):
        self.config = config
        self.connection_id = connection_id
        self.ws: Optional[websockets.ClientConnection] = None
        self.state = ConnectionState.DISCONNECTED
        self.metrics = ConnectionMetrics()
        self._lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task] = None
        self._connected_event = asyncio.Event()
        
        # We only really support one active dispatch per connection for Sarvam because 
        # Sarvam doesn't natively do context_id multiplexing in the same websocket.
        # But for connection pooling to work with orchestrator, we track the current context_id.
        self.current_context_id: Optional[str] = None
        self._dispatch_queue: Optional[asyncio.Queue] = None
        self._dispatch_lock = asyncio.Lock()
        
        self.is_busy = False # Used by Manager for load balancing
    
    async def connect(self) -> bool:
        """Initialize monitor task and wait for connection"""
        async with self._lock:
            if self._monitor_task is None or self._monitor_task.done():
                logger.info(f"[{self.connection_id}] Starting persistent connection monitor...")
                self._monitor_task = asyncio.create_task(self._monitor_loop())
        
        try:
            # Wait for initial connection
            await asyncio.wait_for(self._connected_event.wait(), timeout=10.0)
            return True
        except asyncio.TimeoutError:
            logger.warning(f"[{self.connection_id}] Initial connection timed out, monitor will continue retrying")
            return False
    
    async def _monitor_loop(self):
        """Background task to keep connection alive and dispatch messages"""
        backoff = self.config.reconnect_base_delay_ms / 1000.0
        max_backoff = 30.0
        
        while True:
            try:
                ws_url = f"{self.config.get_websocket_url()}?model={self.config.model}&send_completion_event=true"
                self.state = ConnectionState.CONNECTING
                
                # Add headers for API Key
                additional_headers = {}
                if self.config.api_key:
                    additional_headers["Api-Subscription-Key"] = self.config.api_key
                
                async with websockets.connect(
                    ws_url,
                    ping_interval=self.config.ping_interval_seconds,
                    ping_timeout=self.config.ping_timeout_seconds,
                    max_size=10_000_000,
                    close_timeout=5,
                    additional_headers=additional_headers
                ) as ws:
                    self.ws = ws
                    self.state = ConnectionState.CONNECTED
                    self._connected_event.set()
                    self.metrics.connect_time = time.time()
                    self.metrics.last_activity = time.time()
                    backoff = self.config.reconnect_base_delay_ms / 1000.0
                    
                    logger.info(f"✅ [{self.connection_id}] Persistent WebSocket established")
                    
                    # Message loop
                    async for message in ws:
                        self.metrics.messages_received += 1
                        self.metrics.last_activity = time.time()
                        
                        try:
                            data = json.loads(message)
                            async with self._dispatch_lock:
                                queue = self._dispatch_queue
                                if queue:
                                    await queue.put(data)
                        except Exception as e:
                            logger.error(f"[{self.connection_id}] Dispatch error: {e}")
                
                logger.warning(f"[{self.connection_id}] Connection closed by server")
                
            except Exception as e:
                logger.warning(f"[{self.connection_id}] Connection error: {e}")
            
            # Reset state and retry
            self.metrics.reconnect_count += 1
            self.state = ConnectionState.DISCONNECTED
            self._connected_event.clear()
            self.ws = None
            
            logger.info(f"[{self.connection_id}] Reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def disconnect(self):
        """Shutdown monitor and close connection"""
        async with self._lock:
            if self._monitor_task:
                self._monitor_task.cancel()
                self._monitor_task = None
            
            if self.ws:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None
            
            self.state = ConnectionState.DISCONNECTED
            self._connected_event.clear()
            logger.info(f"[{self.connection_id}] Persistent connection stopped")
    
    async def ensure_connected(self) -> bool:
        """Wait for connection to be ready"""
        if self.state == ConnectionState.CONNECTED:
            return True
        
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            return False
    
    async def send(self, data: Dict[str, Any]):
        """Send message over the connection"""
        if self.ws and self.state == ConnectionState.CONNECTED:
            await self.ws.send(json.dumps(data))
            self.metrics.messages_sent += 1
        else:
            if await self.ensure_connected():
                await self.ws.send(json.dumps(data))
                self.metrics.messages_sent += 1
            else:
                raise ConnectionError(f"[{self.connection_id}] Cannot send: Connection unavailable")

    def is_available(self) -> bool:
        """Check if connection is alive and ready for work"""
        return (
            self.state == ConnectionState.CONNECTED and
            self._connected_event.is_set() and
            not self.is_busy
        )


class SarvamManager:
    """
    High-level manager for Sarvam TTS streaming.
    
    Features:
    - Connection pool for concurrent sessions
    - Robust error handling and reconnection
    """
    
    def __init__(self, config: SarvamConfig):
        self.config = config
        self._connections: List[SarvamConnection] = []
        self._connection_lock = asyncio.Lock()
        self._is_warmed = False
    
    async def warmup(self):
        """Pre-warm connection pool"""
        if self._is_warmed:
            return
        
        logger.info(f"🔥 Warming up {self.config.pool_size} Sarvam connections...")
        
        async with self._connection_lock:
            for i in range(self.config.pool_size):
                conn = SarvamConnection(self.config, f"pool-{i}")
                if await conn.connect():
                    self._connections.append(conn)
                else:
                    logger.warning(f"Failed to warm connection pool-{i}")
        
        self._is_warmed = True
        logger.info(f"✅ Warmed {len(self._connections)} connections")
    
    async def get_connection(self) -> Optional[SarvamConnection]:
        """Get an available connection from the pool"""
        async with self._connection_lock:
            for conn in self._connections:
                if conn.is_available():
                    return conn
            
            if len(self._connections) < self.config.pool_size * 2:
                conn = SarvamConnection(self.config, f"dynamic-{len(self._connections)}")
                if await conn.connect():
                    self._connections.append(conn)
                    return conn
            
            await asyncio.sleep(0.1)
            for conn in self._connections:
                if conn.is_available():
                    return conn
            
            return None

    def _convert_s16_to_f32le(self, audio_bytes: bytes) -> bytes:
        """
        Convert 16-bit PCM (Sarvam linear16) to 32-bit floating point PCM (pcm_f32le)
        for orchestrator compatibility without resampling.
        """
        try:
            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
            float_array = audio_array.astype(np.float32) / 32768.0
            return float_array.tobytes()
        except Exception as e:
            logger.error(f"Failed to convert audio from int16 to float32: {e}")
            return audio_bytes
    
    async def stream_text_to_audio(
        self,
        text_iterator: Union[AsyncGenerator[str, None], List[str], str],
        audio_callback: Callable[[bytes, int, Dict[str, Any]], Any],
        context_id: Optional[str] = None,
        voice_id: Optional[str] = None,
        language: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Stream text to audio using persistent connections.
        """
        if isinstance(text_iterator, str):
            async def _str_gen():
                yield text_iterator
            text_iter = _str_gen()
        elif isinstance(text_iterator, list):
            async def _list_gen():
                for item in text_iterator:
                    yield item
            text_iter = _list_gen()
        else:
            text_iter = text_iterator
        
        conn = await self.get_connection()
        if not conn:
            logger.error("No connection available")
            return {"error": "No connection available"}
        
        if not await conn.ensure_connected():
            return {"error": "Failed to connect"}
        
        conn.is_busy = True
        ctx_id = context_id or str(uuid.uuid4())
        
        queue = asyncio.Queue()
        async with conn._dispatch_lock:
            conn._dispatch_queue = queue
            conn.current_context_id = ctx_id
            
        try:
            stats = {
                "chunks_received": 0,
                "total_audio_bytes": 0,
                "first_chunk_latency_ms": None,
                "total_time_ms": 0,
                "context_id": ctx_id,
            }
            start_time = time.time()
            first_chunk_time: Optional[float] = None
            stream_start_time: Optional[float] = None
            
            send_complete = asyncio.Event()
            receive_complete = asyncio.Event()
            receive_error: Optional[str] = None
            
            async def send_text():
                nonlocal stream_start_time
                try:
                    # 1. Send Configuration first
                    target_model = model_id or self.config.model
                    target_speaker = voice_id or self.config.voice_id
                    target_language = language or self.config.language
                    
                    config_payload = {
                        "type": "config",
                        "data": {
                            "model": target_model,
                            "target_language_code": target_language,
                            "speaker": target_speaker,
                            "speech_sample_rate": str(self.config.sample_rate),
                            "output_audio_codec": self.config.output_audio_codec
                        }
                    }
                    await conn.send(config_payload)
                    
                    # 2. Iterate and send text chunks
                    async for text_chunk in text_iter:
                        if not text_chunk or not text_chunk.strip():
                            continue
                            
                        # Format for Sarvam
                        message = {
                            "type": "text",
                            "data": {
                                "text": text_chunk + " "
                            }
                        }
                        
                        if stream_start_time is None:
                            logger.info(f"[{ctx_id[:8]}] Sending first chunk: {text_chunk[:30]}...")
                            stream_start_time = time.time()
                        
                        await conn.send(message)
                    
                    # 3. Send Flush signal (Signals end of streaming)
                    flush_message = {
                        "type": "flush"
                    }
                    await conn.send(flush_message)
                    logger.info(f"[{ctx_id[:8]}] Send complete, flushed.")
                    
                except Exception as e:
                    logger.error(f"[{ctx_id[:8]}] Send error: {e}")
                finally:
                    send_complete.set()
            
            async def receive_audio():
                nonlocal first_chunk_time, receive_error
                try:
                    while True:
                        try:
                            # Wait for message from dispatcher
                            data = await asyncio.wait_for(queue.get(), timeout=20.0)
                            
                            msg_type = data.get("type")
                            
                            if msg_type == "error":
                                receive_error = data.get("data", {}).get("message") or str(data)
                                logger.error(f"Sarvam API Error: {receive_error}")
                                break
                            
                            elif msg_type == "audio":
                                audio_b64 = data.get("data", {}).get("audio")
                                if audio_b64:
                                    audio_bytes = base64.b64decode(audio_b64)
                                    
                                    # Convert linear16 from server to pcm_f32le if requested
                                    if self.config.output_audio_codec == "linear16":
                                        audio_bytes = self._convert_s16_to_f32le(audio_bytes)
                                    
                                    stats["chunks_received"] += 1
                                    stats["total_audio_bytes"] += len(audio_bytes)
                                    
                                    if first_chunk_time is None and stream_start_time:
                                        first_chunk_time = time.time()
                                        latency_ms = (first_chunk_time - stream_start_time) * 1000
                                        stats["first_chunk_latency_ms"] = latency_ms
                                        logger.info(f"📊 [{ctx_id[:8]}] TTFT: {latency_ms:.0f}ms")
                                    
                                    metadata = {
                                        "chunk_index": stats["chunks_received"],
                                        "context_id": ctx_id,
                                        "sample_rate": self.config.sample_rate,
                                        "format": "pcm_f32le" if self.config.output_audio_codec == "linear16" else self.config.output_audio_codec,
                                    }
                                    
                                    try:
                                        result = audio_callback(audio_bytes, self.config.sample_rate, metadata)
                                        if asyncio.iscoroutine(result):
                                            await result
                                    except Exception as e:
                                        logger.error(f"Callback error: {e}")
                                        
                            elif msg_type == "event":
                                event_type = data.get("data", {}).get("event_type")
                                if event_type == "final":
                                    logger.info(f"✅ [{ctx_id[:8]}] Stream complete (received final event)")
                                    break
                                
                        except asyncio.TimeoutError:
                            if send_complete.is_set():
                                logger.warning(f"[{ctx_id[:8]}] Receive timeout after send complete")
                                break
                            continue
                            
                except Exception as e:
                    logger.error(f"Receive error: {e}")
                    receive_error = str(e)
                finally:
                    receive_complete.set()
            
            # Run concurrently
            await asyncio.gather(send_text(), receive_audio())
            
            stats["total_time_ms"] = (time.time() - start_time) * 1000
            
            if receive_error:
                stats["error"] = receive_error
            return stats
            
        finally:
            # Unregister queue
            async with conn._dispatch_lock:
                conn._dispatch_queue = None
                conn.current_context_id = None
            
            # Close the websocket for Sarvam to ensure clean state next time
            await conn.disconnect()
            await conn.connect() # Reconnect it into the pool in background
            conn.is_busy = False
    
    async def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        language: Optional[str] = None,
    ) -> bytes:
        """
        Synthesize text to complete audio bytes.
        """
        audio_chunks: List[bytes] = []
        
        def collect_audio(audio_bytes: bytes, sample_rate: int, metadata: Dict[str, Any]):
            audio_chunks.append(audio_bytes)
        
        stats = await self.stream_text_to_audio(
            text,
            collect_audio,
            voice_id=voice_id,
            language=language,
        )
        
        if stats.get("error"):
            logger.error(f"Synthesis failed: {stats['error']}")
            return b""
        
        return b"".join(audio_chunks)
    
    async def close(self):
        """Close all connections"""
        async with self._connection_lock:
            for conn in self._connections:
                await conn.disconnect()
            self._connections.clear()
        
        self._is_warmed = False
        logger.info("Sarvam manager closed")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics"""
        return {
            "pool_size": len(self._connections),
            "available_connections": sum(1 for c in self._connections if c.is_available()),
            "is_warmed": self._is_warmed,
            "connections": [
                {
                    "id": c.connection_id,
                    "state": c.state.value,
                    "messages_sent": c.metrics.messages_sent,
                    "messages_received": c.metrics.messages_received,
                    "total_audio_bytes": c.metrics.total_audio_bytes,
                    "reconnect_count": c.metrics.reconnect_count,
                }
                for c in self._connections
            ]
        }

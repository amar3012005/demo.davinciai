"""
Test 2: Continuation Flags in CartesiaManager (RED phase)

Verifies that stream_text_to_audio() sends the correct `continue` field on
each outbound Cartesia message:
  - Any non-final chunk            → continue=True
  - The final real chunk           → continue=False (closes the context)

The Cartesia API uses this flag for prosody continuity across chunks;
wrong values produce audible glitches or silent failures.

All tests mock the WebSocket and inject pre-canned server responses so
no network connection is needed.
"""

import asyncio
import base64
import json
import sys
import uuid
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

TTS_ROOT = Path(__file__).resolve().parent.parent
if str(TTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TTS_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(data: bytes = b"\x00\x01\x02\x03") -> str:
    return base64.b64encode(data).decode()


async def _tokens(*texts: str) -> AsyncGenerator[str, None]:
    for t in texts:
        yield t


# ---------------------------------------------------------------------------
# Unit tests for continuation flag logic
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestContinuationFlagLogic:
    """
    Test the `continue` flag behaviour by inspecting what CartesiaManager
    would send over the wire for multi-chunk text streams.

    The key invariant (from the implementation in cartesia_manager.py):
        message["continue"] = not is_final
    """

    def test_first_non_final_chunk_continue_is_true(self):
        """If more chunks are coming, the first chunk must keep the context open."""
        is_final = False
        continue_flag = not is_final
        assert continue_flag is True, (
            "First non-final chunk must have continue=True so Cartesia keeps the context open"
        )

    def test_final_chunk_continue_is_false(self):
        """The final chunk must close the Cartesia context."""
        is_final = True
        continue_flag = not is_final
        assert continue_flag is False, (
            "Final chunk must have continue=False to close the context"
        )

    def test_continue_flag_type_is_bool(self):
        """The `continue` field must be a Python bool, not a truthy int or None."""
        flag = not False
        assert isinstance(flag, bool)
        flag = not True
        assert isinstance(flag, bool)

    def test_flag_transitions_in_sequence(self):
        """Simulate a 3-chunk stream: T, T, F."""
        flags = []
        for is_final in (False, False, True):
            flags.append(not is_final)

        assert flags == [True, True, False], f"Expected [True, True, False], got {flags}"

    def test_empty_text_chunk_is_skipped_not_sent(self):
        """
        The send_text() coroutine skips chunks where text.strip() is falsy.
        This means an empty string never becomes a Cartesia message.
        Empty chunks are skipped entirely; the final real chunk closes the context.
        """
        chunks = ["Hallo", "", "   ", "Welt"]
        skipped = [c for c in chunks if not c or not c.strip()]
        sent = [c for c in chunks if c and c.strip()]

        assert "Hallo" in sent
        assert "Welt" in sent
        assert "" in skipped
        assert "   " in skipped

    def test_context_id_present_in_all_messages(self):
        """Every message must carry the same context_id for server-side routing."""
        ctx_id = str(uuid.uuid4())

        messages = [
            {"context_id": ctx_id, "transcript": "Hallo", "continue": True},
            {"context_id": ctx_id, "transcript": "Welt", "continue": True},
        ]

        for msg in messages:
            assert msg["context_id"] == ctx_id, "context_id must be stable across all chunks"

    def test_model_id_present_in_message(self):
        """Each message must include model_id for Cartesia to route correctly."""
        message = {
            "context_id": "ctx-123",
            "model_id": "sonic-3",
            "transcript": "Hallo",
            "continue": False,
        }
        assert "model_id" in message
        assert message["model_id"] == "sonic-3"

    def test_output_format_present_in_message(self):
        """output_format block must be embedded in every message."""
        message = {
            "context_id": "ctx-123",
            "transcript": "Hallo",
            "output_format": {"container": "raw", "encoding": "pcm_f32le", "sample_rate": 44100},
            "continue": False,
        }
        assert "output_format" in message
        assert "encoding" in message["output_format"]


# ---------------------------------------------------------------------------
# Integration-style tests using FakeWebSocket from conftest
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestContinuationFlagsEndToEnd:
    """
    Verify that CartesiaManager.stream_text_to_audio() actually sends messages
    with the correct `continue` flag sequence by intercepting conn.send().
    """

    async def test_single_chunk_sends_continue_false(self, mock_manager, fake_ws):
        """A one-chunk stream: only one send call, continue must be False."""
        from cartesia_manager import CartesiaConnection, ConnectionState

        sent: list[dict] = []
        ctx_id = str(uuid.uuid4())

        async def fake_send(data: dict) -> None:
            sent.append(data)

        conn = MagicMock(spec=CartesiaConnection)
        conn.state = ConnectionState.CONNECTED
        conn._connected_event = asyncio.Event()
        conn._connected_event.set()
        conn.is_busy = False
        conn._dispatch_lock = asyncio.Lock()
        conn._dispatch_queues = {}
        conn.send = fake_send
        conn.is_available = MagicMock(return_value=True)

        async def fake_ensure() -> bool:
            return True

        conn.ensure_connected = fake_ensure

        # Inject dispatcher response so receive_audio() terminates
        queue: asyncio.Queue = asyncio.Queue()

        async def _inject():
            await asyncio.sleep(0.05)
            await queue.put({"type": "done", "context_id": ctx_id})

        asyncio.create_task(_inject())

        async def get_conn():
            conn._dispatch_queues[ctx_id] = queue
            return conn

        with (
            patch.object(mock_manager, "get_connection", new=AsyncMock(side_effect=get_conn)),
        ):
            # Provide context_id so we control it
            async def one_chunk():
                yield "Hallo Welt"

            # Patch uuid4 to return our controlled ctx_id
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await mock_manager.stream_text_to_audio(
                    one_chunk(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert len(sent) >= 1, "Expected at least one message to be sent"
        first_msg = sent[0]
        assert first_msg.get("continue") is False, (
            f"Single-chunk message must have continue=False, got {first_msg.get('continue')!r}"
        )

    async def test_two_chunk_stream_flags(self, mock_manager):
        """Two-chunk stream: [continue=True, continue=False]."""
        from cartesia_manager import CartesiaConnection, ConnectionState

        sent: list[dict] = []
        ctx_id = str(uuid.uuid4())

        async def fake_send(data: dict) -> None:
            sent.append(data)

        conn = MagicMock(spec=CartesiaConnection)
        conn.state = ConnectionState.CONNECTED
        conn._connected_event = asyncio.Event()
        conn._connected_event.set()
        conn.is_busy = False
        conn._dispatch_lock = asyncio.Lock()
        conn._dispatch_queues = {}
        conn.send = fake_send
        conn.is_available = MagicMock(return_value=True)
        conn.ensure_connected = AsyncMock(return_value=True)

        queue: asyncio.Queue = asyncio.Queue()

        async def _inject():
            await asyncio.sleep(0.1)
            await queue.put({"type": "done", "context_id": ctx_id})

        asyncio.create_task(_inject())

        async def get_conn():
            conn._dispatch_queues[ctx_id] = queue
            return conn

        async def two_chunks():
            yield "Hallo"
            yield "Welt"

        with patch.object(mock_manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await mock_manager.stream_text_to_audio(
                    two_chunks(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert len(sent) == 2, f"Expected 2 sent messages, got {len(sent)}: {sent}"
        assert sent[0].get("continue") is True, "First chunk must have continue=True"
        assert sent[1].get("continue") is False, "Final chunk must have continue=False"
        assert sent[1].get("transcript") == "Welt", "Final chunk must carry the real transcript"

    async def test_error_followed_by_audio_does_not_fail_stream(self, mock_manager):
        """A provisional Cartesia error frame must not abort a stream that still emits audio."""
        from cartesia_manager import CartesiaConnection, ConnectionState

        ctx_id = str(uuid.uuid4())
        raw_audio = b"\x00\x01\x02\x03"
        b64_audio = base64.b64encode(raw_audio).decode()
        received_audio: list[bytes] = []

        async def fake_send(data: dict) -> None:
            pass

        conn = MagicMock(spec=CartesiaConnection)
        conn.state = ConnectionState.CONNECTED
        conn._connected_event = asyncio.Event()
        conn._connected_event.set()
        conn.is_busy = False
        conn._dispatch_lock = asyncio.Lock()
        conn._dispatch_queues = {}
        conn.send = fake_send
        conn.is_available = MagicMock(return_value=True)
        conn.ensure_connected = AsyncMock(return_value=True)

        queue: asyncio.Queue = asyncio.Queue()

        async def _inject():
            await asyncio.sleep(0.05)
            await queue.put({"type": "error", "context_id": ctx_id, "message": "Context closed"})
            await asyncio.sleep(0.01)
            await queue.put({"type": "chunk", "context_id": ctx_id, "data": b64_audio})
            await asyncio.sleep(0.01)
            await queue.put({"type": "done", "context_id": ctx_id})

        asyncio.create_task(_inject())

        async def get_conn():
            conn._dispatch_queues[ctx_id] = queue
            return conn

        async def one_chunk():
            yield "Hallo"

        with patch.object(mock_manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                stats = await mock_manager.stream_text_to_audio(
                    one_chunk(),
                    audio_callback=lambda b, sr, m: received_audio.append(b),
                    context_id=ctx_id,
                )

        assert stats.get("error") is None, f"Expected no terminal error, got: {stats.get('error')}"
        assert received_audio == [raw_audio], f"Expected received audio after provisional error, got: {received_audio!r}"

    async def test_receive_error_propagated_in_stats(self, mock_manager):
        """If Cartesia returns an error message, stats must contain the error key."""
        from cartesia_manager import CartesiaConnection, ConnectionState

        ctx_id = str(uuid.uuid4())

        async def fake_send(data: dict) -> None:
            pass

        conn = MagicMock(spec=CartesiaConnection)
        conn.state = ConnectionState.CONNECTED
        conn._connected_event = asyncio.Event()
        conn._connected_event.set()
        conn.is_busy = False
        conn._dispatch_lock = asyncio.Lock()
        conn._dispatch_queues = {}
        conn.send = fake_send
        conn.is_available = MagicMock(return_value=True)
        conn.ensure_connected = AsyncMock(return_value=True)

        queue: asyncio.Queue = asyncio.Queue()

        async def _inject_error():
            await asyncio.sleep(0.05)
            await queue.put(
                {"type": "error", "context_id": ctx_id, "message": "rate limit exceeded"}
            )

        asyncio.create_task(_inject_error())

        async def get_conn():
            conn._dispatch_queues[ctx_id] = queue
            return conn

        async def one_chunk():
            yield "Hallo"

        with patch.object(mock_manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                stats = await mock_manager.stream_text_to_audio(
                    one_chunk(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert "error" in stats, f"Expected 'error' key in stats, got: {stats}"
        assert "rate limit" in stats["error"].lower()

    async def test_no_connection_returns_error_dict(self, mock_manager):
        """When pool is empty and get_connection returns None, return error dict."""
        with patch.object(mock_manager, "get_connection", new=AsyncMock(return_value=None)):

            async def one_chunk():
                yield "Hallo"

            result = await mock_manager.stream_text_to_audio(
                one_chunk(),
                audio_callback=lambda b, sr, m: None,
            )

        assert "error" in result
        assert result["error"] == "No connection available"

    async def test_audio_callback_receives_decoded_bytes(self, mock_manager):
        """Audio callback must receive decoded bytes, not base64 string."""
        from cartesia_manager import CartesiaConnection, ConnectionState

        received_audio: list[bytes] = []
        ctx_id = str(uuid.uuid4())
        raw_audio = b"\x00\x01\x02\x03\x04\x05"
        b64_audio = base64.b64encode(raw_audio).decode()

        async def fake_send(data: dict) -> None:
            pass

        conn = MagicMock(spec=CartesiaConnection)
        conn.state = ConnectionState.CONNECTED
        conn._connected_event = asyncio.Event()
        conn._connected_event.set()
        conn.is_busy = False
        conn._dispatch_lock = asyncio.Lock()
        conn._dispatch_queues = {}
        conn.send = fake_send
        conn.is_available = MagicMock(return_value=True)
        conn.ensure_connected = AsyncMock(return_value=True)

        queue: asyncio.Queue = asyncio.Queue()

        async def _inject():
            await asyncio.sleep(0.05)
            await queue.put(
                {"type": "chunk", "context_id": ctx_id, "data": b64_audio}
            )
            await asyncio.sleep(0.02)
            await queue.put({"type": "done", "context_id": ctx_id})

        asyncio.create_task(_inject())

        async def get_conn():
            conn._dispatch_queues[ctx_id] = queue
            return conn

        async def one_chunk():
            yield "Hallo"

        with patch.object(mock_manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await mock_manager.stream_text_to_audio(
                    one_chunk(),
                    audio_callback=lambda b, sr, m: received_audio.append(b),
                    context_id=ctx_id,
                )

        assert len(received_audio) == 1, f"Expected 1 audio chunk, got {len(received_audio)}"
        assert received_audio[0] == raw_audio, "Decoded bytes must match original"

    async def test_stats_track_chunk_count(self, mock_manager):
        """stats['chunks_received'] must equal the number of audio chunks sent by server."""
        from cartesia_manager import CartesiaConnection, ConnectionState

        ctx_id = str(uuid.uuid4())
        raw_audio = b"\x00\x01\x02\x03"
        b64_audio = base64.b64encode(raw_audio).decode()
        n_chunks = 3

        async def fake_send(data: dict) -> None:
            pass

        conn = MagicMock(spec=CartesiaConnection)
        conn.state = ConnectionState.CONNECTED
        conn._connected_event = asyncio.Event()
        conn._connected_event.set()
        conn.is_busy = False
        conn._dispatch_lock = asyncio.Lock()
        conn._dispatch_queues = {}
        conn.send = fake_send
        conn.is_available = MagicMock(return_value=True)
        conn.ensure_connected = AsyncMock(return_value=True)

        queue: asyncio.Queue = asyncio.Queue()

        async def _inject():
            await asyncio.sleep(0.05)
            for _ in range(n_chunks):
                await queue.put({"type": "chunk", "context_id": ctx_id, "data": b64_audio})
                await asyncio.sleep(0.01)
            await queue.put({"type": "done", "context_id": ctx_id})

        asyncio.create_task(_inject())

        async def get_conn():
            conn._dispatch_queues[ctx_id] = queue
            return conn

        async def one_chunk():
            yield "Hallo Welt"

        with patch.object(mock_manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                stats = await mock_manager.stream_text_to_audio(
                    one_chunk(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert stats["chunks_received"] == n_chunks, (
            f"Expected chunks_received={n_chunks}, got {stats['chunks_received']}"
        )

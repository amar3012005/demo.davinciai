"""
Test 6: End-to-End Integration — German Text → Spacing → Continuation → Audio (RED phase)

This integration test wires together:
  1. tts_safe() from context_architecture_bundb.py   (German pronunciation)
  2. Token spacing logic from pipeline.py             (assemble_tokens)
  3. CartesiaManager.stream_text_to_audio()           (continuation flags + audio)

The full path is:
  raw German text
    → tts_safe() → spoken-form text
    → token stream (simulated by splitting on spaces)
    → assemble_tokens() → correctly spaced string
    → CartesiaManager → Cartesia API (mocked) → audio bytes
    → audio_callback receives decoded PCM bytes

Goals:
  - Verify the three stages compose correctly without data loss
  - Verify continuation flags on a multi-token German sentence
  - Verify audio bytes reach callback decoded (not base64)
  - Verify stats are populated: chunks_received, total_audio_bytes, context_id

No real network calls are made; all Cartesia I/O is intercepted.
"""

import asyncio
import base64
import sys
import uuid
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

TTS_ROOT = Path(__file__).resolve().parent.parent
RAG_EU_ROOT = Path(__file__).resolve().parent.parent.parent / "rag-eu"
ORCHESTRATOR_ROOT = Path(__file__).resolve().parent.parent.parent / "Orchestrator-eu"

for root in (TTS_ROOT, RAG_EU_ROOT, ORCHESTRATOR_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tts_safe_german(text: str) -> str:
    """Import and call tts_safe() from the bundb context architecture."""
    from context_architecture.context_architecture_bundb import tts_safe
    return tts_safe(text)


def assemble_tokens(tokens: list[str]) -> str:
    """Replicate pipeline.py token assembly logic."""
    parts: list[str] = []
    for t in tokens:
        if parts and t and not parts[-1][-1:].isspace() and not t[:1].isspace():
            parts.append(" ")
        parts.append(t)
    return "".join(parts)


SAMPLE_AUDIO = b"\x00\x01\x02\x03\x04\x05\x06\x07"
B64_AUDIO = base64.b64encode(SAMPLE_AUDIO).decode()


# ---------------------------------------------------------------------------
# Integration Test 1: tts_safe → assemble_tokens round-trip
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestGermanTextPreprocessing:
    """Stage 1+2: German text preprocessing and token assembly."""

    def test_ki_expands_and_assembles_correctly(self):
        """'KI' expands to 'künstliche Intelligenz' and the sentence reassembles cleanly."""
        raw = "KI ist wichtig für Marken."
        processed = tts_safe_german(raw)

        # KI must have been expanded
        assert "künstliche Intelligenz" in processed, (
            f"Expected 'künstliche Intelligenz' in: {processed!r}"
        )

        # Now simulate the token stream (split on spaces)
        tokens = processed.split(" ")
        reassembled = assemble_tokens(tokens)

        # Must not have double spaces
        assert "  " not in reassembled, f"Double space in: {reassembled!r}"
        # Must still contain the expansion
        assert "künstliche Intelligenz" in reassembled or "künstliche" in reassembled

    def test_protected_bb_dot_survives_preprocessing(self):
        """B&B. must survive tts_safe and remain in token stream."""
        raw = "Das Team von B&B. ist professionell."
        processed = tts_safe_german(raw)
        tokens = processed.split(" ")
        reassembled = assemble_tokens(tokens)

        assert "B&B." in reassembled or "B und B" in reassembled, (
            f"Protected word not found in: {reassembled!r}"
        )

    def test_markdown_stripped_before_tts(self):
        """Markdown must be stripped before the tokens reach the TTS stage."""
        raw = "**Wichtig**: Marke ist alles."
        processed = tts_safe_german(raw)
        assert "**" not in processed

        tokens = processed.split(" ")
        reassembled = assemble_tokens(tokens)
        assert "**" not in reassembled

    def test_no_double_spaces_in_assembled_output(self):
        """After tts_safe + assemble_tokens, output must have no consecutive spaces."""
        sentences = [
            "Das ist eine Markenagentur.",
            "KI und UX sind Schlüsselthemen.",
            "Wir helfen bei DSGVO-Fragen.",
            "B&B. ist in Hannover ansässig.",
        ]
        for raw in sentences:
            processed = tts_safe_german(raw)
            tokens = processed.split(" ")
            result = assemble_tokens(tokens)
            assert "  " not in result, f"Double space in output for {raw!r}: {result!r}"

    def test_empty_string_pipeline_does_not_crash(self):
        """Empty string must pass through all stages without error."""
        processed = tts_safe_german("")
        tokens = processed.split(" ") if processed else []
        result = assemble_tokens(tokens)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Integration Test 2: CartesiaManager end-to-end with mock Cartesia server
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCartersiaManagerEndToEnd:
    """Stage 3: CartesiaManager streams text tokens and delivers audio to callback."""

    def _make_manager_with_mock_conn(self, ctx_id: str, n_audio_chunks: int = 2):
        """
        Build a CartesiaManager with a mocked connection.
        Returns (manager, conn, queue, sent_messages_list).
        """
        from config import CartesiaConfig
        from cartesia_manager import CartesiaManager, CartesiaConnection, ConnectionState

        with patch.dict(
            "os.environ",
            {
                "CARTESIA_API_KEY": "test-api-key-integration-01",
                "CARTESIA_VOICE_ID": "voice-de-test",
            },
        ):
            config = CartesiaConfig()

        manager = CartesiaManager(config)

        sent: list[dict] = []

        async def fake_send(data: dict) -> None:
            sent.append(data)

        conn = MagicMock(spec=CartesiaConnection)
        conn.state = ConnectionState.CONNECTED
        conn._connected_event = asyncio.Event()
        conn._connected_event.set()
        conn.is_busy = False
        conn._dispatch_lock = asyncio.Lock()
        conn._dispatch_queues = {}
        conn.is_available = MagicMock(return_value=True)
        conn.ensure_connected = AsyncMock(return_value=True)
        conn.send = fake_send

        queue: asyncio.Queue = asyncio.Queue()

        async def _inject_responses():
            await asyncio.sleep(0.1)
            for _ in range(n_audio_chunks):
                await queue.put({"type": "chunk", "context_id": ctx_id, "data": B64_AUDIO})
                await asyncio.sleep(0.01)
            await queue.put({"type": "done", "context_id": ctx_id})

        asyncio.create_task(_inject_responses())

        async def get_conn():
            conn._dispatch_queues[ctx_id] = queue
            return conn

        return manager, conn, queue, sent, get_conn

    async def test_audio_bytes_reach_callback(self):
        """Audio callback receives decoded bytes matching the injected audio."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        received: list[bytes] = []

        async def one_token():
            yield "Hallo Welt"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: received.append(b),
                    context_id=ctx_id,
                )

        assert len(received) == 2, f"Expected 2 audio chunks, got {len(received)}"
        for chunk in received:
            assert chunk == SAMPLE_AUDIO, f"Decoded bytes mismatch: {chunk!r}"

    async def test_stats_context_id_matches_requested(self):
        """stats['context_id'] must equal the context_id passed to stream_text_to_audio."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        async def one_token():
            yield "Hallo"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                stats = await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert stats["context_id"] == ctx_id

    async def test_stats_total_audio_bytes_populated(self):
        """stats['total_audio_bytes'] must reflect total bytes delivered."""
        ctx_id = str(uuid.uuid4())
        n_chunks = 3
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(
            ctx_id, n_audio_chunks=n_chunks
        )

        async def one_token():
            yield "Guten Morgen"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                stats = await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        expected_bytes = len(SAMPLE_AUDIO) * n_chunks
        assert stats["total_audio_bytes"] == expected_bytes, (
            f"Expected {expected_bytes} bytes, got {stats['total_audio_bytes']}"
        )

    async def test_single_message_has_continue_false(self):
        """Single-chunk streams must close immediately with continue=False."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        async def one_token():
            yield "Hallo Welt"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert len(sent) >= 1, "At least one message must have been sent"
        assert sent[0].get("continue") is False, (
            f"Single message must have continue=False, got {sent[0].get('continue')!r}"
        )

    async def test_multi_chunk_stream_uses_true_then_false(self):
        """Multi-chunk streams must keep the context open until the final chunk."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        async def two_tokens():
            yield "Hallo"
            yield " Welt"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    two_tokens(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert len(sent) == 2, f"Expected 2 messages, got {len(sent)}: {sent}"
        assert sent[0].get("continue") is True, f"Expected first chunk continue=True, got {sent[0].get('continue')!r}"
        assert sent[1].get("continue") is False, f"Expected final chunk continue=False, got {sent[1].get('continue')!r}"

    async def test_german_text_pipeline_full_flow(self):
        """
        Full pipeline: raw German text → tts_safe → token stream
        → CartesiaManager → audio callback.

        Verifies the three stages compose without data loss or crashes.
        """
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(
            ctx_id, n_audio_chunks=1
        )

        raw_german = "KI ist entscheidend für B&B. Marken."
        processed = tts_safe_german(raw_german)

        # Split into tokens to simulate a streaming RAG response
        raw_tokens = processed.split(" ")
        assembled = assemble_tokens(raw_tokens)

        received: list[bytes] = []

        async def token_stream():
            for token in raw_tokens:
                if token.strip():
                    yield token

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                stats = await manager.stream_text_to_audio(
                    token_stream(),
                    audio_callback=lambda b, sr, m: received.append(b),
                    context_id=ctx_id,
                    language="de",
                )

        # All stages completed without error
        assert "error" not in stats, f"Unexpected error in stats: {stats.get('error')}"
        assert stats["chunks_received"] == 1
        assert len(received) == 1
        assert received[0] == SAMPLE_AUDIO

    async def test_language_de_passed_to_cartesia(self):
        """Language 'de' must appear in the Cartesia message for correct phonology."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        async def one_token():
            yield "Hallo"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                    language="de-DE",
                )

        assert len(sent) >= 1
        first_msg = sent[0]
        assert first_msg.get("language") == "de", (
            f"Expected language='de' (normalized from 'de-DE'), got {first_msg.get('language')!r}"
        )

    async def test_language_normalisation_de_de_to_de(self):
        """'de-DE' must be normalised to 'de' before sending to Cartesia."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        async def one_token():
            yield "Test"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                    language="de-DE",
                )

        for msg in sent:
            lang = msg.get("language")
            assert "-" not in str(lang), (
                f"Language must be normalized (no hyphen), got {lang!r}"
            )

    async def test_async_audio_callback_awaited(self):
        """If audio_callback returns a coroutine, it must be awaited."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        awaited_count: list[int] = [0]

        async def async_callback(audio_bytes: bytes, sample_rate: int, metadata: dict) -> None:
            awaited_count[0] += 1

        async def one_token():
            yield "Hallo"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                stats = await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=async_callback,
                    context_id=ctx_id,
                )

        assert awaited_count[0] == stats["chunks_received"], (
            "Async callback must be awaited once per received chunk"
        )

    async def test_emotion_tag_mapped_before_send(self):
        """Emotion tags must be validated/mapped before reaching Cartesia."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        # 'enthusiastic' is not a valid Cartesia emotion — it maps to 'positive'
        text_with_emotion = 'Hallo! <emotion value="enthusiastic" />'

        async def one_token():
            yield text_with_emotion

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert len(sent) >= 1
        transcript = sent[0].get("transcript", "")
        # Must NOT contain the invalid emotion value
        assert 'value="enthusiastic"' not in transcript, (
            f"Invalid emotion 'enthusiastic' must be mapped. Transcript: {transcript!r}"
        )

    async def test_invalid_emotion_stripped_not_sent(self):
        """Emotions with no valid mapping (e.g. 'calm') must be stripped."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        # 'calm' maps to None → must be stripped
        text_with_calm = 'Alles ruhig. <emotion value="calm" />'

        async def one_token():
            yield text_with_calm

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert len(sent) >= 1
        transcript = sent[0].get("transcript", "")
        assert "<emotion" not in transcript, (
            f"Emotion tag must be stripped for 'calm'. Transcript: {transcript!r}"
        )

    async def test_newlines_in_tokens_replaced_with_space(self):
        """Newline characters in tokens must be replaced with spaces before sending."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        async def one_token():
            yield "Hallo\nWelt"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert len(sent) >= 1
        transcript = sent[0].get("transcript", "")
        assert "\n" not in transcript, (
            f"Newlines must be replaced with spaces. Transcript: {transcript!r}"
        )

    async def test_speed_included_in_message_when_configured(self):
        """If config.speed is set, it must appear in the Cartesia message."""
        ctx_id = str(uuid.uuid4())
        manager, conn, queue, sent, get_conn = self._make_manager_with_mock_conn(ctx_id)

        # Ensure config has speed set
        assert hasattr(manager.config, "speed")
        assert manager.config.speed is not None

        async def one_token():
            yield "Test"

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                await manager.stream_text_to_audio(
                    one_token(),
                    audio_callback=lambda b, sr, m: None,
                    context_id=ctx_id,
                )

        assert len(sent) >= 1
        assert "speed" in sent[0], (
            f"Speed must be included in Cartesia message when config.speed is set. "
            f"Message keys: {list(sent[0].keys())}"
        )


# ---------------------------------------------------------------------------
# Integration Test 3: Synthesize helper
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSynthesizeHelper:
    """CartesiaManager.synthesize() convenience method."""

    async def test_synthesize_returns_bytes(self):
        """synthesize() must return complete audio bytes joined from all chunks."""
        from config import CartesiaConfig
        from cartesia_manager import CartesiaManager, CartesiaConnection, ConnectionState

        ctx_id = str(uuid.uuid4())

        with patch.dict(
            "os.environ",
            {
                "CARTESIA_API_KEY": "test-synth-key-abcdef123456",
                "CARTESIA_VOICE_ID": "voice-synth-test",
            },
        ):
            config = CartesiaConfig()

        manager = CartesiaManager(config)

        async def fake_send(data: dict) -> None:
            pass

        conn = MagicMock(spec=CartesiaConnection)
        conn.state = ConnectionState.CONNECTED
        conn._connected_event = asyncio.Event()
        conn._connected_event.set()
        conn.is_busy = False
        conn._dispatch_lock = asyncio.Lock()
        conn._dispatch_queues = {}
        conn.is_available = MagicMock(return_value=True)
        conn.ensure_connected = AsyncMock(return_value=True)
        conn.send = fake_send

        queue: asyncio.Queue = asyncio.Queue()

        async def _inject():
            await asyncio.sleep(0.05)
            await queue.put({"type": "chunk", "context_id": ctx_id, "data": B64_AUDIO})
            await queue.put({"type": "chunk", "context_id": ctx_id, "data": B64_AUDIO})
            await queue.put({"type": "done", "context_id": ctx_id})

        asyncio.create_task(_inject())

        async def get_conn():
            conn._dispatch_queues[ctx_id] = queue
            return conn

        with patch.object(manager, "get_connection", new=AsyncMock(side_effect=get_conn)):
            with patch("cartesia_manager.uuid.uuid4", return_value=ctx_id):
                result = await manager.synthesize("Hallo Welt", language="de")

        assert isinstance(result, bytes), f"synthesize() must return bytes, got {type(result)}"
        assert result == SAMPLE_AUDIO + SAMPLE_AUDIO, (
            "Synthesize result must be concatenation of all chunks"
        )

    async def test_synthesize_returns_empty_bytes_on_error(self):
        """synthesize() must return b'' when the stream fails."""
        from config import CartesiaConfig
        from cartesia_manager import CartesiaManager

        with patch.dict(
            "os.environ",
            {
                "CARTESIA_API_KEY": "test-err-key-abcdef123456",
                "CARTESIA_VOICE_ID": "voice-err-test",
            },
        ):
            config = CartesiaConfig()

        manager = CartesiaManager(config)

        with patch.object(manager, "get_connection", new=AsyncMock(return_value=None)):
            result = await manager.synthesize("Hallo")

        assert result == b"", f"Expected b'', got {result!r}"

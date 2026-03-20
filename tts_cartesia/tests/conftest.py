"""
Shared pytest fixtures for tts_cartesia test suite.

All external dependencies (Cartesia WebSocket, asyncio tasks) are mocked
so tests run without network access and without a real API key.
"""

import asyncio
import inspect
import json
import sys
import uuid
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path wiring — add tts_cartesia root so imports resolve without installation
# ---------------------------------------------------------------------------
TTS_ROOT = Path(__file__).resolve().parent.parent
if str(TTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TTS_ROOT))


# ---------------------------------------------------------------------------
# Async test runner (mirrors rag-eu/tests/conftest.py pattern)
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.addinivalue_line("markers", "unit: pure unit tests")
    config.addinivalue_line("markers", "integration: integration tests")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):  # type: ignore[override]
    test_fn = pyfuncitem.obj
    if inspect.iscoroutinefunction(test_fn):
        # Only pass fixtures that the test function actually accepts
        sig = inspect.signature(test_fn)
        accepted_args = {k: v for k, v in pyfuncitem.funcargs.items() if k in sig.parameters}
        asyncio.run(test_fn(**accepted_args))
        return True
    return None


# ---------------------------------------------------------------------------
# CartesiaConfig fixture — bypasses env-var requirement for api_key
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_config():
    """A fully-populated CartesiaConfig that does NOT hit any real service."""
    from config import CartesiaConfig

    with patch.dict(
        "os.environ",
        {
            "CARTESIA_API_KEY": "test-api-key-abcdef1234567890",
            "CARTESIA_VOICE_ID": "test-voice-id-123",
            "CARTESIA_MODEL": "sonic-3",
            "CARTESIA_SAMPLE_RATE": "16000",
            "CARTESIA_OUTPUT_FORMAT": "pcm_s16le",
            "CARTESIA_SPEED": "0.95",
            "CARTESIA_LANGUAGE": "de",
        },
    ):
        return CartesiaConfig()


@pytest.fixture
def minimal_config():
    """Minimum viable config — only api_key set."""
    from config import CartesiaConfig

    with patch.dict(
        "os.environ",
        {
            "CARTESIA_API_KEY": "test-api-key-minimum",
        },
        clear=False,
    ):
        # Remove optional vars so defaults kick in
        env_overrides = {
            "CARTESIA_API_KEY": "test-api-key-minimum",
            "CARTESIA_VOICE_ID": "voice-abc",
            "CARTESIA_SAMPLE_RATE": "44100",
            "CARTESIA_OUTPUT_FORMAT": "pcm_f32le",
            "CARTESIA_SPEED": "0.9",
            "CARTESIA_LANGUAGE": "de",
        }
        with patch.dict("os.environ", env_overrides):
            return CartesiaConfig()


# ---------------------------------------------------------------------------
# Mock WebSocket helpers
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """
    Minimal WebSocket stub that records sent messages and lets tests
    inject pre-canned responses via `incoming_messages`.
    """

    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent_messages.append(json.loads(raw))

    def inject_response(self, payload: dict) -> None:
        """Queue a server-side JSON message for the next recv() call."""
        self._incoming.put_nowait(json.dumps(payload))

    def inject_done(self, context_id: str) -> None:
        self.inject_response({"type": "done", "context_id": context_id})

    def inject_chunk(self, context_id: str, b64_audio: str = "AAAA") -> None:
        self.inject_response(
            {"type": "chunk", "context_id": context_id, "data": b64_audio}
        )

    async def close(self) -> None:
        self.closed = True

    # websockets.ClientConnection compatibility
    @property
    def state(self):
        from websockets.protocol import State

        return State.OPEN if not self.closed else State.CLOSED

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            msg = await asyncio.wait_for(self._incoming.get(), timeout=0.5)
            return msg
        except asyncio.TimeoutError:
            raise StopAsyncIteration


@pytest.fixture
def fake_ws() -> FakeWebSocket:
    return FakeWebSocket()


# ---------------------------------------------------------------------------
# CartesiaConnection fixture with mocked underlying WebSocket
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_connection(valid_config):
    """A CartesiaConnection where the real WS is replaced by FakeWebSocket."""
    from cartesia_manager import CartesiaConnection, ConnectionState

    conn = CartesiaConnection(valid_config, connection_id="test-conn-0")
    conn.state = ConnectionState.CONNECTED
    conn._connected_event.set()
    conn.ws = FakeWebSocket()
    return conn


# ---------------------------------------------------------------------------
# CartesiaManager fixture — pool NOT warmed (no real WS connections opened)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_manager(valid_config):
    """CartesiaManager with no pre-warmed connections."""
    from cartesia_manager import CartesiaManager

    return CartesiaManager(valid_config)


# ---------------------------------------------------------------------------
# Async text iterator helpers
# ---------------------------------------------------------------------------

async def _tokens(*texts: str) -> AsyncGenerator[str, None]:
    for t in texts:
        yield t


def token_stream(*texts: str) -> AsyncGenerator[str, None]:
    """Build an async generator from a list of text tokens."""
    return _tokens(*texts)

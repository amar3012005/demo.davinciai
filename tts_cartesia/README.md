# TTS-Cartesia Microservice

Ultra-low latency Text-to-Speech streaming service using Cartesia AI.

## Features

- **Ultra-low latency**: ~40ms time-to-first-audio (vs ~150ms for ElevenLabs)
- **Robust connection management**: Connection pooling with automatic reconnection
- **Prosody continuity**: `context_id` tracking for natural multi-turn conversations
- **WebSocket streaming**: Real-time audio streaming to clients
- **HTTP synthesis**: Fallback endpoint for simple use cases

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Orchestrator   │────▶│  TTS-Cartesia    │────▶│  Cartesia   │
│                 │◀────│  (this service)  │◀────│    API      │
└─────────────────┘     └──────────────────┘     └─────────────┘
     WebSocket              Connection Pool           wss://
```

## Quick Start

### Environment Variables

```bash
CARTESIA_API_KEY=your_api_key          # Required: Get from https://play.cartesia.ai/
CARTESIA_VOICE_ID=voice_id             # Voice ID (default: English male)
CARTESIA_MODEL=sonic-2                 # Model: sonic-3, sonic-2, sonic
CARTESIA_SAMPLE_RATE=24000             # Audio sample rate
CARTESIA_OUTPUT_FORMAT=pcm_s16le       # Audio format
CARTESIA_LANGUAGE=en                   # Language code
CONNECTION_POOL_SIZE=3                 # Pre-warmed connections
TTS_CARTESIA_PORT=8000                 # Service port
```

### Docker Run

```bash
docker build -t tts-cartesia .

docker run -d \
  --name tts-cartesia \
  -p 5010:8000 \
  -e CARTESIA_API_KEY="your_api_key" \
  -e CARTESIA_VOICE_ID="your_voice_id" \
  tts-cartesia
```

### Test Client

Open http://localhost:5010/client for the interactive testing interface.

## API Endpoints

### Health Check
```bash
GET /health
```

### HTTP Synthesis
```bash
POST /api/v1/synthesize
Content-Type: application/json

{
  "text": "Hello world",
  "voice_id": "optional_voice_id",
  "language": "en"
}
```

### WebSocket Streaming
```javascript
const ws = new WebSocket('ws://localhost:5010/api/v1/stream');

// On connection
ws.onopen = () => {
  // Single synthesis
  ws.send(JSON.stringify({
    type: 'synthesize',
    text: 'Hello world'
  }));
};

// Receive audio chunks
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'audio') {
    // data.data = base64 encoded PCM audio
    // data.sample_rate = 24000
  }
  if (data.type === 'complete') {
    // Synthesis finished
  }
};
```

## WebSocket Protocol

### Client → Server Messages

| Type | Description | Example |
|------|-------------|---------|
| `prewarm` | Pre-warm connection (optional) | `{"type": "prewarm"}` |
| `synthesize` | Full synthesis request | `{"type": "synthesize", "text": "..."}` |
| `stream_chunk` | Streaming text chunk | `{"type": "stream_chunk", "text": "..."}` |
| `stream_end` | End of streaming | `{"type": "stream_end"}` |

### Server → Client Messages

| Type | Description |
|------|-------------|
| `connected` | Connection confirmed with session_id |
| `audio` | Audio chunk (base64 PCM) |
| `complete` | Synthesis complete with stats |
| `error` | Error message |

## Performance

| Metric | Target | Typical |
|--------|--------|---------|
| First Audio Chunk | <50ms | ~40ms |
| Connection Warmup | <2s | ~1s |
| Reconnection | <3s | ~1.5s |
| Concurrent Sessions | 10+ | Unlimited* |

*Limited by connection pool size and API rate limits.

## Comparison with TTS_LABS (ElevenLabs)

| Feature | TTS_LABS | TTS-Cartesia |
|---------|----------|--------------|
| First Audio | ~150ms | **~40ms** |
| Model | eleven_turbo_v2_5 | sonic-2/sonic-3 |
| Connection | Per-request | **Pooled** |
| Prosody Continuity | Per-connection | **context_id** |
| SDK | Raw WebSocket | Raw WebSocket |

## Integration with Orchestrator

Update `docker-compose-daytona-v2.yml`:

```yaml
orchestrator-daytona.v2:
  environment:
    - TTS_SERVICE_URL=http://tts-cartesia:8000
```

Or for external access:
```yaml
    - TTS_SERVICE_URL=http://host.docker.internal:5010
```

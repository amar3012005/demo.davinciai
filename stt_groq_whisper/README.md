# Groq Whisper STT Microservice

Ultra-low latency speech-to-text using Groq's Whisper API with micro-chunking strategy.

## Architecture

This service implements a "micro-chunking loop" to simulate real-time streaming with Groq's REST API:

1. **Buffer audio**: Accumulate 300ms audio chunks from WebSocket client
2. **Send to Groq**: Call `/v1/audio/transcriptions` with context from previous transcriptions
3. **Stream results**: Emit partial transcripts immediately for real-time UX
4. **Context chain**: Pass previous transcript as `prompt` to maintain coherence

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Service info |
| `GET /health` | Health check (add `?deep=true` for upstream check) |
| `GET /metrics` | Performance metrics |
| `WS /api/v1/transcribe/stream` | WebSocket for audio streaming |

## WebSocket Protocol

**Client → Server:**
- Binary: Raw PCM audio (16kHz, 16-bit, mono)
- JSON: `{"type": "ping"}` for keepalive

**Server → Client:**
- `{"type": "connected", "session_id": "...", "message": "..."}`
- `{"type": "data", "data": {"transcript": "...", "is_final": false}}`
- `{"type": "events", "data": {"event_type": "vad_event", "signal_type": "SPEECH_START"}}`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | Required | Groq API key |
| `GROQ_WHISPER_MODEL` | `whisper-large-v3-turbo` | Whisper model |
| `CHUNK_DURATION_MS` | `300` | Micro-chunk duration |
| `OVERLAP_DURATION_MS` | `50` | Overlap between chunks |
| `ORCHESTRATOR_WS_URL` | None | Direct WebSocket to orchestrator |
| `LOG_LEVEL` | `INFO` | Logging level |

## Running Locally

```bash
cd stt_groq_whisper
pip install -r requirements.txt
GROQ_API_KEY=your_key python app.py
```

## Docker

```bash
docker build -t stt-groq-whisper -f stt_groq_whisper/Dockerfile .
docker run -p 8002:8002 -e GROQ_API_KEY=your_key stt-groq-whisper
```

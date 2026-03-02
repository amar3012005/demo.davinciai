# Orchestra-daytona

Generalized orchestrator for Daytona with multi-language support (English & German).

## Enterprise Templating

This copy is prepared as an enterprise template (`Orchestrator-eu`) with env-driven identity:

- `ORGANIZATION_NAME`
- `ORGANIZATION_FULL_NAME`
- `AGENT_NAME`
- `AGENT_ID`
- `TENANT_ID`

Use `.env.example` as the starting point for tenant-specific deployments.

## Features

- **Unified WebSocket Architecture**: Single bidirectional connection for audio I/O
- **Multi-Language Support**: English and German with automatic language detection
- **YAML Configuration**: Structured, customizable configuration
- **Generic Service Interfaces**: Configurable STT, TTS, and RAG services
- **Dialogue Management**: Configurable fillers, greetings, timeouts per language

## Architecture

```
Browser (WebSocket)
    ↓
Orchestra-daytona (/ws)
    ├─ STT Service (WebSocket)
    ├─ RAG Service (HTTP)
    └─ TTS Service (WebSocket)
```

## Configuration

Edit `config.yaml` to customize:

- **Services**: STT, TTS, RAG URLs and settings
- **Languages**: Supported languages and default
- **Dialogues**: Greetings, fillers, timeouts per language
- **Session**: Timeout settings, TTL

## Usage

### Local Development

```bash
cd orchestra_daytona
pip install -r requirements.txt
python app.py
```

### Docker

```bash
docker build -t orchestra-daytona .
docker run -p 8004:8004 orchestra-daytona
```

### WebSocket Connection

Connect to `ws://localhost:8004/ws?session_id=optional_id`

## Message Protocol

### Client → Server

- `audio_chunk`: Base64-encoded audio data
- `playback_done`: Browser confirms audio playback complete
- `interrupt`: User interrupt signal
- `start_session`: Start session with intro
- `end_session`: End session gracefully

### Server → Client

- `session_ready`: Session initialized
- `transcript`: STT transcription (with language)
- `agent_response`: RAG/LLM response tokens
- `audio_chunk`: TTS audio chunks (base64)
- `state_update`: State machine transitions

## Configuration Example

See `config.yaml` for full configuration options.

## License

MIT










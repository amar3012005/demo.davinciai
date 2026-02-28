# TARA Platform — API Reference

> **Base URL (local):** `http://localhost:8004`  
> **Base URL (production):** `https://your-deployment-domain`  
> All endpoints are accessed through the **Orchestrator** (port 8004), which proxies internally to RAG (8003), STT (8002), and TTS (8000).

---

## Table of Contents

- [1. Health & Metrics](#1-health--metrics)
- [2. Main WebSocket (Voice + Text)](#2-main-websocket-voice--text)
- [3. Audio Stream WebSocket](#3-audio-stream-websocket)
- [4. RAG Query Endpoints](#4-rag-query-endpoints)
- [5. HiveMind Knowledge Management](#5-hivemind-knowledge-management)
- [6. HiveMind Visualization & Insights](#6-hivemind-visualization--insights)
- [7. HiveMind WebSocket (Real-time Updates)](#7-hivemind-websocket-real-time-updates)
- [8. Session Management](#8-session-management)
- [9. Visual Co-Pilot (TARA)](#9-visual-co-pilot-tara)
- [10. Phone (Twilio) Integration](#10-phone-twilio-integration)
- [11. Typing Stream (SSE)](#11-typing-stream-sse)
- [12. Static Assets](#12-static-assets)
- [13. Frontend Integration Guide](#13-frontend-integration-guide)

---

## 1. Health & Metrics

### `GET /health`

Service health check. Use `?deep=true` for full connectivity verification.

```bash
curl http://localhost:8004/health?deep=true
```

**Response:**
```json
{
  "status": "healthy",
  "services": {
    "rag": { "healthy": true },
    "stt": { "healthy": true },
    "tts": { "healthy": true }
  }
}
```

### `GET /metrics`

Internal performance metrics.

```bash
curl http://localhost:8004/metrics
```

**Response:**
```json
{
  "requests_total": 142,
  "errors_total": 3,
  "active_sessions": 2,
  "uptime_seconds": 86400
}
```

### `GET /api/v1/metrics`

Public dashboard metrics (system health + performance).

```bash
curl http://localhost:8004/api/v1/metrics
```

---

## 2. Main WebSocket (Voice + Text)

### `WS /ws`

The primary bidirectional WebSocket for voice conversations. Handles audio input, TTS output, state sync, and interrupts.

**Connection:**
```
ws://localhost:8004/ws?session_id=<optional_session_id>
```

If `session_id` is omitted, one is auto-generated.

#### Handshake Flow

```
1. Client opens:    ws://localhost:8004/ws?session_id=my-session-123
2. Server accepts + sends:  { "type": "session_start", "session_id": "my-session-123", "state": "IDLE" }
3. Client sends config:     { "type": "config", "stt_mode": "audio", "tts_mode": "audio" }
4. Server confirms:         { "type": "config_ack", "stt_mode": "audio", "tts_mode": "audio" }
```

#### Client → Server Messages

| Message Type | Payload | Description |
|---|---|---|
| `config` | `{ "type": "config", "stt_mode": "audio"\|"text", "tts_mode": "audio"\|"text" }` | Set input/output modes |
| `audio_chunk` | `{ "type": "audio_chunk", "data": "<base64_pcm>" }` | Send microphone audio (PCM f32le, 44100Hz) |
| `text_input` | `{ "type": "text_input", "text": "Hello" }` | Send text instead of audio (text mode) |
| `interrupt` | `{ "type": "interrupt" }` | Barge-in: stop current TTS playback |
| `end_session` | `{ "type": "end_session" }` | Gracefully end the session |
| `ping` | `{ "type": "ping" }` | Keep-alive ping |

#### Server → Client Messages

| Message Type | Payload | Description |
|---|---|---|
| `session_start` | `{ "type": "session_start", "session_id": "...", "state": "IDLE" }` | Connection established |
| `state_change` | `{ "type": "state_change", "from": "IDLE", "to": "LISTENING" }` | FSM state transition |
| `transcript` | `{ "type": "transcript", "text": "...", "is_final": true }` | STT transcription result |
| `agent_response` | `{ "type": "agent_response", "text": "...", "sources": [...], "metadata": {...} }` | RAG answer |
| `audio_chunk` | `{ "type": "audio_chunk", "data": "<base64_pcm>", "sample_rate": 44100 }` | TTS audio chunk |
| `audio_end` | `{ "type": "audio_end" }` | TTS playback finished |
| `filler_audio` | `{ "type": "filler_audio", "data": "<base64>" }` | Thinking filler audio |
| `error` | `{ "type": "error", "message": "..." }` | Error message |
| `pong` | `{ "type": "pong" }` | Keep-alive response |

#### JavaScript Example

```javascript
const ws = new WebSocket('ws://localhost:8004/ws');

ws.onopen = () => {
  // Configure for text-only mode (no microphone)
  ws.send(JSON.stringify({ type: 'config', stt_mode: 'text', tts_mode: 'text' }));
};

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  
  switch(msg.type) {
    case 'session_start':
      console.log('Session:', msg.session_id);
      break;
    case 'agent_response':
      console.log('TARA:', msg.text);
      console.log('Sources:', msg.sources);
      break;
    case 'transcript':
      if (msg.is_final) console.log('You said:', msg.text);
      break;
    case 'audio_chunk':
      // Play PCM audio (base64 → ArrayBuffer → AudioContext)
      playAudioChunk(msg.data, msg.sample_rate);
      break;
  }
};

// Send a text query
ws.send(JSON.stringify({ type: 'text_input', text: 'What courses are available?' }));

// Send microphone audio (PCM f32le base64)
ws.send(JSON.stringify({ type: 'audio_chunk', data: base64AudioChunk }));

// Interrupt TTS playback
ws.send(JSON.stringify({ type: 'interrupt' }));

// End session
ws.send(JSON.stringify({ type: 'end_session' }));
```

---

## 3. Audio Stream WebSocket

### `WS /ws/audio`

Dedicated binary audio stream. Separates TTS audio from control messages to reduce stuttering.

```
ws://localhost:8004/ws/audio?session_id=<existing_session_id>
```

> **Note:** `session_id` is **required** and must match an existing `/ws` session.

This WebSocket only sends binary PCM audio frames (no JSON). Use alongside the main `/ws` for control messages.

---

## 4. RAG Query Endpoints

### `POST /api/v1/query`

Full RAG pipeline: retrieves context → generates LLM answer.

```bash
curl -X POST http://localhost:8004/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What skilling programs does TASK offer?",
    "context": {},
    "history_context": "",
    "language": "english",
    "tenant_id": "demo"
  }'
```

**Response:**
```json
{
  "answer": "TASK offers various skilling programs including...",
  "sources": ["task_brochure.pdf"],
  "confidence": 0.87,
  "timing_breakdown": {
    "total_ms": 1250,
    "retrieval_ms": 340,
    "generation_ms": 890
  },
  "cached": false,
  "metadata": {
    "method": "rag",
    "raw_chunks": [...]
  }
}
```

### `POST /api/v1/retrieve`

Retrieve context only (no LLM generation). Used by the HiveMind Top-K test widget.

```bash
curl -X POST http://localhost:8004/api/v1/retrieve \
  -H "Content-Type: application/json" \
  -d '{
    "query": "job placement services",
    "context": {},
    "history_context": "",
    "tenant_id": "demo"
  }'
```

**Response:**
```json
{
  "query_english": "job placement services",
  "original_language": "english",
  "relevant_docs": [
    {
      "text": "TASK provides placement drives...",
      "metadata": { "id": "abc-123", "category": "placements" },
      "similarity": 0.82,
      "boosted_similarity": 0.88
    }
  ],
  "agent_skills": [
    { "id": "sk-001", "text": "Always mention registration process", "score": 0.75, "doc_type": "Agent_Skill" }
  ],
  "agent_rules": [],
  "general_kb": [],
  "hive_mind_context": "...",
  "web_results": "",
  "timing": { "embedding_ms": 12, "search_ms": 85 },
  "fast_path_type": null,
  "history_context": ""
}
```

### `POST /api/v1/query/stream`

Server-Sent Events (SSE) stream of the RAG response.

```bash
curl -N http://localhost:8004/api/v1/query/stream \
  -H "Content-Type: application/json" \
  -d '{ "query": "Tell me about TASK", "tenant_id": "demo" }'
```

**Each SSE event:**
```json
{"text": "TASK is", "is_final": false}
{"text": " a government initiative", "is_final": false}
{"text": "...", "is_final": true}
```

---

## 5. HiveMind Knowledge Management

### `POST /api/v1/skills`

Create an agent skill or rule in the HiveMind (Qdrant).

```bash
curl -X POST http://localhost:8004/api/v1/skills \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Always greet users by their name when available",
    "type": "agent_skill",
    "topic": "greeting",
    "tenant_id": "demo"
  }'
```

**Response:**
```json
{
  "status": "created",
  "id": "uuid-here",
  "message": "Skill created successfully"
}
```

### `GET /api/v1/skills?tenant_id=demo`

List all skills and rules.

```bash
curl http://localhost:8004/api/v1/skills?tenant_id=demo
```

**Response:**
```json
{
  "skills": [
    { "id": "sk-001", "text": "Always greet users...", "type": "agent_skill", "topic": "greeting", "score": null }
  ],
  "rules": [
    { "id": "rl-001", "text": "Never share personal data", "type": "agent_rule", "topic": "privacy", "severity": "critical" }
  ],
  "total": 2
}
```

### `DELETE /api/v1/skills/{point_id}?tenant_id=demo`

Delete a specific skill/rule/chunk by its Qdrant point ID.

```bash
curl -X DELETE http://localhost:8004/api/v1/skills/abc-123?tenant_id=demo
```

**Response:**
```json
{
  "status": "deleted",
  "point_id": "abc-123"
}
```

### `POST /api/v1/upload`

Upload a document to the HiveMind knowledge base. (Multipart form)

```bash
curl -X POST http://localhost:8004/api/v1/upload \
  -F "file=@brochure.pdf" \
  -F "doc_type=General" \
  -F "topics=skilling,employment" \
  -F "tenant_id=demo"
```

### `POST /api/v1/save-case`

Save a resolved support case for collective learning.

```bash
curl -X POST http://localhost:8004/api/v1/save-case \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "+91-9876543210",
    "issue": "User could not register for course",
    "solution": "Guided through the mobile registration flow",
    "tenant_id": "demo"
  }'
```

---

## 6. HiveMind Visualization & Insights

### `GET /api/v1/hive-mind/visualize`

Fetch Hive Mind vectors projected to 2D for visualization.

| Param | Type | Default | Description |
|---|---|---|---|
| `algorithm` | string | `tsne` | `tsne` or `pca` |
| `limit` | int | `100` | Max points (capped at 500) |
| `tenant_id` | string | `demo` | Tenant filter |

```bash
curl 'http://localhost:8004/api/v1/hive-mind/visualize?algorithm=tsne&limit=100'
```

**Response:**
```json
{
  "points": [
    {
      "id": "abc-123",
      "x": 0.542,
      "y": -0.318,
      "text": "TASK offers placement drives...",
      "summary": "Placement services overview",
      "doc_type": "General_KB",
      "domain": "placements",
      "issue": null,
      "solution": null,
      "issue_type": null,
      "customer_segment": null
    }
  ],
  "collection_name": "tara_hive",
  "total_points": 100,
  "dimension": 384,
  "algorithm": "tsne"
}
```

### `GET /api/v1/hive-mind/insights`

Get trending domains, recent knowledge, and collection stats.

```bash
curl 'http://localhost:8004/api/v1/hive-mind/insights?limit=5'
```

**Response:**
```json
{
  "recent_knowledge": [
    {
      "id": "xyz-456",
      "text": "Registration fee is ₹500 + GST for SC/ST",
      "summary": "Fee structure",
      "doc_type": "General_KB",
      "domain": "registration"
    }
  ],
  "trending_domains": [
    { "domain": "registration", "count": 45, "percentage": 32.5 },
    { "domain": "placements", "count": 28, "percentage": 20.1 }
  ],
  "total_knowledge": 500,
  "unique_domains": 13,
  "customer_segments": { "developer": 12, "enterprise": 8 },
  "collection_name": "tara_hive"
}
```

---

## 7. HiveMind WebSocket (Real-time Updates)

### `WS /ws/hive-mind`

Live WebSocket for real-time knowledge additions. The Orchestrator proxies this to the RAG service's native WebSocket.

**Connection:**
```
ws://localhost:8004/ws/hive-mind
```

#### Server → Client Messages

```json
{
  "type": "new_knowledge",
  "node": {
    "id": "abc-123",
    "text": "New knowledge was added",
    "doc_type": "Case_Memory",
    "domain": "support"
  }
}
```

#### JavaScript Example

```javascript
const hiveMindWs = new WebSocket('ws://localhost:8004/ws/hive-mind');

hiveMindWs.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'new_knowledge') {
    console.log('New knowledge:', data.node);
    // Refresh your visualization
    reloadVisualization();
  }
};

hiveMindWs.onclose = () => {
  // Auto-reconnect after 5 seconds
  setTimeout(() => connectHiveMind(), 5000);
};
```

---

## 8. Session Management

### `GET /api/v1/sessions`

List all active WebSocket sessions.

```bash
curl http://localhost:8004/api/v1/sessions
```

### `GET /api/v1/sessions/{session_id}/metrics`

Turn-by-turn performance metrics (TTFT, TTFC).

```bash
curl http://localhost:8004/api/v1/sessions/my-session-123/metrics
```

### `GET /api/v1/sessions/{session_id}/history`

Full conversation history for a session.

```bash
curl http://localhost:8004/api/v1/sessions/my-session-123/history
```

### `POST /api/v1/sessions/{session_id}/summary`

Generate an AI summary of the conversation.

```bash
curl -X POST http://localhost:8004/api/v1/sessions/my-session-123/summary
```

### `POST /api/v1/analyze-session`

Post-session analysis with sentiment & business intelligence.

```bash
curl -X POST http://localhost:8004/api/v1/analyze-session \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "my-session-123",
    "history_context": [...],
    "tenant_id": "demo"
  }'
```

---

## 9. Visual Co-Pilot (TARA)

### `POST /api/v1/plan-step`

**Primary TARA planning endpoint.** Accepts DOM elements, mission goal, and returns the next action.

```bash
curl -X POST http://localhost:8004/api/v1/plan-step \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Navigate to the registration page",
    "dom_context": [
      { "tag": "a", "text": "Register", "id": "reg-link", "xpath": "//a[@id=\"reg-link\"]" }
    ],
    "session_id": "my-session-123",
    "current_url": "https://example.com",
    "completed_steps": [],
    "failed_attempts": []
  }'
```

### `POST /api/v1/fast-sense`

Quick DOM scan for immediate situational awareness.

```bash
curl -X POST http://localhost:8004/api/v1/fast-sense \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Find the login button",
    "dom_context": [...],
    "session_id": "my-session-123"
  }'
```

### `POST /api/v1/map-hints`

Fetch GPS navigation hints from HiveMind for a given goal (called once at mission start).

```bash
curl -X POST http://localhost:8004/api/v1/map-hints \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Complete user registration",
    "client_id": "demo",
    "current_url": "https://example.com"
  }'
```

### `POST /api/v1/check-domain`

Check if a domain is in Mapped or Explorer mode.

```bash
curl -X POST http://localhost:8004/api/v1/check-domain \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/dashboard",
    "client_id": "demo"
  }'
```

---

## 10. Phone (Twilio) Integration

### `POST /api/v1/phone/incoming`

Twilio webhook for incoming calls. Returns TwiML to start media streaming.

### `WS /api/v1/phone/audio/{call_sid}`

WebSocket for Twilio phone audio streaming (G.711 µ-law).

### `POST /api/v1/phone/status`

Twilio call status callback (completed, failed, etc.).

### `POST /api/v1/call/outgoing`

Initiate an outbound call.

```bash
curl -X POST http://localhost:8004/api/v1/call/outgoing \
  -H "Content-Type: application/json" \
  -d '{
    "to": "+919876543210",
    "from": "+14155551234"
  }'
```

---

## 11. Typing Stream (SSE)

### `GET/POST /api/v1/typing-stream`

Server-Sent Events stream that simulates typing with a configurable delay.

| Param | Type | Default | Description |
|---|---|---|---|
| `q` / `query` | string | — | The query text |
| `lang` | string | `en` | Response language |
| `delay` | float | `0.03` | Delay between characters (seconds) |

```bash
curl -N 'http://localhost:8004/api/v1/typing-stream?q=What+is+TASK&lang=en&delay=0.03'
```

---

## 12. Static Assets

| Path | Description |
|---|---|
| `GET /` | Redirects to the client page |
| `GET /client` | Browser-based voice client interface |
| `GET /hive-mind` | HiveMind visualization dashboard |
| `GET /static/tara-widget.js` | Embeddable TARA widget (cache-busted) |
| `GET /static/*` | Other static assets |

---

## 13. Frontend Integration Guide

### Minimal Voice Chat (Text Mode)

```html
<script>
  const ws = new WebSocket('ws://localhost:8004/ws');
  
  ws.onopen = () => {
    // Use text mode (no microphone needed)
    ws.send(JSON.stringify({ type: 'config', stt_mode: 'text', tts_mode: 'text' }));
  };
  
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'agent_response') {
      document.getElementById('response').textContent = msg.text;
    }
  };
  
  function sendQuery(text) {
    ws.send(JSON.stringify({ type: 'text_input', text }));
  }
</script>
```

### Voice Chat (Audio Mode)

```javascript
// 1. Connect
const ws = new WebSocket('ws://localhost:8004/ws');

ws.onopen = () => {
  ws.send(JSON.stringify({ type: 'config', stt_mode: 'audio', tts_mode: 'audio' }));
};

// 2. Capture microphone
const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
const ctx = new AudioContext({ sampleRate: 44100 });
const source = ctx.createMediaStreamSource(stream);
const processor = ctx.createScriptProcessor(4096, 1, 1);

source.connect(processor);
processor.connect(ctx.destination);

processor.onaudioprocess = (e) => {
  const pcm = e.inputBuffer.getChannelData(0);
  const base64 = arrayBufferToBase64(pcm.buffer);
  ws.send(JSON.stringify({ type: 'audio_chunk', data: base64 }));
};

// 3. Play TTS audio
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === 'audio_chunk') {
    const pcm = base64ToFloat32Array(msg.data);
    playPCM(pcm, msg.sample_rate || 44100);
  }
};

// 4. Interrupt (barge-in)
document.getElementById('stopBtn').onclick = () => {
  ws.send(JSON.stringify({ type: 'interrupt' }));
};
```

### Embed TARA Widget

```html
<!-- Drop-in widget for any website -->
<script src="http://localhost:8004/static/tara-widget.js"
        data-ws-url="ws://localhost:8004/ws"
        data-tenant-id="demo">
</script>
```

### HiveMind REST Integration

```javascript
// Fetch visualization data
const vizResp = await fetch('/api/v1/hive-mind/visualize?algorithm=tsne&limit=200');
const vizData = await vizResp.json();
// vizData.points → Array of { id, x, y, text, doc_type, ... }

// Fetch insights
const insightResp = await fetch('/api/v1/hive-mind/insights?limit=5');
const insights = await insightResp.json();
// insights.trending_domains, insights.recent_knowledge, etc.

// Top-K retrieval test
const retrieveResp = await fetch('/api/v1/retrieve', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ query: 'registration process', history_context: '', context: {} })
});
const chunks = await retrieveResp.json();

// Delete a chunk
await fetch(`/api/v1/skills/${chunkId}?tenant_id=demo`, { method: 'DELETE' });
```

---

## Service Architecture

```
┌──────────────────────────────────────────────────────┐
│                    FRONTEND                          │
│          (Browser / Widget / Mobile App)             │
└───────────┬──────────────┬───────────────────────────┘
            │              │
     WS /ws │     REST /api/v1/*
            │              │
┌───────────▼──────────────▼───────────────────────────┐
│              ORCHESTRATOR (:8004)                     │
│  • WebSocket handler (voice + text)                  │
│  • State machine (IDLE→LISTENING→THINKING→SPEAKING)  │
│  • Proxy layer for all /api/v1/* routes              │
│  • Session management                               │
│  • Twilio phone integration                          │
└──────┬────────────┬────────────┬─────────────────────┘
       │            │            │
  ┌────▼────┐ ┌─────▼─────┐ ┌───▼───┐
  │RAG:8003 │ │ STT:8002  │ │TTS:8000│
  │         │ │ (Sarvam)  │ │(Cartesia)
  │• Query  │ │• Audio→   │ │• Text→ │
  │• Retrieve│ │  Text    │ │  Audio │
  │• HiveMind│ │          │ │        │
  │• Skills │ └───────────┘ └────────┘
  │• Visualize│
  └──────┬──┘
         │
  ┌──────▼──────┐
  │Qdrant Cloud │
  │(HiveMind DB)│
  └─────────────┘
```

---

## Environment Variables

| Variable | Service | Default | Description |
|---|---|---|---|
| `LLM_API_KEY` | RAG | — | LLM provider API key |
| `LLM_MODEL` | RAG | `openai/gpt-oss-20b` | LLM model name |
| `QDRANT_URL` | RAG | `http://qdrant:6333` | Qdrant connection URL |
| `QDRANT_API_KEY` | RAG | — | Qdrant authentication key |
| `QDRANT_COLLECTION` | RAG | `tara_hive` | Collection name |
| `ENABLE_HIVE_MIND` | RAG | `true` | Enable HiveMind features |
| `ENABLE_LOCAL_RETRIEVAL` | RAG | `false` | Enable local FAISS index |
| `REDIS_URL` | ALL | `redis://redis:6379/0` | Redis connection |
| `CARTESIA_API_KEY` | TTS | — | Cartesia TTS API key |
| `SARVAM_API_KEY` | STT | — | Sarvam STT API key |
| `TENANT_ID` | ALL | `demo` | Default tenant identifier |
| `WIDGET_WS_URL` | Orchestrator | `ws://localhost:8004/ws` | Widget WebSocket URL |
| `PUBLIC_URL` | Orchestrator | `http://localhost:8004` | Public-facing URL |

---

*Generated: 2026-02-28 • TARA Platform by DaVinci AI*

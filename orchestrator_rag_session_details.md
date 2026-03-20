# TARA Orchestrator-EU, RAG-EU & Session Cleanup — Deep Dive

**Date**: 2026-03-20
**Branch**: bundb
**Focus**: Service architecture, session lifecycle, memory management

---

## Orchestrator-EU: Central Control Plane

### Architecture Overview

Orchestrator-EU is the **beating heart** of TARA — a FastAPI + WebSocket server that:
- Manages multi-service orchestration (STT → RAG → TTS)
- Maintains conversation state machine (FSM)
- Handles user interruptions (barge-in)
- Applies conversation policy (sales vs clinical modes)
- Synchronizes TTS playback with action execution

### Core Components

#### 1. WebSocket Handler (`core/ws_handler.py`)

**State Machine: LISTENING → THINKING → SPEAKING → EXECUTING**

```
┌─────────────────────────────────────────────────────────┐
│                    LISTENING                             │
│  Waiting for user speech via STT WebSocket               │
│  When audio received → parse STT result                  │
└──────────────────┬──────────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────────┐
│                    THINKING                              │
│  Process query through RAG service                       │
│  Execute RAG streaming endpoint                          │
│  Assemble response tokens                                │
└──────────────────┬──────────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────────┐
│                    SPEAKING                              │
│  Synthesize response via TTS                             │
│  Stream audio back to client                             │
│  Wait for TTS completion (SYNC RULE)                     │
└──────────────────┬──────────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────────┐
│                    EXECUTING                             │
│  Execute visual actions (DOM clicks, navigation)         │
│  Update page state                                       │
│  Return to LISTENING                                     │
└─────────────────────────────────────────────────────────┘
```

**Key Methods**:

- `handle_session_open()`: Initialize session, create session_id, setup context
- `handle_text_result()`: Process STT transcription + metadata
- `evaluate_turn()`: Apply conversation policy (get policy_mode decision)
- `_execute_next_step()`: Dispatch action to RAG or visual executor
- `handle_interruption()`: Cancel current response, restart with new query
- `cleanup_session()`: Remove session from Redis, free resources

**Barge-in (Interruption) Flow**:

```
User speaks while TARA is in SPEAKING/EXECUTING:
  1. STT detects new audio chunk
  2. ws_handler recognizes interruption
  3. Cancel current TTS stream: reset_buffer()
  4. Save interrupted_text (what TARA was saying)
  5. Collect interruption_transcripts (user's new speech)
  6. Determine interruption_type: 'addon', 'topic_change', 'clarification', 'noise'
  7. Pass context to pipeline: { interrupted_text, interruption_transcripts, interruption_type }
  8. Restart FSM from THINKING with new query
```

#### 2. Processing Pipeline (`core/pipeline.py`)

Handles the **STT → Detection → RAG → TTS** journey.

**`process_query()` Signature**:

```python
async def process_query(
    query: str,
    session_id: str,
    stt_metadata: Optional[Dict[str, Any]] = None,  # Language hints from STT
    language: Optional[str] = None,
    history_context: Optional[str] = None,  # Conversation history
    session_summary_window: Optional[str] = None,  # Layered summary
    session_summary_revision: int = 0,
    recent_turns: Optional[List[Dict[str, Any]]] = None,  # Minimal raw turns
    form_data: Optional[Dict[str, Any]] = None,  # Appointment booking slots
    tenant_id: Optional[str] = None,  # bundb, davinci, etc.
    interrupted_text: Optional[str] = None,  # What TARA was saying
    interruption_transcripts: Optional[List[str]] = None,  # User's interruption
    interruption_type: Optional[str] = None,  # addon, topic_change, etc.
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Yields: { 'token', 'language', 'is_final', 'action', 'confidence', ... }
    """
```

**Step-by-step**:

1. **Language Detection** (if not provided):
   ```python
   # Try metadata first (STT may have language hints)
   detected_lang = detect_language_from_metadata(stt_metadata)
   if not detected_lang:
       # Fall back to text-based detection
       detected_lang = detect_language(query, self.supported_languages)
   if not detected_lang:
       detected_lang = "de"  # Final fallback to German
   ```

2. **Intent Classification** (optional):
   ```python
   if self.intent_client.enabled:
       intent = await self.intent_client.classify(query)
       # Could trigger special behavior: appointment booking, fallback, escalation
   ```

3. **RAG Query with Streaming**:
   ```python
   async for token_batch in self.rag_client.stream_query(
       query=query,
       language=language,
       history=history_context,
       session_summary_window=session_summary_window,
       recent_turns=recent_turns,
       form_data=form_data,
       tenant_id=tenant_id,
       interrupted_text=interrupted_text,
       interruption_transcripts=interruption_transcripts,
       interruption_type=interruption_type,
   ):
       yield token_batch
   ```

4. **Metrics & Logging**:
   ```python
   elapsed = time.time() - start_time
   logger.info(f"[{session_id}] Query processed in {elapsed:.2f}s")
   ```

#### 3. Conversation Policy (`core/conversation_policy.py`)

**Lightweight, heuristic-first policy layer** for stateful conversation decisions.

**`evaluate_turn()` Method**:

```python
@staticmethod
def evaluate_turn(
    previous_context: Optional[ConversationPolicyDecision],
    tenant_id: str,
    session_id: str,
    turn_id: str,
    query: str,
    policy_mode_default: str = "sales",
) -> ConversationPolicyDecision:
    """
    Lightweight heuristic evaluation (NOT LLM-based for latency).

    Returns:
        ConversationPolicyDecision with:
        - policy_mode: "sales", "clinical", "rapport", "deep_dive"
        - conversation_stage: "opening", "intake", "hypothesis", "refinement", "closing"
        - response_act: "ask", "answer", "clarify", "confirm"
        - hypotheses: List of generated hypotheses (clinical mode)
        - ranked_differentials: Scored hypotheses
        - missing_slots: Info still needed
        - confirmed_dx / ruled_out_dx: Diagnosis tracking
    """
```

**Policy Mode Routing**:

| Mode | Use Case | Behavior |
|------|----------|----------|
| **sales** | Default, lead nurturing | Yes-ladder (small yeses → big yes) |
| **clinical** | Structured intake | Hypothesis generation, ranking, discriminating questions |
| **rapport** | Relationship building | Empathy-first, minimal asking |
| **deep_dive** | Investigative research | Multi-turn hypothesis refinement |

**Clinical Mode Example**:

```python
# User: "Wir haben Markenproblem"
# TARA evaluates:
# 1. Generate 5-7 hypotheses
hypothesis_candidates = [
    "Internal brand inconsistency (team doesn't embody it)",
    "External messaging gap (clients confused)",
    "Visual identity mismatch",
    "Competitive differentiation unclear",
    "Brand strategy exists but unenforced",
]

# 2. Rank by danger + discriminating power
ranked = [
    {"dx": "Internal inconsistency", "danger": 4, "discriminating_power": 0.9},
    {"dx": "External messaging", "danger": 5, "discriminating_power": 0.85},
    {"dx": "Visual mismatch", "danger": 2, "discriminating_power": 0.5},
]

# 3. Return most discriminating question
response_act = "ask"
question = "Sind Ihre Kunden verwirrt, was Sie tun? Oder sind Ihre Teams inkonsistent?"
```

#### 4. Service Client (`core/service_client.py`)

**RAGClient**: Communicates with RAG service via HTTP streaming.

```python
class RAGClient:
    async def stream_query(
        self,
        query: str,
        language: str,
        history: Optional[str],
        session_summary_window: Optional[str],
        recent_turns: Optional[List[Dict]],
        form_data: Optional[Dict],
        tenant_id: Optional[str],
        interrupted_text: Optional[str],
        interruption_transcripts: Optional[List[str]],
        interruption_type: Optional[str],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        POST /api/v1/stream_query with QueryRequest
        Yields token batches with streaming_mode='continuous'
        """
        payload = QueryRequest(
            query=query,
            language=language,
            history_context=history,
            session_summary_window=session_summary_window,
            recent_turns=recent_turns,
            form_data=form_data,
            tenant_id=tenant_id,
            interrupted_text=interrupted_text,
            interruption_transcripts=interruption_transcripts,
            interruption_type=interruption_type,
        )
```

**TTSClient**: Manages TTS WebSocket with **buffer reset optimization**.

```python
class TTSClient:
    async def reset_buffer(self):
        """
        Clear chunk buffer + cancel pending flush WITHOUT closing WebSocket.

        BEFORE: abort_stream() closed socket → forced reconnect (~1946ms TTFC)
        AFTER: reset_buffer() keeps connection alive → ~800ms TTFC

        Implementation:
        - Clear self._chunk_buffer = []
        - Cancel self._flush_task if pending
        - Keep WebSocket open
        """
```

#### 5. Session Summary Manager (`core/session_summary_manager.py`)

Manages **layered conversation windows** to prevent context bloat.

**Problem**: 100-turn conversation = 50KB history = token overflow

**Solution**:

```python
class SessionSummaryManager:
    def update_session_window(
        self,
        session_id: str,
        new_turn: Dict[str, Any],
        max_tokens: int = 1500,  # Target window size
    ) -> str:
        """
        Maintains a "sliding summary window" of recent conversation.

        Layers:
        1. Raw turns (recent 3): Preserve exact phrasing
        2. Summary (older turns): Compressed representation
        3. Key facts: Extracted entities, decisions, hypotheses

        Returns: Concise session_summary_window string
        """

        # Add new turn
        self.turns[session_id].append(new_turn)

        # If total exceeds max_tokens:
        #   - Keep recent 3 turns as-is
        #   - Compress older turns into summary
        #   - Bump revision counter

        return self._render_window(session_id)
```

---

## RAG-EU: Intelligence & Reasoning Engine

### Architecture Overview

RAG-EU is the **brain** — handles:
- Document retrieval from vector DB
- Prompt assembly with context architecture
- LLM reasoning (Groq, OpenRouter, Claude, etc.)
- Streaming response generation
- Policy-driven response tuning

### Core Components

#### 1. FastAPI App (`app.py`)

**Endpoints**:

- `POST /api/v1/query` — Single synchronous query
- `POST /api/v1/stream_query` — Streaming response (default)
- `GET /api/v1/health` — Health check
- `POST /api/v1/upload_kb` — Knowledge base upload
- `WebSocket /ws` — Real-time streaming (beta)

**QueryRequest Model**:

```python
@dataclass
class QueryRequest:
    query: str  # User's question
    context: Optional[Dict[str, Any]] = None  # From intent service
    language: Optional[str] = "german"  # Response language
    tenant_id: Optional[str] = "tara"  # Multi-tenant isolation
    session_id: Optional[str] = None  # Session tracking

    # Conversation context
    history_context: Optional[Union[str, List[Dict]]] = None
    session_summary_window: Optional[str] = None
    session_summary_revision: int = 0
    recent_turns: Optional[List[Dict]] = None

    # Form data (appointment booking, etc.)
    form_data: Optional[Dict[str, Any]] = None

    # Interruption handling
    interrupted_text: Optional[str] = None
    interruption_transcripts: Optional[List[str]] = None
    interruption_type: Optional[str] = None  # addon, topic_change, clarification
```

**Processing Flow**:

```python
@app.post("/api/v1/stream_query")
async def stream_query(request: QueryRequest):
    # 1. Resolve tenant ID
    tenant_id = _resolve_effective_tenant_id(request.tenant_id)

    # 2. Check cache (hash query + context)
    cache_key = f"{tenant_id}:{hashlib.md5(request.query.encode()).hexdigest()}"
    cached = await redis_client.get(cache_key)
    if cached:
        return StreamingResponse(iter([cached]), media_type="application/x-ndjson")

    # 3. Retrieve documents
    docs = await rag_engine.retrieve(request.query, tenant_id)

    # 4. Assemble prompt (context architecture)
    prompt = context_architect.assemble_prompt(
        query=request.query,
        retrieved_docs=docs,
        history=request.history_context,
        language=request.language,
        policy_mode=request.context.get("policy_mode", "sales"),
    )

    # 5. Stream LLM response
    full_answer = ""
    async for token in llm_provider.stream(prompt):
        full_answer += token
        yield {"token": token, "is_final": False}

    # 6. Cache result
    await redis_client.setex(cache_key, 3600, full_answer)

    # 7. Write to hivemind (optional)
    if config.enable_hivemind_write:
        await hivemind_service.upsert(
            tenant_id=tenant_id,
            session_id=request.session_id,
            query=request.query,
            response=full_answer,
            policy_mode=request.context.get("policy_mode"),
        )
```

#### 2. Configuration (`config.py`)

**Multi-Provider LLM Support**:

```python
@dataclass
class RAGConfig:
    # LLM Provider (replaces Gemini-specific config)
    llm_provider: str = "groq"  # gemini, openai, openrouter, claude, ollama, groq
    llm_api_key: str = ""
    llm_model: str = "llama-3.1-70b-versatile"

    # Specialized models
    hivemind_llm_model: Optional[str] = None  # Dashboard queries
    analytics_model: str = "qwen/qwen3-32b"  # Reasoning tasks

    # Vector store
    vector_store_path: str = "/app/index"
    embedding_model_name: str = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dimension: int = 384

    # Retrieval
    top_k: int = 8  # Candidates before filtering
    top_n: int = 5  # Final docs after filtering
    similarity_threshold: float = 0.3

    # Chunking
    chunk_size_min: int = 500
    chunk_size_max: int = 800
    chunk_overlap: int = 100

    # Response
    response_style: str = "friendly_casual"
    max_response_length: int = 450
    enable_humanization: bool = True
    timeout: float = 30.0

    # Caching
    cache_ttl: int = 3600  # Redis cache (seconds)

    # Search
    enable_hybrid_search: bool = True
    enable_web_search: bool = False
```

#### 3. RAG Engine (`rag_engine.py`)

**Retrieval + Reasoning Pipeline**:

```python
class RAGEngine:
    async def retrieve(
        self,
        query: str,
        tenant_id: str,
        top_k: int = 8,
    ) -> List[Dict[str, Any]]:
        """
        1. Embed query using multilingual embeddings
        2. Search FAISS/Qdrant
        3. Apply similarity filtering
        4. Return ranked documents with relevance scores
        """

    async def reason(
        self,
        query: str,
        retrieved_docs: List[Dict],
        history: Optional[str],
        policy_mode: str = "sales",
    ) -> AsyncGenerator[str, None]:
        """
        1. Assemble prompt using context_architecture
        2. Inject policy mode (compact sales vs full clinical)
        3. Stream LLM response
        4. Apply humanization + TTS-safe transformations
        """
```

**Policy Snapshot**:

```python
# Before calling LLM, build policy context
policy_snapshot = {
    "policy_mode": policy_mode or policy_mode_default,  # Fallback to compact
    "conversation_stage": context.get("conversation_stage", "general"),
    "response_act": context.get("response_act", "answer"),
    "hypotheses": context.get("hypotheses", [])[:3],  # Top 3 only
    "missing_slots": context.get("missing_slots", []),
}

# This keeps prompt compact even in clinical mode
# Sales mode: ~400 tokens total
# Clinical mode: ~600 tokens total
```

**Streaming with Callbacks**:

```python
def streaming_callback(token: str):
    """
    Called for each LLM token.

    CRITICAL: Check `if streaming_callback and text:` to avoid
    sending empty tokens that corrupt partial word assembly.
    """
    if streaming_callback and token:  # Guard against empty text
        asyncio.create_task(streaming_callback(token))
```

#### 4. Context Architecture (`context_architecture_bundb.py`)

**Prompt Assembly Framework** — Token-efficient, cache-optimized.

**Zones**:

```
ZONE A (Static, ~1050 tokens) — Persona + Rules
┌─────────────────────────────────────────────────────┐
│ # Persona: TARA, B&B. brand consulting colleague     │
│ # Agency knowledge: BLAIQ, differentiation           │
│ # Constraints: No invented facts, German-first       │
│ # Tone: Natural, consultant energy                   │
└─────────────────────────────────────────────────────┘

ZONE B (Static, ~320 tokens) — Examples
┌─────────────────────────────────────────────────────┐
│ ## Example 1: Sales mode (yes-ladder pattern)        │
│ ## Example 2: Clinical mode (hypothesis generation)  │
│ ## Example 3: Rapport mode (empathy first)           │
│ ## Example 4: Deep-dive mode (research-oriented)     │
└─────────────────────────────────────────────────────┘

ZONE C (Dynamic, ~100 tokens) — Context
┌─────────────────────────────────────────────────────┐
│ ## Conversation History (3 recent turns)             │
│ ## Retrieved Documents (2×900 chars)                 │
│ ## User Query                                        │
└─────────────────────────────────────────────────────┘

ZONE D (Dynamic, ~0-150 tokens) — Optional
┌─────────────────────────────────────────────────────┐
│ ## Policy Rules (if policy_mode != sales)            │
│ ## Skills (if form_data present)                     │
│ ## Interruption Context (if barge-in)                │
└─────────────────────────────────────────────────────┘

BASELINE: ~1,470 tokens
Groq caching: ~1,370 tokens cached from turn 2 onward
```

**Method Signature**:

```python
class ContextArchitect:
    @staticmethod
    def assemble_prompt(
        query: str,
        raw_query: str,
        retrieved_docs: List[Dict],
        history: Optional[str],
        hive_mind: Optional[Dict],
        user_profile: Optional[Dict],
        agent_skills: Optional[List[str]],
        agent_rules: Optional[List[str]],
        policy_mode: str = "sales",
        language: str = "german",
    ) -> str:
        """
        Returns assembled prompt ready for LLM.

        Groq caching hint: Zones A+B are static, cache-eligible.
        Only C+D vary per query.
        """
```

---

## Session Lifecycle & Cleanup

### Session Creation

When user opens WebSocket connection:

```python
# orchestrator-eu/core/ws_handler.py
async def handle_session_open(websocket: WebSocket, tenant_id: str):
    # 1. Generate session_id (UUID)
    session_id = str(uuid.uuid4())

    # 2. Initialize session state
    session_state = {
        "session_id": session_id,
        "tenant_id": tenant_id,
        "created_at": datetime.utcnow().isoformat(),
        "state": "LISTENING",
        "turn_count": 0,
        "last_activity": datetime.utcnow().isoformat(),
        "action_history": [],  # CRITICAL: Prevents click loops
        "conversation_stage": "opening",
        "policy_mode": "sales",
        "hypotheses": [],  # For clinical mode
        "confirmed_dx": [],
        "ruled_out_dx": [],
    }

    # 3. Store in Redis
    await redis_client.hset(
        f"session:{session_id}",
        mapping=session_state,
        ex=3600,  # 1 hour TTL
    )

    # 4. Track in sessions map
    self.active_sessions[session_id] = websocket

    # 5. Send welcome message
    await websocket.send_json({
        "type": "session_init",
        "session_id": session_id,
        "tenant_id": tenant_id,
        "language": "de",  # Default German
        "message": "Session initialized",
    })
```

### Session Activity Tracking

Each turn increments activity:

```python
async def handle_text_result(session_id: str, transcription: str):
    # Update last_activity + turn_count
    await redis_client.hset(
        f"session:{session_id}",
        mapping={
            "last_activity": datetime.utcnow().isoformat(),
            "turn_count": redis_client.hincrby(f"session:{session_id}", "turn_count", 1),
        }
    )

    # Also extend session TTL (keep alive if active)
    await redis_client.expire(f"session:{session_id}", 3600)
```

### Session Cleanup

**Triggered by**:
1. **User closes WebSocket** (explicit disconnect)
2. **Inactivity timeout** (3600 seconds)
3. **Error recovery** (failed service calls)
4. **Server shutdown** (graceful drain)

**Cleanup Handler**:

```python
async def cleanup_session(session_id: str, reason: str = "normal"):
    """
    Full session teardown.

    Args:
        session_id: Session to cleanup
        reason: "normal", "timeout", "error", "shutdown"
    """

    logger.info(f"[{session_id}] Cleaning up session (reason: {reason})")

    # 1. Stop active operations
    if session_id in self.active_sessions:
        try:
            websocket = self.active_sessions[session_id]
            await websocket.close(code=1000, reason=reason)
        except Exception as e:
            logger.warning(f"[{session_id}] Error closing WebSocket: {e}")

    # 2. Abort TTS stream
    await self.tts_client.abort_stream(session_id)

    # 3. Clear Redis session data
    await redis_client.delete(f"session:{session_id}")
    await redis_client.delete(f"session:{session_id}:history")
    await redis_client.delete(f"session:{session_id}:summary_window")

    # 4. Remove from active sessions map
    if session_id in self.active_sessions:
        del self.active_sessions[session_id]

    # 5. Optional: Archive to hivemind
    if reason != "timeout":  # Only archive on normal close or error
        await self._archive_session_to_hivemind(session_id)

    # 6. Log metrics
    session_stats = {
        "session_id": session_id,
        "turn_count": await redis_client.hget(f"session:{session_id}", "turn_count"),
        "duration_seconds": (datetime.utcnow() - session_created_at).total_seconds(),
        "cleanup_reason": reason,
    }
    logger.info(f"Session stats: {session_stats}")
```

### Memory Management

**Redis Memory Strategy**:

```python
# Session data TTL (1 hour)
await redis_client.setex(f"session:{session_id}", 3600, session_data)

# Conversation history (6 hour rolling window, then summarized)
history_key = f"session:{session_id}:history"
await redis_client.lpush(history_key, turn_json)  # Left push (newest first)
await redis_client.ltrim(history_key, 0, 99)  # Keep last 100 turns
await redis_client.expire(history_key, 21600)  # 6 hours

# Session summary window (permanent until cleanup)
summary_key = f"session:{session_id}:summary_window"
await redis_client.set(summary_key, summary_text)
await redis_client.expire(summary_key, 3600)

# Cache (3600 seconds)
cache_key = f"{tenant_id}:query:{query_hash}"
await redis_client.setex(cache_key, 3600, response)
```

### Inactivity Timeout

Background task monitors & cleans up idle sessions:

```python
# In Orchestrator startup
@asyncio.task
async def inactivity_monitor():
    while True:
        await asyncio.sleep(60)  # Check every 60 seconds

        # Get all active sessions
        cursor = 0
        while True:
            cursor, keys = await redis_client.scan(
                cursor,
                match="session:*",
                count=100,
            )

            for key in keys:
                session_data = await redis_client.hgetall(key)
                last_activity = datetime.fromisoformat(session_data["last_activity"])
                age = datetime.utcnow() - last_activity

                if age.total_seconds() > 3600:  # 1 hour idle
                    session_id = key.split(":")[1]
                    await cleanup_session(session_id, reason="timeout")

            if cursor == 0:
                break

        await asyncio.sleep(300)  # Recheck in 5 mins
```

### Error Recovery

If RAG/TTS service fails:

```python
async def handle_service_error(session_id: str, service: str, error: Exception):
    """
    Graceful degradation on service failure.
    """

    logger.error(f"[{session_id}] {service} error: {error}")

    if service == "rag":
        # Fall back to pre-recorded message
        fallback_response = "Entschuldigung, es gibt ein technisches Problem. Können Sie die Frage wiederholen?"
    elif service == "tts":
        # Return text-only response
        fallback_response = query_response

    # Send to client
    await websocket.send_json({
        "type": "error",
        "error_type": f"{service}_error",
        "message": fallback_response,
        "can_retry": True,
    })

    # If critical error, cleanup
    if error.status_code >= 500:
        await cleanup_session(session_id, reason="error")
```

---

## Integration Points

### Orchestrator → RAG Communication

```
POST /api/v1/stream_query

Headers:
  Authorization: Bearer {RAG_API_KEY}
  Content-Type: application/json

Body: QueryRequest {
  query: "Frage...",
  language: "german",
  tenant_id: "bundb",
  session_id: "uuid",
  history_context: "...",
  session_summary_window: "...",
  session_summary_revision: 5,
  context: {
    policy_mode: "sales",
    conversation_stage: "intake",
    hypotheses: [...],
  }
}

Response: Stream of NDJSON
  {"token": "Ich", "is_final": false}
  {"token": " bin", "is_final": false}
  {"token": " TARA", "is_final": false}
  ...
  {"token": ".", "is_final": true, "action": "click", "xpath": "..."}
```

### RAG → LLM Providers

```
Groq LLM (Default):
  POST https://api.groq.com/openai/v1/chat/completions

OpenRouter:
  POST https://openrouter.ai/api/v1/chat/completions

Claude:
  POST https://api.anthropic.com/v1/messages

All providers:
- Support streaming (SSE or JSON-streaming)
- Token counting via tiktoken
- Retry logic with exponential backoff
```

---

## Monitoring & Debugging

### Key Metrics

```python
metrics = {
    "total_sessions": len(active_sessions),
    "avg_turn_latency_ms": np.mean(turn_latencies),
    "cache_hit_rate": cache_hits / (cache_hits + cache_misses),
    "rag_error_rate": rag_errors / rag_requests,
    "tts_ttfc_ms": tts_time_to_first_chunk,
    "policy_mode_distribution": {
        "sales": count,
        "clinical": count,
        "rapport": count,
    }
}
```

### Logs

```
[session-uuid] LISTENING
[session-uuid] Received STT: "Frage..."
[session-uuid] Detected language: german
[session-uuid] → THINKING
[session-uuid] RAG query (tenant: bundb, policy: sales)
[session-uuid] Retrieved 5 documents, assembled 1470 token prompt
[session-uuid] → SPEAKING (TTS synthesis started)
[session-uuid] Token assembly: 25 tokens, 450 characters
[session-uuid] TTS complete (800ms)
[session-uuid] → EXECUTING (click action, dom_xpath: "...")
[session-uuid] → LISTENING
[session-uuid] Cleanup: turn_count=3, duration=45.2s, reason=normal
```

---

## Best Practices

### For Orchestrator Development
1. **Always await TTS completion** before state transitions
2. **Reset TTS buffer, don't reconnect** (use `reset_buffer()`)
3. **Track action history** to prevent click loops
4. **Update session TTL** on every activity
5. **Archive sessions to hivemind** before cleanup (optional but recommended)

### For RAG Development
1. **Check cache before retrieving** documents
2. **Validate policy_mode** before assembling prompt
3. **Stream tokens only if non-empty** (guard `if token:`)
4. **Inject policy snapshot** for compact reasoning
5. **Skip Groq translation** for sales/clinical modes

### For Session Management
1. **Keep session TTL at 3600s** (1 hour)
2. **Extend TTL on every user activity**
3. **Inactivity monitor every 60s** (check every 5 mins for old sessions)
4. **Archive before cleanup** (preserve knowledge for analytics)
5. **Monitor Redis memory** (use MEMORY STATS)

---

*Document Version: 1.0 | Last Updated: 2026-03-20 | Purpose: Deep technical reference*

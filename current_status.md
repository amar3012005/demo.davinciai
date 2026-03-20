# TARA Project — Current Status Report
**Date**: 2026-03-20
**Branch**: `bundb`
**Project**: Dual-Stream AI Visual Co-Pilot for Expressive Co-Browsing

---

## Executive Summary

TARA is a **production-grade conversational AI system** featuring:
- **Dual-stream architecture**: Fast voice acknowledgment (Stream A) + intelligent planning (Stream B)
- **Multi-language support**: German (primary) + English fallback
- **Brand-aware agents**: B&B. real estate consulting, strategic business intake
- **Clinical reasoning**: Hypothetico-deductive structured decision-making
- **Expressive narration**: TARA tells you what she's doing before acting

**Current Status**: Actively developing `bundb` branch with strategic enhancements for B&B. brand voice and clinical reasoning modes.

---

## System Architecture

### Services Overview

| Service | Port | Function | Technology |
|---------|------|----------|------------|
| **Orchestrator-eu** | 8004 (ext: 8030) | WebSocket handler, FSM, session management | FastAPI, Websockets |
| **RAG-eu** | 8003 (ext: 8031) | Intelligence, retrieval, planning | FastAPI, FAISS/Qdrant, Groq |
| **STT (stt_groq_whisper)** | 8002 (ext: 8032) | Speech-to-Text | Groq Whisper API |
| **TTS (tts_cartesia)** | 8000 (ext: 8033) | Text-to-Speech | Cartesia Sonic-3 |
| **Redis** | 6379 | State & cache | Redis 7 |

### Dual-Stream Architecture

```
User Voice Input
    ↓
   STT (Groq Whisper)
    ↓ (metadata: language)
    ├─→ [STREAM A — FAST RESPONSE]
    │   └─→ Language Detection
    │       └─→ Groq Llama 8B (quick acknowledgment)
    │           └─→ TTS (Cartesia Sonic-3)
    │               └─→ Voice Output (~500ms latency)
    │
    └─→ [STREAM B — INTELLIGENT PLANNING]
        └─→ RAG Service
            └─→ Domain reasoning (Llama 70B or Qwen 32B)
            └─→ Structured decision-making
            └─→ Contextual response assembly
                └─→ TTS synthesis
                    └─→ Full response (~2-5s)
```

**Design Philosophy**: TARA narrates her intent, then acts. No silent robots.

---

## Service Details

### 1. Orchestrator-eu (`/Users/amar/demo.davinciai/Orchestrator-eu`)

**Purpose**: Central control plane for multi-service orchestration
**Framework**: FastAPI + WebSockets + Redis

**Key Components**:

#### `app.py`
- FastAPI bootstrap with multi-service coordination
- Kubernetes integration (CRD support for multi-tenant deployments)
- Phone integration (Twilio VoiceResponse)
- Static file serving (client_bundb.html)
- Rate limiting middleware

#### `config_loader.py`
- YAML-based configuration with env var overrides
- Dataclass-driven settings (ServerConfig, STTConfig, TTSConfig, RAGConfig, RedisConfig)
- Policy mode support: `sales` (default), `clinical` (structured intake), custom

#### `core/ws_handler.py` (WebSocket FSM)
- **State Machine**: LISTENING → THINKING → SPEAKING → EXECUTING
- **Barge-in support**: User can interrupt TARA mid-speech
- **Action history**: Prevents infinite click loops
- **Policy evaluation**: Multi-turn context awareness
- **TTS synchronization**: Await TTS completion before next step

#### `core/pipeline.py`
- **Language detection**: Text + metadata-based detection
- **Intent classification**: Optional external intent service
- **RAG query**: Streaming response generation
- **Error handling**: Graceful fallbacks for service failures

#### `core/conversation_policy.py`
- Lightweight policy layer (intentionally heuristic-first for latency)
- **Clinical mode**: Hypothetico-deductive reasoning for structured intake
- **Sales mode**: Yes-ladder conversion (small yeses → shared vision → big yes)
- **Stateful evaluation**: Track conversation stage, hypotheses, missing slots
- **Differential diagnosis ranking**: For clinical reasoning (confirmed/ruled-out)

#### `core/session_summary_manager.py` (NEW)
- Layered session window management
- Prevents token bloat in long conversations
- Revision tracking for consistency

#### `core/service_client.py`
- **RAGClient**: Streaming query interface
- **TTSClient**: WebSocket synthesis with buffer reset optimization
- `reset_buffer()` method: Clears chunk buffer without reconnect (~1946ms → ~800ms TTFC)

#### `Orchestrator-eu/journal.md`
- Development diary with recent fixes and features
- March 2026: Pipeline optimization, TTS reconnect reduction, clinical reasoning implementation

### 2. RAG-eu (`/Users/amar/demo.davinciai/rag-eu`)

**Purpose**: Knowledge retrieval, reasoning, and response generation
**Framework**: FastAPI, FAISS/Qdrant, Groq/OpenRouter

**Key Components**:

#### `app.py`
- **QueryRequest**: User query + session context + language
- **Tenant resolution**: Multi-tenant isolation (bundb, davinci, demo, techz)
- **Streaming callbacks**: Token-by-token response generation
- **Redis caching**: Query hash-based caching with TTL
- **Hibemind schema**: Integration with knowledge base

#### `config.py`
- **Multi-provider LLM support**: Groq, OpenAI, OpenRouter, Claude, Ollama, Gemini
- **Vector store**: Xenova/paraphrase-multilingual-MiniLM-L12-v2 (384-dim embeddings)
- **Chunking**: 500-800 char chunks, 100 char overlap
- **Retrieval**: Top-K 8, top-N 5, 0.3 similarity threshold
- **Response**: Max 450 chars, friendly_casual style, humanization enabled

#### `context_architecture/` (Prompt Assembly Framework)

**context_architecture_bundb.py** (v6 — B&B. Brand Voice)
- **Deep agency knowledge**: B&B. services, BLAIQ platform, differentiation
- **Proactive questioning**: Strategic consultant energy, not receptionist
- **Yes-ladder pattern**: Small yeses → confirmed pain → shared vision → big yes
- **Token budget**: ~1,470 baseline (Groq caches ~1,370 from turn 2)
  - Zone A: Static persona + rules (~1050 tok)
  - Zone B: 4 seed examples (~320 tok)
  - Zone C: Dynamic history + docs + query (~100 tok)
  - Zone D: Skills/rules (~0-150 tok)
- **TTS-safe**: German pronunciation handling, loanword mapping

**context_architecture_bundb_clinical.py** (NEW — Clinical Intake)
- **Hypothetico-deductive reasoning**: Generate hypotheses, rank by danger/urgency
- **Structured questions**: Ask the most discriminating question first
- **Reflection loop**: Interpret user response before proposing hypothesis
- **Brand consulting context**: "Clinical" = structured business intake (same methodology)
- **Differential ranking**: Confirmed diagnoses, ruled-out differentials

#### `rag_engine.py`
- **Retrieval pipeline**: Hybrid search, pattern detection, fallback strategies
- **Streaming interface**: Token generation via callbacks
- **Policy snapshot**: Fallback to `policy_mode_default` (compact sales prompt)
- **Groq translation**: Skipped for sales/clinical modes (saves ~200ms)
- **Qdrant timeout**: Reduced from 2.5s → 1.0s for non-dashboard queries

#### `visual_copilot/` (Visual Reasoning)
- **semantic_detective.py**: DOM element extraction and reasoning
- **last_mile.py**: Final response refinement
- **mission_planner.py**: Action sequencing
- **memory/**: HiveService (persistent knowledge), LiveGraphService, CacheService

#### `tests/` (Comprehensive Test Coverage)
- `test_rag_engine.py`: Retrieval and response generation
- `test_semantic_detective_v3.py`: Visual DOM analysis
- `test_last_mile_reasoning.py`: Response refinement
- `test_decision_route_v3.py`: Policy routing

### 3. STT — stt_groq_whisper (`/Users/amar/demo.davinciai/stt_groq_whisper`)

**Purpose**: Speech-to-Text transcription with language detection
**API**: Groq Whisper (whisper-large-v3)

**Key Components**:

#### `app.py`
- WebSocket endpoint: `/ws`
- Audio chunk buffering (16-bit PCM)
- Streaming transcription response
- Language metadata extraction

#### `groq_client.py`
- Groq API client wrapper
- Audio format handling (WAV, PCM)
- Retry logic with exponential backoff

#### `session_manager.py` (MODIFIED)
- Session state tracking
- Language storage per session
- Cleanup on disconnect

#### `config.py`
- Groq API credentials
- Supported languages: German, English
- Sample rate: 16000 Hz

### 4. TTS — tts_cartesia (`/Users/amar/demo.davinciai/tts_cartesia`)

**Purpose**: Text-to-Speech synthesis with expressive narration
**Provider**: Cartesia Sonic-3 (Voice ID: f786b574-daa5-4673-aa0c-cbe3e8534c02)

**Key Components**:

#### `app.py`
- WebSocket endpoint: `/ws`
- Audio chunking and streaming
- Language-specific voice selection

#### `cartesia_manager.py`
- Cartesia API integration
- Voice configuration per language
- Chunk assembly and buffering
- Audio quality optimization

#### `config.py`
- Cartesia API key and voice ID
- Language-voice mappings
- Audio format (24-bit, 44.1kHz)

---

## Pipeline Flow (End-to-End)

```
1. USER SPEAKS
   ↓
2. STT (stt_groq_whisper) — Groq Whisper
   - Audio → Transcription + language_metadata
   ↓
3. ORCHESTRATOR — WebSocket handler receives STT result
   - Extract: query, language_metadata
   - Detect language (text + metadata)
   ↓
4. LANGUAGE DISPATCH
   - German? → Use German prompts, German TTS voice
   - English? → Use English prompts, English TTS voice
   ↓
5. [STREAM A] — IMMEDIATE ACKNOWLEDGMENT
   - Groq Llama 8B: Quick "I understand" response
   - TTS: Synthesize acknowledgment (~500ms)
   - OUTPUT: User hears quick response
   ↓
6. [STREAM B] — INTELLIGENT RESPONSE
   - ORCHESTRATOR → RAG Service (FastAPI endpoint)
   - RAG:
     a) Retrieve from vector DB (FAISS/Qdrant)
     b) Assemble context (context_architecture)
     c) Choose policy mode (sales vs clinical)
     d) Generate structured prompt
     e) Call Groq/OpenRouter LLM
     f) Stream response tokens
   ↓
7. ORCHESTRATOR — Assemble and forward tokens to client
   - Token assembly (handle whitespace collapsing)
   - TTS synthesis (continuous streaming)
   ↓
8. CLIENT — Render final response
   - Display text
   - Play audio
   ↓
9. [BARGE-IN] — User interrupts during TARA's response
   - ORCHESTRATOR detects new audio
   - Cancels TARA's response
   - Restarts pipeline with new query
   - Saves interruption_transcripts for context
   ↓
10. SESSION MANAGEMENT
    - Store: query, response, policy_mode, turn_id
    - Redis: Cache hits for identical queries
    - Hivemind: Knowledge graph updates (optional)
```

---

## Clinical Reasoning (Structured Decision-Making)

### Purpose
Transform TARA into a **strategic business consultant** using the same methodology as medical diagnosis: hypothesize, rank, ask, reflect, refine.

### Implementation (`conversation_policy.py`)

**Phase 1: Hypothesis Generation**
```
User input → Generate 5-7 candidate hypotheses
  - Brand positioning gap?
  - Market differentiation issue?
  - Internal communication problem?
  - Customer experience gap?
  - Strategic partnership opportunity?
```

**Phase 2: Ranking (by urgency & discriminating power)**
```
Each hypothesis scored:
  - Danger/Urgency (1-5)
  - Discriminating power (likelihood of this question eliminating false hypotheses)
  - Confidence (0-1)
→ Top 3 ranked for follow-up
```

**Phase 3: Structured Question**
```
Ask the #1 most discriminating question first:
  ✓ Not open-ended ("Tell me everything...")
  ✓ Not closed ("Is this the problem?")
  ✓ **Targeted interrogative**: Forces user to clarify the actual pain

  Example: "When you say 'brand confusion,' do you mean internally (team doesn't
  embody it) or externally (clients don't perceive the differentiation)?"
```

**Phase 4: Reflection**
```
Before proposing a hypothesis:
  1. Interpret user's response
  2. Map to hypothesis space
  3. Update confirmed/ruled-out lists
  4. Ask clarifying follow-up if needed
```

**Phase 5: Conversation Move**
```
Policy modes:
  - "sales": Yes-ladder (small → big yeses)
  - "clinical": Structured intake (hypothesis → question → reflection)
  - "rapport": Relationship-building first
  - "deep_dive": Multi-turn research
```

### Data Structures

```python
@dataclass
class ConversationPolicyDecision:
    policy_mode: str = "sales"           # Which mode to use
    conversation_stage: str = "general"  # opening, intake, hypothesis, refinement, closing
    response_act: str = "answer"         # ask, answer, clarify, confirm
    hypotheses: List[str]                # Generated candidates
    ranked_differentials: List[Dict]     # [{dx: "...", danger: 3, confidence: 0.8}]
    confirmed_dx: List[str]              # Ruling in hypotheses
    ruled_out_dx: List[str]              # Ruling out hypotheses
    missing_slots: List[str]             # Info needed
```

### Example: B&B. Brand Consulting Intake

**User**: "We're struggling with our brand identity."

**TARA (Phase 1 — Hypothesis)**:
```
Generated hypotheses:
1. Visual identity doesn't reflect service quality (danger: 4)
2. Target market messaging unclear (danger: 5)
3. Internal team doesn't embody brand (danger: 3)
4. Competitive differentiation not communicated (danger: 4)
5. Brand guidelines exist but not enforced (danger: 2)
```

**TARA (Phase 3 — Most Discriminating Question)**:
```
"Let me ask the sharpest question first: When you say 'struggling,'
are your clients confused about what you actually do? Or are your
team members inconsistent in how they present you?"
```

**User Response**: "Both, honestly. But mostly the team."

**TARA (Phase 4 — Reflection & Update)**:
```
Interpretation:
  ✓ Confirmed: Internal brand inconsistency (hypothesis #3)
  ✓ Partially confirmed: External messaging (hypothesis #2)
  ✗ Ruled out: Visual identity is fine (hypothesis #1)

Next question: "Okay—so your visual identity is actually solid,
but the **stories** your team tells differ. Are we talking training,
or is the brand strategy itself unclear?"
```

**Refinement Loop**: Continues until:
- Root cause identified (strategy → training → enforcement)
- Confirmed action path clear
- Buy-in secured for next step

---

## Recent Changes (bundb Branch)

### March 20, 2026 — Optimization & Clinical Reasoning Launch

#### Performance Improvements

**pipeline.py**
- Fixed whitespace collapsing in completion logs
- Prevented "Thatsoundslike" rendering artifact
- Better token assembly with boundary checking

**service_client.py (TTSClient)**
- Added `reset_buffer()` method for mid-stream optimization
- **Before**: ~1946ms TTFC (full reconnect on each batch turn)
- **After**: ~800ms TTFC (buffer reset without reconnect)

**rag_engine.py**
- Qdrant query timeout: 2.5s → 1.0s (non-dashboard queries)
- Groq translation skipped for sales/clinical modes (~200ms save)
- Removed `tts_safe()` from per-token streaming (was corrupting partial tokens)
- Policy snapshot always builds with fallback mode (compact prompt)

#### Feature: Clinical Reasoning Mode

**conversation_policy.py** (NEW)
- Hypothetico-deductive structured intake
- Differential ranking with danger/urgency scoring
- Reflection loop: interpret → map → ask clarifying
- Stateful multi-turn tracking (confirmed/ruled-out hypotheses)

**context_architecture_bundb_clinical.py** (NEW)
- Clinical policy mode prompt assembly
- Hypothesis generation and ranking
- Discriminating question selection
- Reflection guidance for TARA

#### Code Quality

**context_architecture_bundb.py (v6)**
- Zone C history depth: 4 turns → 3 turns (better context window efficiency)
- Zone D deduplication: Removed `tenant_memory` + `knowledge_base` blocks (~3200 char save)
- Enhanced "Aktives Zuhören" (Active Listening) section

**config.py**
- `policy_mode_default = "sales"` (confirmed for production stability)

#### New Files Added
- `Orchestrator-eu/core/conversation_policy.py` — Policy layer
- `Orchestrator-eu/core/session_summary_manager.py` — Session windowing
- `rag-eu/context_architecture/context_architecture_bundb_clinical.py` — Clinical intake prompts
- `line/` — Debugging/analysis tools (WIP)

---

## Critical Rules (Inviolable)

These exist due to hard-won debugging lessons. **Never violate without explicit approval.**

| Rule | Reason | Impact |
|------|--------|--------|
| **Action History Rule** | Prevents infinite click loops | Lost context = silent hangs |
| **JSON Keyword Rule** | Groq API crashes without it | Syntax errors, malformed responses |
| **Narrate Then Act Rule** | TARA must say what she's doing | Breaks expressive co-pilot persona |
| **Sync Rule** | TTS must finish before next step | Voice lagging behind actions, confusion |
| **Contextual ID Rule** | Never read raw element IDs | "tara-btn-123" sounds robotic |
| **Stability Rule** | Use targeted edits, not rewrites | Introduces unexpected bugs in complex codebase |

---

## Configuration

### Environment Variables (Key)

| Variable | Purpose | Example |
|----------|---------|---------|
| `GROQ_API_KEY` | Groq LLM access | `gsk_...` |
| `CARTESIA_API_KEY` | Cartesia TTS | `cartesia_...` |
| `CARTESIA_VOICE_ID` | German voice ID | `f786b574-...` |
| `RAG_SERVICE_URL` | Internal RAG endpoint | `http://rag-eu:8003` |
| `REDIS_URL` | Redis connection | `redis://redis-eu:6379/0` |
| `POLICY_MODE_DEFAULT` | Default policy mode | `sales`, `clinical` |
| `TARA_DEFAULT_LANGUAGE` | Default language | `de`, `en` |

### Docker Compose (`docker-compose-eu.yml`)

- **Redis 7**: In-memory cache, session state
- **RAG-eu**: Uvicorn on port 4001 (internal) → 8031 (external)
- **STT**: Groq Whisper, port 4002 → 8032
- **TTS**: Cartesia Sonic-3, port 4000 → 8033
- **Orchestrator-eu**: WebSocket on 8004 → 8030 (WSS)

All services reach each other via internal docker DNS (e.g., `http://rag-eu:8003`).

---

## Testing

### Unit Tests
- `rag-eu/tests/test_rag_engine.py` — Retrieval and response generation
- `rag-eu/tests/test_semantic_detective_v3.py` — DOM analysis
- `rag-eu/tests/test_decision_route_v3.py` — Policy routing

### Integration Tests
- End-to-end WebSocket session handling
- Language detection accuracy
- TTS streaming synchronization
- Barge-in interruption handling

### Running Tests
```bash
# All RAG tests
pytest rag-eu/tests/ -v

# Specific test
pytest rag-eu/tests/test_rag_engine.py -v

# With coverage
pytest rag-eu/tests/ --cov=rag-eu --cov-report=html
```

---

## Known Issues & Limitations

| Issue | Status | Workaround |
|-------|--------|-----------|
| Whitespace collapsing in logs | ✅ FIXED (3/20) | Token assembly now boundary-aware |
| TTS reconnect latency | ✅ FIXED (3/20) | `reset_buffer()` method added |
| Partial token corruption | ✅ FIXED (3/20) | Removed `tts_safe()` from streaming path |
| Qdrant query timeout (2.5s) | ✅ FIXED (3/20) | Reduced to 1.0s for non-dashboard |
| Long conversation context bloat | ✅ MITIGATED (3/20) | Session summary windowing added |
| Groq translation overhead | ✅ OPTIMIZED (3/20) | Skipped for sales/clinical modes |

---

## Next Steps & Roadmap

### Immediate (This Sprint)
- [ ] Clinical reasoning A/B testing with real B&B. consultants
- [ ] Fine-tune hypothesis ranking weights
- [ ] Add explicit "switch policy mode" user intent detection

### Mid-term (Next 2 Sprints)
- [ ] Multi-language clinical reasoning (German prompts fully validated)
- [ ] Conversational differential export (PDF report of explored hypotheses)
- [ ] Real-time policy adjustment based on user engagement

### Long-term
- [ ] Integration with Hubspot CRM (lead scoring via policy decisions)
- [ ] Advanced memory (persistent differential tracking per client)
- [ ] Multi-turn dialogue benchmarking (clinical reasoning quality metrics)

---

## Support & Documentation

**Developer Cookbook**: `~/tara_developer_cookbook/`
- `01_Architecture_Overview.md` — System design & data flow
- `02_Debugging_Diaries.md` — Hard-won bug fixes
- `03_Expressive_CoPilot_Guide.md` — Personality & narration
- `04_Service_Configuration.md` — Deployment reference
- `05_Rules_For_Future_Agents.md` — Critical constraints

**Local Development**:
```bash
# Start services locally
docker compose -f docker-compose.local-eu.yml up

# Monitor logs
docker logs -f orchestrator-eu
docker logs -f rag-eu

# Health check
curl http://localhost:8004/health
curl http://localhost:8003/health
```

---

## Contact & Escalation

**Project Lead**: Amar
**Repo**: `/Users/amar/demo.davinciai` (main branch: `main`, dev: `bundb`)
**Memory**: Project context saved in HIVE-MIND (conversation_id: TARA_X1)

---

*Document Version: 2.0 | Last Updated: 2026-03-20 | Status: Production (Staging: bundb)*

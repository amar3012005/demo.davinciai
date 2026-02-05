# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TARA is a **dual-stream AI Visual Co-Pilot** for "Expressive Co-Browsing". She narrates her intent as she performs actions, waits for her voice to finish before acting, remembers action history to prevent loops, and asks for help when stuck.

**Core Philosophy**: TARA is not a silent robot - she narrates, then acts.

## Service Architecture

| Service | Directory | Port (internal:external) | Function |
|---------|-----------|--------------------------|----------|
| Orchestrator | `orchestra_daytona.v2/` | 8004:8443 | WebSocket handler, state management |
| RAG | `rag-daytona.v2/` | 8003:8444 | Planning, intelligence, DOM analysis |
| TTS | `tts_cartesia/` | 8000 | Text-to-Speech (Cartesia Sonic-3) |
| STT | `stt_groq_whisper/` | 8002 | Speech-to-Text (Groq Whisper) |
| Redis | - | 6379 | State & cache |

### Dual-Stream Architecture
- **Stream A (Voice)**: Fast acknowledgment via STT → Groq Llama 8b → TTS (<500ms latency)
- **Stream B (Planner)**: Complex reasoning via RAG Service with Llama 70b (2-5s latency)

## Development Commands

```bash
# Service health and logs
docker compose ps
docker logs -f orchestrator-ekgcgssw8css400gs8wwggwo
docker logs -f rag-ekgcgssw8css400gs8wwggwo

# Local development
cd orchestra_daytona.v2 && pip install -r requirements.txt && python app.py
cd rag-daytona.v2 && pip install -r requirements.txt && uvicorn app:app --reload --port 8003

# Run tests
pytest rag-daytona.v2/tests/ -v
pytest shared/tests/ -v

# Rebuild and deploy
docker compose build --no-cache
docker compose up -d
```

## Critical Rules

These constraints exist due to hard-won debugging lessons. **Do not violate them.**

1. **Action History Rule**: Never remove `action_history` logic from Orchestrator or Planner - prevents infinite click loops
2. **JSON Keyword Rule**: Keep the safety net in `rag-daytona.v2/llm_providers/groq_provider.py` that injects "Respond in json format" - Groq API crashes without it
3. **Narrate Then Act Rule**: All actions must have a `speech` field - silent actions break the co-pilot persona
4. **Sync Rule**: `ws_handler.py` must await TTS task before `_execute_next_step` - prevents voice lagging behind actions
5. **Contextual ID Rule**: Never read raw element IDs (e.g., `tara-btn-123`) to users - sounds robotic
6. **Stability Rule**: Use targeted edits, not full file rewrites - complex codebase where rewrites introduce new bugs

## Key Files

| File | Purpose |
|------|---------|
| `orchestra_daytona.v2/core/ws_handler.py` | WebSocket session handler, state machine (LISTENING→THINKING→SPEAKING→EXECUTING) |
| `orchestra_daytona.v2/config.yaml` | Service configuration (languages, dialogues, timeouts) |
| `rag-daytona.v2/visual_orchestrator.py` | Self-aware planner with OODA loop and map hint validation |
| `rag-daytona.v2/llm_providers/groq_provider.py` | Groq API integration with JSON safety net |
| `rag-daytona.v2/app.py` | RAG FastAPI app with `PlanStepRequest` (must accept `action_history`) |
| `rag-daytona.v2/rag_engine.py` | FAISS vector search, entity boosting, response humanization |
| `shared/` | Common utilities: events, Redis client, health checks |

## Technology Stack

- **Backend**: Python 3.9+, FastAPI, Uvicorn, WebSockets
- **LLM Providers**: Groq (Llama 8b/70b), OpenAI, Anthropic, Gemini, OpenRouter
- **Vector DB**: Qdrant (cloud), FAISS (local)
- **TTS**: Cartesia Sonic-3 (voice ID: `f786b574-daa5-4673-aa0c-cbe3e8534c02`)
- **STT**: Groq Whisper (whisper-large-v3)
- **State/Cache**: Redis 7

## Environment Variables

Key variables (see docker-compose.yml for full list):
- `GROQ_API_KEY` / `LLM_API_KEY` - LLM provider API key
- `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` - TTS configuration
- `RAG_SERVICE_URL=http://rag:8003` - Internal service URL (not localhost in Docker)
- `REDIS_URL=redis://redis:6379/0` - Redis connection
- `QDRANT_URL`, `QDRANT_API_KEY` - Vector database

## Documentation

See `~/tara_developer_cookbook/` for comprehensive survival guide:
- `01_Architecture_Overview.md` - Dual-stream system, data flow diagrams
- `02_Debugging_Diaries.md` - Hard-won bug fixes and lessons learned
- `03_Expressive_CoPilot_Guide.md` - Personality prompts, narration guidelines
- `04_Service_Configuration.md` - Docker, environment variables, deployment
- `05_Rules_For_Future_Agents.md` - Critical constraints (see above)

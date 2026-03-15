# TARA EU Stack - Development Journal

## 2026-03-14

### Session 1: Persona & Identity Fixes

**1. STT Prompt Hallucination Fix**
- Removed `GROQ_BASE_PROMPT` entirely from `docker-compose-eu.yml` (stt-eu and orchestrator-eu services)
- Set `GROQ_BASE_PROMPT=` (empty) in `.env.eu` to disable globally
- Root cause: Groq Whisper was returning the prompt itself as transcription for silence/noise

**2. TARA Persona Identity Correction** (`context_architecture_bundb.py`)
- Updated Zone A identity section to clarify:
  - TARA works AT B&B. as a colleague (not that B&B. is built by DaVinci AI)
  - TARA is BUILT BY DaVinci AI
  - B&B. is an independent agency, NOT built by DaVinci AI
- Added new section "Umgang mit Wissenslücken — nicht halluzinieren"
  - Instructs TARA to acknowledge when she doesn't know something
  - Provides example phrases for honest uncertainty
  - Prevents hallucination about B&B.-specific information

---

## Journal Tracking Format
*Add 1-2 lines below after each update:*
- **YYYY-MM-DD**: [File] - [Brief description of change]

---

## Tracking Log

<!-- Add new entries here (most recent first) -->

- **2026-03-15**: `Orchestrator-eu/static/BundBTaraVoiceWidget_new.jsx` - Fixed `playback_done` not being sent: added debug logging, fixed is_final handling for stale chunks, only reset audioStreamCompleteRef when actual audio data exists
- **2026-03-15**: `context_architecture/context_architecture.py` - Fixed `TypeError` with `interrupted_text` param: updated `_render_zone_c()` signature and added interruption block rendering for barge-in handling
- **2026-03-15**: `docker-compose.embeddings.yml` - Added embeddings-eu service with native HTTPS (port 8001→4006, SSL certs from Let's Encrypt), matching orchestrator-eu pattern
- **2026-03-15**: `.env.eu`, `.env.coolify` - Updated `EMBEDDINGS_SERVICE_URL` to internal Docker URL `https://embeddings-eu:4006/embed`
- **2026-03-15**: `remote_embeddings.py` - Added `follow_redirects=True`, `verify=False` for HTTPS, `httpx.ConnectError` handler with warning-level logging
- **2026-03-14**: `context_architecture_bundb.py` - Fixed TARA persona: clarified she works AT B&B. (built BY DaVinci AI), B&B. is independent, added anti-hallucination instructions
- **2026-03-14**: `docker-compose-eu.yml`, `.env.eu` - Removed `GROQ_BASE_PROMPT` entirely to fix STT prompt hallucination issue

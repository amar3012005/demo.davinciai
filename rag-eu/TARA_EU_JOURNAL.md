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

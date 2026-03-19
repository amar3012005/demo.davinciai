# Rag-Visual-Copilot Cleanup Summary

## What Was Dumped

### 1. extract_payload/ (20 files, ~1,532 lines)
One-off web scraping utilities that are not part of the runtime:
- `auto_linker.py`, `auto_linker_groq.py` - Link extraction utilities
- `console_groq_extractor.py`, `run_console_scraper.py` - Console scraping
- `engel_voelkers_extractor.py`, `run_engel_scraper.py` - Real estate scraping
- `pornpics_de_extractor.py`, `run_pornpics_scraper.py` - NSFW site scraping
- `groq_com_extractor.py` - Groq site extraction
- `push_*.py` - Data push utilities
- `mock_extraction.py` - Test mock
- `delete_mess.py`, `parse_local.py`, `urls.py` - Misc utilities
- `*.json` - Extracted data files

### 2. root_unused/ (2 files, ~925 lines)
- `config.py` - Unused configuration module
- `websocket_handler_ultimate.py` - Unused WebSocket handler

### 3. reexport_wrappers/ (7 files, ~21 lines)
Files that only did `from X import *` (re-exports):
- `mind_reader.py` - Re-exported root mind_reader.py
- `mission_brain.py` - Re-exported root mission_brain.py
- `semantic_detective.py` - Re-exported root semantic_detective.py
- `semantic_detector.py` - Re-exported semantic_detective
- `mission_planner.py` - Re-exported mission_brain
- `hive_interface.py` - Re-exported root hive_interface.py
- `live_graph.py` - Re-exported root live_graph.py

### 4. domain_prompts/ (2 files, ~245 lines)
Site-specific prompt files (unused domains):
- `pornpics_de.py` - NSFW domain
- `engelvoelkers_com.py` - Real estate domain

## What Was Fixed

Updated import in `visual_copilot/detection/semantic_detective_service.py`:
- Changed: `from visual_copilot.semantic_detective import ...`
- To: `from semantic_detective import ...`
- Changed: `from visual_copilot.live_graph import ...`
- To: `from live_graph import ...`

## What Remains

### Root Level (Active)
- `app.py` - Main FastAPI application
- `ultimate_api.py` - Ultimate TARA pipeline facade
- `visual_orchestrator.py` - Legacy orchestrator (fallback)
- Core modules: `mind_reader.py`, `mission_brain.py`, `hive_interface.py`, etc.

### visual_copilot/ (Active - 83 Python files)
```
visual_copilot/
├── api/                    # API endpoints (analyse_page, map_hints, etc.)
├── detection/              # Semantic detective service
├── intent/                 # Mind reader service, schema normalization
├── logging/                # Structured logging
├── memory/                 # Hive service, cache, live graph service
├── mission/                # Last-mile execution, constraints, page state, visit graph
├── models/                 # Pydantic contracts
├── navigation/             # Site map validator
├── orchestration/          # Pipeline stages (10+ stage files)
├── prompts/                # Mind reader prompts, mission end prompts
├── routing/                # Lexical, semantic, tier3 routers
└── text/                   # Tokenization, normalization, label extraction
```

## Next Steps for Last-Mile Hardening

1. **Split `visual_copilot/mission/last_mile.py`** (~1,200 lines) into:
   - `page_validator.py` - Site map validation
   - `evidence_collector.py` - Goal-term evidence scoring
   - `action_planner.py` - LLM reasoning
   - `fallback_router.py` - Tiered fallbacks (like pre_decision_stage)
   - `completion_guard.py` - Entity anchor verification

2. **Add missing patterns from pre_decision_stage:**
   - Structured fallback sequences
   - Confidence calibration
   - In-memory caching
   - Token-bounded DOM compression

3. **Consider removing:**
   - `build_site_index.py` - One-time index builder
   - `redis_page_graph.py` - May be unused (check imports)
   - `visual_orchestrator.py` - Legacy, mostly bypassed

## Stats

- **Files removed:** 31
- **Lines removed:** ~2,723
- **Broken imports fixed:** 2

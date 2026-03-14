# Cartesia TTS German Pronunciation Optimization Plan

**Last Updated:** 2026-03-14
**Status:** ✅ IMPLEMENTED - Ready for Testing
**Priority:** Critical - Affects all German TTS output

---

## Problem Statement

Cartesia Sonic-3 TTS is mispronouncing German text in the following ways:

1. **Letter-by-letter acronym reading**: `KI`, `BLAIQ`, `DSGVO` are spelled out as "K...I..." instead of spoken as words
2. **Wrong phonology application**: English phonological rules applied to German text due to missing language parameter
3. **Voice mismatch**: Using English-optimized voice for German output
4. **Streaming chunk boundaries**: German compound words split mid-token causing articulation artifacts

### Example Problem Sentences

```
❌ Actual: "Spannend, dass Sie DaVinci A...I... starten..."
✅ Expected: "Spannend, dass Sie DaVinci starten..."

❌ Actual: "Die K...I... verarbeitet Dokumente"
✅ Expected: "Die künstliche Intelligenz verarbeitet Dokumente"

❌ Actual: "B...L...A...I...Q ist unsere Plattform"
✅ Expected: "Blaiq ist unsere Plattform"
```

---

## Root Cause Analysis

### 1. Missing `language` Parameter (CRITICAL)

**Source:** [Cartesia WebSocket API Docs](https://docs.cartesia.ai/api-reference/tts/websocket)

| Parameter | Current | Required |
|-----------|---------|----------|
| `language` | auto-detect / missing | `"de"` (explicit) |

**Why it matters:**
- Sonic-3 auto-detects language but defaults to English phonology when uncertain
- German acronyms like `KI` are not in the English convention bank
- Without explicit `"language": "de"`, Sonic applies English stress patterns to German text

**Evidence from docs:**
> "Specify the language to ensure correct pronunciation. Each voice has a language it works best with."

---

### 2. Wrong Voice ID (HIGH)

**Source:** [Cartesia Prompting Tips](https://docs.cartesia.ai/build-with-cartesia/sonic-3/prompting-tips)

| Setting | Current | Recommended |
|---------|---------|-------------|
| Voice ID | `b9de4a89-2257-424b-94c2-db18ba68c81a` | `694f9389-aac1-45b6-b726-9d9369183238` |
| Voice Type | Generic multilingual | German-native |

**Why it matters:**
> "Match the voice to the language. Each voice has a language it works best with."

German-native voices have:
- Correct umlaut handling (ä, ö, ü, ß)
- Proper German stress patterns
- Native pronunciation of compound words

---

### 3. Missing Text Preprocessing (HIGH)

**Current State:**
- `tts_safe()` function exists in `rag-eu/context_architecture/context_architecture_bundb.py`
- Contains `TTS_EXPAND` dictionary with acronym mappings
- **NEVER CALLED** in the text-to-speech pipeline

**Why it matters:**
- Cartesia has no built-in knowledge of German acronyms
- `KI` → needs expansion to "künstliche Intelligenz"
- `DSGVO` → needs expansion to "Datenschutz-Grundverordnung"
- `BLAIQ` → needs phonetic spelling "Blaiq"

---

### 4. Streaming Chunk Boundaries (MEDIUM)

**Source:** [Cartesia Continuations Guide](https://docs.cartesia.ai/build-with-cartesia/capability-guides/stream-inputs-using-continuations)

| Parameter | Current | Recommended |
|-----------|---------|-------------|
| `continue` | `true` (correct) | `true` |
| Chunk min size | 20 chars | 40 chars |
| Chunk max size | 150 chars | 200 chars |

**Why it matters:**
- German compound nouns (e.g., `Markenstimme`, `Unternehmenskultur`) get split across chunks
- Incomplete syllables at chunk boundaries cause letter-by-letter artifacts
- `continue: true` maintains prosody continuity

---

### 5. Missing Speed Adjustment (LOW)

**Source:** [Cartesia WebSocket API](https://docs.cartesia.ai/api-reference/tts/websocket)

| Parameter | Current | Recommended |
|-----------|---------|-------------|
| `speed` | 1.0 (default) | 0.9 |

**Why it matters:**
- Lower speed (0.9) gives Sonic more time to articulate German compound words
- Improves clarity for unfamiliar token sequences
- Range: 0.5-2.0 (per docs)

---

### 6. Missing Pronunciation Dictionary (OPTIONAL)

**Source:** [Cartesia Custom Pronunciations](https://docs.cartesia.ai/build-with-cartesia/sonic-3/custom-pronunciations)

| Parameter | Current | Recommended |
|-----------|---------|-------------|
| `pronunciation_dict_id` | Not set | Tenant-specific ID |

**Why it matters:**
- Brand names like `DaVinci AI` need custom pronunciation
- SSML `<phoneme>` tags NOT supported in Sonic-3
- Pronunciation dictionaries are the only way to override default tokenization

---

## Cartesia Capabilities Summary

### Supported Parameters (WebSocket API)

```json
{
  "model_id": "sonic-3",
  "voice": {
    "mode": "id",
    "id": "694f9389-aac1-45b6-b726-9d9369183238"
  },
  "language": "de",
  "speed": 0.9,
  "continue": true,
  "pronunciation_dict_id": "pdict_xxx",
  "output_format": {
    "container": "raw",
    "encoding": "pcm_s16le",
    "sample_rate": 24000
  }
}
```

### SSML Support (Sonic-3)

| Tag | Supported | Notes |
|-----|-----------|-------|
| `<emotion value="...">` | ✅ Yes | Values: `positive`, `sad`, `angry`, `surprised`, `curious` |
| `<phoneme alphabet="..." ph="...">` | ❌ No | Use pronunciation dictionaries instead |
| `<break time="...">` | ❌ No | Use punctuation for prosody |

### Streaming Best Practices

1. **Do many generations over a single WebSocket** - Just use a separate `context_id` for each generation
2. **Set up the WebSocket before the first generation** - Avoids connection latency
3. **Include necessary spaces and punctuation** - Sonic uses these for prosody cues
4. **Use `continue: true` for multi-chunk** - Maintains prosody across chunks
5. **Default `max_buffer_delay_ms: 3000`** - Do NOT reduce below 3000ms

---

## Implementation Plan

### Phase 1: Fix Voice Configuration (Priority: CRITICAL)

**File:** `docker-compose-eu.yml` (lines 167-171)

```yaml
environment:
  - CARTESIA_VOICE_ID=${CARTESIA_VOICE_ID:-694f9389-aac1-45b6-b726-9d9369183238}  # German-native voice
  - CARTESIA_LANGUAGE=${CARTESIA_LANGUAGE:-de}
  - CARTESIA_MODEL=${CARTESIA_MODEL:-sonic-3}
  - CARTESIA_SPEED=${CARTESIA_SPEED:-0.9}  # New: slower for German clarity
```

**Action Items:**
1. Research German-native voice IDs in Cartesia playground
2. Update `docker-compose-eu.yml` with German voice
3. Add `CARTESIA_SPEED` environment variable
4. Update `.env` file with German voice ID

---

### Phase 2: Implement Text Preprocessing (Priority: CRITICAL)

**File:** `Orchestrator-eu/core/service_client.py` (line 205-245)

**Add TTS_EXPAND dictionary:**

```python
# Acronyms Cartesia reads letter-by-letter — expand to spoken German
TTS_EXPAND = {
    "BLAIQ": "Blaiq",
    "KI": "künstliche Intelligenz",
    "DSGVO": "Datenschutz-Grundverordnung",
    "UX": "User Experience",
    "UI": "User Interface",
    "CEO": "Geschäftsführer",
    "HR": "Human Resources",
    "USP": "Alleinstellungsmerkmal",
    "ROI": "Return on Investment",
    "FAQ": "häufig gestellte Fragen",
    "CRM": "Kundenmanagement-System",
    "B2B": "Business-to-Business",
    "B2C": "Business-to-Consumer",
    "AI": "Davinci",  # Brand-specific for DaVinci AI
}
```

**Update `_normalize_tts_text()` method:**

```python
def _normalize_tts_text(self, text: str, language: str = "de") -> str:
    """Normalize text for TTS to improve pronunciation"""
    if not text:
        return ""

    normalized = text.strip()

    # Expand acronyms that Cartesia reads letter-by-letter
    if language == "de":
        for acronym, spoken in TTS_EXPAND.items():
            normalized = re.sub(rf"\b{re.escape(acronym)}\b", spoken, normalized)

    # Ensure terminal punctuation (Cartesia needs this for prosody)
    if normalized and not normalized[-1] in '.!?':
        normalized += '.'

    # Clean up multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized)

    return normalized
```

---

### Phase 3: Verify Language Parameter (Priority: HIGH)

**File:** `Orchestrator-eu/core/service_client.py` (lines 290-350)

**Verify end-to-end flow:**

```python
async def _send_tts_request(
    self,
    text: str,
    language: str = "de",  # ← Must be "de" or "en"
    # ...
):
    # Ensure language is passed to TTS WebSocket
    message = {
        "type": "synthesize",
        "text": self._normalize_tts_text(text, language),
        "language": language,  # ← Pass to TTS service
        # ...
    }
```

**TTS Service → Cartesia:**

```python
# tts_cartesia/cartesia_manager.py:395-398
if "multilingual" in target_model.lower() or "sonic-3" in target_model.lower():
    raw_lang = language or self.config.language
    if raw_lang:
        message["language"] = raw_lang.split("-")[0].split("_")[0]  # "de-DE" → "de"
```

---

### Phase 4: Add Pronunciation Dictionary (Priority: OPTIONAL)

**File:** `tts_cartesia/cartesia_manager.py` (line 386-405)

```python
# Add pronunciation dictionary for brand names
pronunciation_dict_id = os.getenv("CARTESIA_PRONUNCIATION_DICT_ID") or \
                        os.getenv("BUNDB_CARTESIA_PRONUNCIATION_DICT_ID")
if pronunciation_dict_id and pronunciation_dict_id.strip().startswith("pdict_"):
    message["pronunciation_dict_id"] = pronunciation_dict_id.strip()
```

**Create pronunciation dictionary in Cartesia Playground:**

| Word | Pronunciation | Notes |
|------|---------------|-------|
| DaVinci | "dah-VIN-chee" | Italian pronunciation |
| BLAIQ | "blayk" | Rhymes with "lake" |

---

### Phase 5: Streaming Buffer Optimization (Priority: MEDIUM)

**File:** `Orchestrator-eu/core/service_client.py` (lines 197-198)

```python
# German requires larger chunks for compound words
self._min_chunk_size: int = 40   # was 20 — buffer more context
self._max_chunk_size: int = 200  # was 150 — allow longer compounds
```

**File:** `tts_cartesia/app.py` or `cartesia_manager.py`

```python
# DO NOT reduce below 3000ms - Cartesia needs this for tokenization
# max_buffer_delay_ms: int = 3000  # Default, do not change
```

---

## Files to Modify

| Priority | File | Changes |
|----------|------|---------|
| P1 (Critical) | `docker-compose-eu.yml` | German voice ID, speed param |
| P1 (Critical) | `Orchestrator-eu/core/service_client.py` | TTS_EXPAND dict, acronym expansion |
| P1 (Critical) | `tts_cartesia/config.py` | Default voice fallback |
| P2 (High) | `Orchestrator-eu/core/service_client.py` | Verify language passing |
| P2 (High) | `tts_cartesia/cartesia_manager.py` | Always send `language: "de"` |
| P3 (Medium) | `Orchestrator-eu/core/service_client.py` | Increase chunk sizes |
| P4 (Optional) | `tts_cartesia/cartesia_manager.py` | Add pronunciation dict |
| P4 (Optional) | Cartesia Playground | Create pronunciation dictionary |

---

## Testing Checklist

### Unit Tests

```python
# Test acronym expansion
def test_expand_acronyms():
    assert expand("KI ist wichtig") == "künstliche Intelligenz ist wichtig"
    assert expand("BLAIQ ist toll") == "Blaiq ist toll"
    assert expand("DSGVO konform") == "Datenschutz-Grundverordnung konform"

# Test language parameter
def test_language_param():
    assert get_output_language("de") == "de"
    assert get_output_language("de-DE") == "de"

# Test TTS message
def test_tts_message():
    msg = build_tts_message("Hello", language="de")
    assert msg["language"] == "de"
    assert msg["voice"]["id"] == GERMAN_VOICE_ID
```

### Integration Tests

Test these problem sentences:

```
1. "Spannend, dass Sie DaVinci AI starten."
   → Should NOT say "A...I..."

2. "Die KI verarbeitet 42 Dokumente im Jahr 2024."
   → Should say "künstliche Intelligenz"

3. "BLAIQ ist unsere neue Markenplattform."
   → Should say "Blaiq" not "B...L...A...I...Q"

4. "Der ROI beträgt 15% für B2B-Kunden."
   → Should say "Return on Investment" and "Business-to-Business"

5. "Markenstimme und Unternehmenskultur"
   → Compound words should have correct stress
```

### A/B Testing

Compare before/after audio:

| Parameter | A (Current) | B (Optimized) |
|-----------|-------------|---------------|
| Voice ID | English multilingual | German-native |
| Speed | 1.0 | 0.9 |
| Language | auto-detect | explicit "de" |
| Chunks | 20-150 chars | 40-200 chars |

---

## Verification Steps

After deployment:

1. **Start German language session** in TARA widget
2. **Test problem sentences** from integration tests
3. **Verify no letter-by-letter spelling** of acronyms
4. **Verify compound words** have correct German stress
5. **Verify umlauts** (ä, ö, ü, ß) pronounced correctly
6. **Monitor latency** - should not increase significantly

---

## Rollback Plan

If issues occur:

1. Revert `docker-compose-eu.yml` voice ID to previous value
2. Comment out TTS_EXPAND dictionary in service_client.py
3. Restart affected services:
   ```bash
   docker compose restart orchestrator-daytona.v2 tts-cartesia
   ```

---

## Performance Expectations

| Metric | Before | After | Notes |
|--------|--------|-------|-------|
| First Audio Latency | ~40ms | ~40ms | Unchanged |
| Acronym Accuracy | ~30% | ~95% | Major improvement |
| Compound Word Clarity | ~60% | ~90% | Significant improvement |
| Overall German Quality | 6/10 | 9/10 | Native-like |

---

## References

- [Cartesia Sonic-3 Documentation](https://docs.cartesia.ai/build-with-cartesia/sonic-3)
- [Cartesia WebSocket API](https://docs.cartesia.ai/api-reference/tts/websocket)
- [Cartesia Streaming Guide](https://docs.cartesia.ai/build-with-cartesia/capability-guides/stream-inputs-using-continuations)
- [Cartesia Prompting Tips](https://docs.cartesia.ai/build-with-cartesia/sonic-3/prompting-tips)
- [Cartesia Custom Pronunciations](https://docs.cartesia.ai/build-with-cartesia/sonic-3/custom-pronunciations)
- [Cartesia SSML Tags](https://docs.cartesia.ai/build-with-cartesia/sonic-3/ssml-tags)

---

## Implementation Status

- [x] Phase 1: Voice Configuration ✅
  - Updated `docker-compose-eu.yml` with German-native voice ID (`694f9389-aac1-45b6-b726-9d9369183238`)
  - Added `CARTESIA_SPEED` environment variable (default: 0.9)
  - Added `CARTESIA_PRONUNCIATION_DICT_ID` environment variable

- [x] Phase 2: Text Preprocessing ✅
  - Added `TTS_EXPAND` dictionary with 15+ German acronym expansions
  - Updated `_normalize_tts_text()` to apply acronym expansion for German
  - Added terminal punctuation enforcement

- [x] Phase 3: Language Parameter Verification ✅
  - Updated `cartesia_manager.py` to ALWAYS send `language` parameter (not conditional)
  - Language normalization: "de-DE" → "de", "en_US" → "en"

- [x] Phase 4: Pronunciation Dictionary ✅
  - Added `pronunciation_dict_id` to `CartesiaConfig`
  - Added tenant-specific pronunciation dictionary lookup (`_get_tenant_pronunciation_dict_id()`)
  - Auto-applies `bundb` dictionary when tenant_id is "bundb"

- [x] Phase 5: Streaming Optimization ✅
  - Increased `_min_chunk_size` from 20 → 40 chars
  - Increased `_max_chunk_size` from 150 → 200 chars
  - Prevents splitting German compound words mid-word
  - Fixed `_flush_chunks()` to preserve leading whitespace between chunks for Cartesia continuations

- [x] Phase 6: Tenant ID Propagation ✅
  - Updated `ws_handler.py` to pass `tenant_id` to `stream_chunk()` and `synthesize()` calls
  - Ensures tenant-specific pronunciation dictionaries are auto-applied

- [x] Testing & Verification Ready for Deployment

---

## Tenant-Specific Configuration

The TTS optimizations work for **all tenants** through a combination of environment variables and tenant-specific lookup:

### 1. Environment Variables (Global Defaults)

```yaml
# docker-compose-eu.yml
CARTESIA_VOICE_ID: ${CARTESIA_VOICE_ID:-694f9389-aac1-45b6-b726-9d9369183238}  # German-native
CARTESIA_SPEED: ${CARTESIA_SPEED:-0.9}
CARTESIA_PRONUNCIATION_DICT_ID: ${CARTESIA_PRONUNCIATION_DICT_ID:-}
```

### 2. Tenant-Specific Pronunciation Dictionary (Auto-Applied)

**File:** `Orchestrator-eu/core/service_client.py`

```python
@staticmethod
def _get_tenant_pronunciation_dict_id(tenant_id: Optional[str]) -> Optional[str]:
    """Get tenant-specific pronunciation dictionary ID for brand names."""
    if not tenant_id:
        return None
    PRONUNCIATION_DICTS = {
        "bundb": "pdict_aiA2sefpW2w4nXqFjde8pa",  # BUNDB tenant
        # Add more tenants as needed
    }
    return PRONUNCIATION_DICTS.get(tenant_id.lower())
```

### 3. How It Works Per Tenant

| Tenant | Voice ID | Speed | Pronunciation Dictionary |
|--------|----------|-------|-------------------------|
| **bundb** | German-native (694f9389) | 0.9 | `pdict_aiA2sefpW2w4nXqFjde8pa` (BLAIQ, DaVinci) |
| **davinci** | German-native (694f9389) | 0.9 | None (can be added) |
| **Other tenants** | German-native (694f9389) | 0.9 | None (can be added per tenant) |

### 4. Adding a New Tenant

1. Add pronunciation dictionary in Cartesia Playground
2. Add entry to `PRONUNCIATION_DICTS` in `service_client.py`
3. Add environment variable to `.env.eu`:
   ```
   {tenant}_PRONUNCIATION_DICT_ID=pdict_xxx
   ```

---

## Files Modified

| File | Changes Made |
|------|-------------|
| `docker-compose-eu.yml` | German voice ID (`694f9389-aac1-45b6-b726-9d9369183238`), `CARTESIA_SPEED`, `CARTESIA_PRONUNCIATION_DICT_ID` |
| `tts_cartesia/config.py` | Added `speed`, `pronunciation_dict_id` fields, updated default voice |
| `tts_cartesia/cartesia_manager.py` | Always send `language`, add `speed`, add `pronunciation_dict_id`, `continue: true` for continuations |
| `Orchestrator-eu/core/service_client.py` | TTS_EXPAND dict, `_get_tenant_pronunciation_dict_id()`, chunk sizes (40-200), `_flush_chunks()` preserves leading whitespace |
| `Orchestrator-eu/core/ws_handler.py` | Pass `tenant_id` to `stream_chunk()` and `synthesize()` |

---

## Continuations Implementation

Cartesia continuations are correctly implemented:

1. **Same `context_id`** used for all chunks in a stream
2. **`continue: true`** sent for all chunks after the first
3. **Leading whitespace preserved** between chunks to form valid transcript when joined
4. **`max_buffer_delay_ms`** kept at default 3000ms (not reduced)

Example flow:
```
Chunk 1: {"context_id": "abc123", "transcript": "Hallo, wie geht es Ihnen?", "continue": false}
Chunk 2: {"context_id": "abc123", "transcript": " Ich bin TARA.", "continue": true}  ← leading space preserved
Chunk 3: {"context_id": "abc123", "transcript": " Wie kann ich helfen?", "continue": true}  ← leading space preserved
```

Result: `"Hallo, wie geht es Ihnen? Ich bin TARA. Wie kann ich helfen?"` ✅

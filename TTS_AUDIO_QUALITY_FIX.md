# TARA TTS Audio Quality Fix — Pre-Deployment Audit
**Date**: 2026-03-20
**Status**: Critical fixes required before production
**Focus**: German pronunciation, buffer optimization, Cartesia settings

---

## Critical Issues Identified (from NotebookLM Analysis)

### 1. ❌ TEXT SPACING BREAKS PRONUNCIATION
**Problem**: When streaming LLM tokens, chunks are concatenated WITHOUT spacing.
- Input: `"Hallo"` + `"Welt"` → **"HalloWelt"** ❌ (invalid German word)
- Expected: `"Hallo"` + `" Welt"` → **"Hallo Welt"** ✅

**Impact**: Mispronunciation, German sounds robotic

**Location**: Orchestrator-EU / TTS streaming pipeline

---

### 2. ❌ NO CONTINUATION FLAG FOR PROSODY
**Problem**: Cartesia needs `continue=True/False` flag to maintain natural speech flow.
- Without continuation flag: Audio has sudden pitch drops, unnatural pacing
- With `continue=True` on intermediate chunks: Model maintains conversational tone
- With `continue=False` on final chunk: Model finishes with natural downward inflection

**Current State**: Likely sending all chunks without continuation flags

**Impact**: Choppy, robotic speech even when pronunciation is correct

**Location**: `tts_cartesia/cartesia_manager.py` → streaming input handler

---

### 3. ❌ SAMPLE RATE MISMATCH
**Current Config**: `sample_rate: 44100` Hz (44.1kHz)
**Problem**:
- Orchestrator may expect 16kHz (standard for voice agents)
- Mismatch causes "chipmunk" or distorted audio on playback

**Recommendation**: Use **16000 Hz** (16kHz) for real-time voice agents
- Reduces bandwidth 3x
- Eliminates sample rate conversion distortion
- Standard for WebRTC / browser playback

**Location**: `tts_cartesia/config.py` line 33

---

### 4. ❌ OUTPUT ENCODING SUBOPTIMAL
**Current Config**: `output_format: pcm_f32le` (32-bit float)
**Issues**:
- Client browsers struggle with f32le decoding
- Unnecessary precision for voice (human ear can't distinguish)

**Recommendation**: Change to **pcm_s16le** (16-bit signed integer)
- Native browser support
- 2x smaller bandwidth
- Standard for telephony/voice

**Location**: `tts_cartesia/config.py` line 34

---

### 5. ❌ SPEED SETTING UNCLEAR
**Current Config**: `speed: 0.9`
**Issue**:
- 0.9 = 90% speed = SLOWER than normal
- Can make German sound unnatural if not tuned precisely
- No testing of impact on German pronunciation

**Recommendation for German**: Test both
- `0.95` (5% slower) — preserve natural rhythm, clarify German sounds
- `1.0` (normal speed) — faster, more energetic

**Location**: `tts_cartesia/config.py` line 39

---

### 6. ❌ NO BUFFER MANAGEMENT BETWEEN TURNS
**Problem**:
- If TTS context doesn't properly signal "end of input", Cartesia waits indefinitely
- Results in late/missing final chunks
- Creates latency

**Current**: `reset_buffer()` exists in Orchestrator, but may not properly signal context close

**Recommendation**:
- Explicitly send `continue=False` on final chunk
- Send empty string with `continue=False` to flush buffer
- Implement proper context lifecycle management

**Location**: `tts_cartesia/cartesia_manager.py` → streaming method

---

## Quick Fix Summary

| Issue | Fix | Priority |
|-------|-----|----------|
| Text spacing | Add space preservation between tokens | 🔴 CRITICAL |
| Continuation flags | Implement `continue=True/False` logic | 🔴 CRITICAL |
| Sample rate | Change 44100 → 16000 Hz | 🟠 HIGH |
| Encoding | Change pcm_f32le → pcm_s16le | 🟠 HIGH |
| Speed | Test 0.95-1.0 for German | 🟡 MEDIUM |
| Context flush | Proper `continue=False` on final chunk | 🔴 CRITICAL |

---

## Detailed Fixes

### FIX 1: Text Spacing in Token Assembly

**File**: `Orchestrator-eu/core/pipeline.py` or `Orchestrator-eu/core/service_client.py`

**Current (WRONG)**:
```python
full_text = ""
async for token in rag_stream:
    full_text += token  # ❌ No space handling
    yield {"token": token}
```

**Fixed (CORRECT)**:
```python
full_text = ""
prev_token = ""
async for token in rag_stream:
    # Add space between tokens if neither ends/starts with whitespace
    if full_text and prev_token and not prev_token.endswith(" ") and not token.startswith(" "):
        full_text += " " + token
    else:
        full_text += token

    prev_token = token
    yield {"token": token, "requires_space": not token.startswith(" ")}
```

**Or at TTS input level** (`tts_cartesia/cartesia_manager.py`):
```python
async def stream_text_to_speech(self, text_chunks: AsyncIterator[str], ...):
    """
    Stream text chunks to Cartesia, with proper spacing.
    """
    buffered_text = ""
    chunk_count = 0

    async for chunk in text_chunks:
        chunk_count += 1

        # Add space if previous chunk didn't end with space AND new chunk doesn't start with space
        if buffered_text and not buffered_text.endswith((" ", "\n", "-")):
            if chunk and not chunk.startswith((" ", "\n", "-")):
                buffered_text += " "

        buffered_text += chunk

        # Don't buffer too long (100 chars max per chunk to Cartesia)
        if len(buffered_text) > 100:
            await self._send_text_chunk(
                text=buffered_text,
                continue_flag=True,  # More chunks coming
            )
            buffered_text = ""

    # Final chunk with close signal
    if buffered_text:
        await self._send_text_chunk(
            text=buffered_text,
            continue_flag=False,  # This is the last chunk
        )
```

---

### FIX 2: Implement Continuation Flags

**File**: `tts_cartesia/cartesia_manager.py`

**Current (MISSING CONTINUATION)**:
```python
async def _send_text_chunk(self, text: str):
    message = {
        "api_key": self.config.api_key,
        "text": text,
        "voice": self.config.get_voice_config(),
        "output_format": self.config.get_output_format_config(),
        # ❌ Missing "continue" flag
    }
    await self.ws.send(json.dumps(message))
```

**Fixed (WITH CONTINUATION)**:
```python
async def _send_text_chunk(
    self,
    text: str,
    continue_flag: bool = True,  # Default: more chunks coming
    is_final: bool = False,
):
    """
    Send text chunk to Cartesia with proper continuation signaling.

    Args:
        text: Text to synthesize
        continue_flag: True if more chunks coming, False if this is last chunk
        is_final: If True, also send empty string to flush buffer
    """

    # Main chunk
    message = {
        "api_key": self.config.api_key,
        "text": text,
        "voice": self.config.get_voice_config(),
        "output_format": self.config.get_output_format_config(),
        "continue": continue_flag,  # ✅ CRITICAL: Tells Cartesia about stream status
        "speed": self.config.speed,
    }

    logger.debug(f"Sending chunk: len={len(text)}, continue={continue_flag}")
    await self.ws.send(json.dumps(message))

    # If final chunk, also send empty string to flush
    if is_final:
        await asyncio.sleep(0.05)  # Small delay
        flush_message = {
            "api_key": self.config.api_key,
            "text": "",  # Empty = flush buffer
            "continue": False,
            "speed": self.config.speed,
        }
        logger.debug("Flushing buffer with empty chunk")
        await self.ws.send(json.dumps(flush_message))
```

**Usage in Streaming Loop**:
```python
async for i, chunk in enumerate(text_stream):
    is_last = (i == total_chunks - 1)  # Know if this is last
    await self._send_text_chunk(
        text=chunk,
        continue_flag=not is_last,  # False only on last chunk
        is_final=is_last,
    )
```

---

### FIX 3: Optimize Audio Settings

**File**: `tts_cartesia/config.py`

**Change 1: Sample Rate (44.1kHz → 16kHz)**
```python
# Line 33 - BEFORE
sample_rate: int = field(default_factory=lambda: int(os.getenv("CARTESIA_SAMPLE_RATE", "44100")))

# AFTER
sample_rate: int = field(default_factory=lambda: int(os.getenv("CARTESIA_SAMPLE_RATE", "16000")))
```

**Rationale**:
- 16kHz is standard for voice agents
- Eliminates sample rate conversion (chipmunk effect)
- Reduces bandwidth 3x (44100 bytes → 16000 bytes per second)
- Browser/WebRTC native support

**Change 2: Output Format (f32le → s16le)**
```python
# Line 34 - BEFORE
output_format: str = field(default_factory=lambda: os.getenv("CARTESIA_OUTPUT_FORMAT", "pcm_f32le").strip())

# AFTER
output_format: str = field(default_factory=lambda: os.getenv("CARTESIA_OUTPUT_FORMAT", "pcm_s16le").strip())
```

**Rationale**:
- 16-bit signed integer = standard for telephony
- Native support in browsers (WebAudio API)
- No precision loss for human speech
- Smaller payload (2x less data)

**Change 3: Speed (0.9 → 0.95 for German)**
```python
# Line 39 - BEFORE
speed: float = field(default_factory=lambda: float(os.getenv("CARTESIA_SPEED", "0.9")))

# AFTER (for German clarity)
speed: float = field(default_factory=lambda: float(os.getenv("CARTESIA_SPEED", "0.95")))
```

**Rationale**:
- 0.95 = 5% slower = preserves German pronunciation clarity
- Not too slow (0.9 can sound sluggish)
- Balances clarity + naturalness
- **TEST BOTH** 0.95 and 1.0 in UAT

**Updated Config Validation**:
```python
def __post_init__(self):
    # ... existing code ...

    logger.info(f"✅ Audio Configuration:")
    logger.info(f"   Sample Rate: {self.sample_rate}Hz (16kHz recommended for voice agents)")
    logger.info(f"   Encoding: {self.output_format} (pcm_s16le recommended)")
    logger.info(f"   Speed: {self.speed} (0.95-1.0 for German)")
    logger.info(f"   Voice: {self.voice_id} (German-native)")

    # Warn if non-optimal settings
    if self.sample_rate != 16000:
        logger.warning(f"⚠️  Sample rate {self.sample_rate} may cause distortion. Recommend 16000Hz")

    if self.output_format != "pcm_s16le":
        logger.warning(f"⚠️  Encoding {self.output_format} not optimal. Recommend pcm_s16le")

    if self.speed < 0.95 or self.speed > 1.0:
        logger.warning(f"⚠️  Speed {self.speed} outside optimal range [0.95-1.0] for German")
```

---

### FIX 4: Proper Context Lifecycle

**File**: `tts_cartesia/cartesia_manager.py`

**Problem**: WebSocket context may hang if not properly closed

**Solution**: Implement explicit context close signal
```python
class CartesiaManager:
    async def synthesize_stream(
        self,
        text_stream: AsyncIterator[str],
        voice_id: Optional[str] = None,
        language: str = "de",
    ) -> AsyncIterator[bytes]:
        """
        Stream text to speech with proper context lifecycle.
        """

        voice_id = voice_id or self.config.voice_id
        context_id = str(uuid.uuid4())

        try:
            chunk_count = 0
            async for chunk in text_stream:
                chunk_count += 1

                # Estimate if this is likely the last chunk
                # (In reality, would need to know from upstream)
                is_final_chunk = False

                await self._send_text_chunk(
                    text=chunk,
                    context_id=context_id,
                    continue_flag=not is_final_chunk,
                    is_final=is_final_chunk,
                )

                # Stream audio chunks back
                async for audio_chunk in self._receive_audio(context_id):
                    yield audio_chunk

            # CRITICAL: Signal end of input stream
            logger.info(f"[{context_id}] End of text stream reached, closing context")
            await self._close_context(context_id)

            # Receive any final audio chunks
            async for audio_chunk in self._receive_audio(context_id, timeout=2.0):
                yield audio_chunk

        except Exception as e:
            logger.error(f"[{context_id}] Streaming error: {e}")
            await self._close_context(context_id)
            raise

    async def _close_context(self, context_id: str):
        """Explicitly close WebSocket context"""
        close_message = {
            "api_key": self.config.api_key,
            "context_id": context_id,
            "text": "",
            "continue": False,  # Signal: no more input
        }
        await self.ws.send(json.dumps(close_message))
        logger.debug(f"[{context_id}] Context close signal sent")
```

---

### FIX 5: German Pronunciation Improvements

**File**: `rag-eu/context_architecture_bundb.py`

**Current**: Limited TTS expansion dictionary

**Enhanced German TTS Dictionary**:
```python
# Expand this dictionary
TTS_EXPAND: Dict[str, str] = {
    # Abbreviations
    "Dr.": "Doktor",
    "Prof.": "Professor",
    "Ltd.": "Limited",
    "GmbH": "Gesellschaft mit beschränkter Haftung",
    "AG": "Aktiengesellschaft",
    "e.V.": "eingetragener Verein",

    # Acronyms
    "BLAIQ": "Blaiq",  # Keep as-is, it's a brand name
    "KI": "künstliche Intelligenz",
    "AI": "künstliche Intelligenz",
    "CRM": "Kundenmanagement-System",
    "SEO": "Suchmaschinen-Optimierung",
    "UX": "User Experience",
    "UI": "User Interface",
    "URL": "Web-Adresse",
    "API": "Schnittstelle",

    # German-specific business terms
    "B&B": "B und B",
    "Brand Voice": "Markenstimme",
    "Compliance": "Richtlinienkonformität",
    "Due Diligence": "genaue Überprüfung",
    "Stakeholder": "Interessenvertreter",
}

# NEW: Pronunciation overrides for tricky words
TTS_PRONUNCIATION_OVERRIDES: Dict[str, str] = {
    "B&B": "B und B",
    "B&B.": "B und B",
    "Coworking": "Ko-working",
    "Upscale": "Up-skayl",  # Anglicism, but this helps TTS
    "Digital": "Di-gi-tal",  # Stress on first syllable in German
    "Premium": "Pre-mi-um",
    "Lifestyle": "Lajf-stajl",
}

# NEW: German number pronunciation (already exists but expand it)
NUMBERS_DE = {
    0: "null", 1: "eins", 2: "zwei", 3: "drei",
    # ... (existing) ...
    # Add support for ordinals
}

def tts_safe(text: str, language: str = "de") -> str:
    """
    Prepare text for TTS with German-native pronunciation.

    Steps:
    1. Expand abbreviations
    2. Replace loanwords
    3. Add SSML breaks for natural pacing
    4. Validate parentheses (read numbers as digits, not words)
    """

    if language != "de":
        return text

    # Step 1: Expand abbreviations
    for abbr, expanded in TTS_EXPAND.items():
        # Use word boundaries to avoid partial replacement
        import re
        text = re.sub(r'\b' + re.escape(abbr) + r'\b', expanded, text)

    # Step 2: Apply pronunciation overrides (for tricky words)
    for word, pronunciation in TTS_PRONUNCIATION_OVERRIDES.items():
        text = text.replace(word, pronunciation)

    # Step 3: Add SSML breaks for natural pacing
    # Insert short breaks after commas and semicolons
    text = text.replace(",", ",<break time=\"500ms\"/>")
    text = text.replace(";", ";<break time=\"700ms\"/>")

    # Insert longer breaks before "aber", "jedoch", "allerdings"
    import re
    text = re.sub(r'(\. )([aA]ber|[jJ]edoch|[aA]llerdings)', r'\1<break time="800ms"/>\2', text)

    # Step 4: Protect brand names (don't expand)
    for brand in PROTECTED_WORDS:
        # Keep as-is
        pass

    return text
```

---

### FIX 6: Debug Logging for Audio Quality

**File**: `tts_cartesia/cartesia_manager.py`

**Add detailed logging**:
```python
async def stream_text_to_speech(self, text_stream, ...):
    """
    Enhanced logging for audio quality debugging.
    """

    logger.info(f"""
    ═══════════════════════════════════════════════
    🎤 TTS Synthesis Started
    ═══════════════════════════════════════════════
    Voice ID: {self.config.voice_id}
    Model: {self.config.model}
    Language: {self.config.language}
    Speed: {self.config.speed}
    Sample Rate: {self.config.sample_rate}Hz
    Encoding: {self.config.output_format}
    ═══════════════════════════════════════════════
    """)

    chunk_number = 0
    total_audio_bytes = 0

    async for chunk in text_stream:
        chunk_number += 1
        logger.debug(f"[Chunk {chunk_number}] len={len(chunk)}, text={chunk[:50]}...")

        # ... synthesis logic ...

        async for audio_chunk in audio_stream:
            total_audio_bytes += len(audio_chunk)
            logger.debug(f"[Audio] chunk={len(audio_chunk)} bytes, total={total_audio_bytes}")
            yield audio_chunk

    logger.info(f"""
    ═══════════════════════════════════════════════
    ✅ TTS Synthesis Complete
    ═══════════════════════════════════════════════
    Total chunks: {chunk_number}
    Total audio bytes: {total_audio_bytes}
    Expected latency: {total_audio_bytes / self.config.sample_rate:.2f}s
    ═══════════════════════════════════════════════
    """)
```

---

## Environment Variables to Update

**File**: `.env.eu` or `.env.local`

```bash
# Audio Quality Settings
CARTESIA_SAMPLE_RATE=16000          # Was 44100 (CRITICAL)
CARTESIA_OUTPUT_FORMAT=pcm_s16le    # Was pcm_f32le (CRITICAL)
CARTESIA_SPEED=0.95                 # Was 0.9 (IMPORTANT for German)
CARTESIA_VOICE_ID=f786b574-daa5-4673-aa0c-cbe3e8534c02  # Verify this is German-native
CARTESIA_LANGUAGE=de                # Ensure German is set
CARTESIA_MODEL=sonic-3              # Multilingual model (supports German well)

# Connection optimization
CARTESIA_PING_INTERVAL=20
CARTESIA_PING_TIMEOUT=10
TTS_CARTESIA_DEBUG=true             # Enable debug logging temporarily
```

---

## Testing Checklist

Before deployment, verify:

### Audio Quality
- [ ] German pronunciation is **natural, not robotic**
- [ ] No "chipmunk" distortion
- [ ] No choppy/stuttering audio
- [ ] Sentence boundaries have natural pauses
- [ ] Speed (0.95) feels natural, not too slow

### Text Handling
- [ ] Multi-word tokens concatenate with proper spacing
- [ ] Abbreviations expanded correctly (Dr. → Doktor)
- [ ] Brand names protected (B&B → B und B)
- [ ] Loanwords handled gracefully

### Latency
- [ ] Time-to-first-audio: **< 1 second**
- [ ] Audio buffer doesn't hang indefinitely
- [ ] Context closes properly after synthesis

### Sample Rate / Encoding
- [ ] Audio plays correctly in browser
- [ ] No sample rate conversion artifacts
- [ ] pcm_s16le decodes without errors

---

## Deployment Steps

1. **Stop services**:
   ```bash
   docker compose -f docker-compose-eu.yml down
   ```

2. **Apply code fixes**:
   - [ ] Fix 1: Text spacing in pipeline
   - [ ] Fix 2: Continuation flags in cartesia_manager.py
   - [ ] Fix 3: Update config.py (sample rate, encoding, speed)
   - [ ] Fix 4: Context lifecycle management
   - [ ] Fix 5: Enhanced German TTS dictionary
   - [ ] Fix 6: Debug logging

3. **Update .env.eu**:
   ```bash
   CARTESIA_SAMPLE_RATE=16000
   CARTESIA_OUTPUT_FORMAT=pcm_s16le
   CARTESIA_SPEED=0.95
   ```

4. **Rebuild and start**:
   ```bash
   docker compose -f docker-compose-eu.yml build --no-cache
   docker compose -f docker-compose-eu.yml up -d
   ```

5. **Test audio quality**:
   ```bash
   # Listen to sample synthesis
   curl -X POST http://localhost:8000/api/v1/synthesize \
     -H "Content-Type: application/json" \
     -d '{
       "text": "Hallo, ich bin TARA. Ich helfe Ihnen mit Ihrer Markenstrategie.",
       "voice_id": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
       "language": "de",
       "speed": 0.95
     }' > /tmp/tara_sample.wav

   # Listen in your audio player
   open /tmp/tara_sample.wav  # macOS
   ```

6. **Monitor logs**:
   ```bash
   docker logs -f tts-eu | grep "TTS Synthesis"
   docker logs -f orchestrator-eu | grep "Token assembly"
   ```

---

## Rollback Plan

If issues occur:

1. Revert `.env.eu` changes
2. Revert code changes (git checkout specific files)
3. Rebuild: `docker compose build --no-cache`
4. Restart services

---

## Post-Deployment Monitoring

**Key metrics to watch**:
- `TTS_TTFC_MS`: Time-to-first-chunk (target: < 1000ms)
- `AUDIO_QUALITY`: User feedback on German pronunciation
- `CONTEXT_ERRORS`: Any WebSocket context hangs
- `ENCODING_ERRORS`: Any audio decoding failures

---

*Critical for Production Release | Pre-Deployment Must-Have | Status: Pending Implementation*

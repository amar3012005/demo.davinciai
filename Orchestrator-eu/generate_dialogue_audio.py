"""
Utility script to pre-synthesize all dialogue manager phrases using TTS-LABS (ElevenLabs).

Reads language-specific JSON configs:
  - orchestra_daytona/assets/dialogues_en.json
  - orchestra_daytona/assets/dialogues_de.json

For each dialogue entry that defines an "audio_file", this script:
  - Calls the TTS-LABS HTTP endpoint (/api/v1/synthesize) via the running
    TTS microservice (default: http://localhost:8006)
  - Receives base64-encoded PCM audio
  - Wraps it in a WAV container and saves to orchestra_daytona/assets/audio/<audio_file>

If a target WAV file already exists, it is skipped (no overwrite by default).

USAGE (from repo root):
  # English (uses http://localhost:2007 by default - exposed Docker port)
  python -m orchestra_daytona.generate_dialogue_audio --lang en

  # German
  python -m orchestra_daytona.generate_dialogue_audio --lang de

  # Overwrite existing files
  python -m orchestra_daytona.generate_dialogue_audio --lang en --overwrite

  # Use custom TTS URL (for Docker or different port)
  DIALOGUE_TTS_URL=http://localhost:2007 python -m orchestra_daytona.generate_dialogue_audio --lang en
  # Or when running inside Docker:
  DIALOGUE_TTS_URL=http://tara-task-tts-labs:8006 python -m orchestra_daytona.generate_dialogue_audio --lang en
"""

import asyncio
import base64
import json
import os
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp
try:
    import yaml
except ImportError:
    yaml = None  # Optional dependency


# Paths
SCRIPT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = SCRIPT_DIR / "assets"
AUDIO_DIR = ASSETS_DIR / "audio"

# Default TTS-LABS HTTP endpoint
# When running locally, use exposed port 2007 (mapped from 8006 in Docker)
# When running in Docker, use service name: http://tara-task-tts-labs:8006
DEFAULT_TTS_BASE_URL = os.getenv(
    "DIALOGUE_TTS_URL", 
    os.getenv("TTS_SERVICE_URL", "http://localhost:2007")  # Default to exposed port
)
TTS_SYNTH_ENDPOINT = f"{DEFAULT_TTS_BASE_URL}/api/v1/synthesize"

# Language-specific voice IDs (from config.yaml or environment)
# Try to load from config.yaml first
VOICE_IDS = {
    "en": os.getenv("ELEVENLABS_VOICE_EN", "sarah"),  # Default English voice
    "de": os.getenv("ELEVENLABS_VOICE_DE", "anna"),    # Default German voice
}

# Try to load voice IDs from config.yaml
try:
    import yaml
    config_path = SCRIPT_DIR / "config.yaml"
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
            tts_config = config_data.get("services", {}).get("tts", {})
            voices = tts_config.get("voices", {})
            if "en" in voices:
                VOICE_IDS["en"] = voices["en"].get("voice_id", VOICE_IDS["en"])
            if "de" in voices:
                VOICE_IDS["de"] = voices["de"].get("voice_id", VOICE_IDS["de"])
except Exception as e:
    # If config loading fails, use defaults
    pass


@dataclass
class DialogueTarget:
    text: str
    emotion: str
    audio_file: str
    language: str

    @property
    def output_path(self) -> Path:
        return AUDIO_DIR / self.audio_file


def load_dialogue_targets(lang: str) -> List[DialogueTarget]:
    """
    Load dialogue entries that require audio for a given language.

    Args:
        lang: 'en' or 'de'
    """
    if lang.lower().startswith("de"):
        json_path = ASSETS_DIR / "dialogues_de.json"
        language_code = "de"
    else:
        json_path = ASSETS_DIR / "dialogues_en.json"
        language_code = "en"

    if not json_path.exists():
        raise FileNotFoundError(f"Dialogue JSON not found: {json_path}")

    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    dialogues = data.get("dialogues", {})
    targets: List[DialogueTarget] = []

    # We care about any group that defines an "audio_file"
    for group_name, items in dialogues.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            audio_file = item.get("audio_file")
            if not audio_file:
                # This dialogue is text-only; nothing to pre-synthesize
                continue
            emotion = item.get("emotion") or "neutral"
            targets.append(DialogueTarget(
                text=text,
                emotion=emotion,
                audio_file=audio_file,
                language=language_code
            ))

    return targets


async def synthesize_one(
    session: aiohttp.ClientSession,
    target: DialogueTarget,
    overwrite: bool = False,
) -> Tuple[DialogueTarget, bool]:
    """
    Synthesize a single dialogue entry via TTS-LABS and write WAV file.

    Returns:
        (target, success)
    """
    out_path = target.output_path

    if out_path.exists() and not overwrite:
        print(f"[SKIP] {out_path.name} already exists")
        return target, True

    # Get voice ID for language
    voice_id = VOICE_IDS.get(target.language, VOICE_IDS["en"])
    
    # Determine language code for ElevenLabs
    language_code = "en-US" if target.language == "en" else "de-DE"

    # TTS-LABS API expects "voice" (not "voice_id") and optional "language"
    payload = {
        "text": target.text,
        "voice": voice_id,  # Use "voice" field as per TTS-LABS API
        "language": language_code,
        "emotion": target.emotion,  # Include emotion (though it may be ignored by ElevenLabs)
    }

    try:
        async with session.post(TTS_SYNTH_ENDPOINT, json=payload, timeout=60) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"[FAIL] {out_path.name}: HTTP {resp.status} -> {text}")
                return target, False

            data = await resp.json()
            
            # Check if response indicates failure
            if not data.get("success", True):
                error_msg = data.get("error", "Unknown error")
                print(f"[FAIL] {out_path.name}: Synthesis failed -> {error_msg}")
                return target, False
                
    except aiohttp.ClientConnectorError as exc:
        print(f"[FAIL] {out_path.name}: Connection error - is TTS service running at {DEFAULT_TTS_BASE_URL}?")
        print(f"       Error: {exc}")
        print(f"       Tip: Set DIALOGUE_TTS_URL environment variable or ensure TTS service is accessible")
        return target, False
    except Exception as exc:
        print(f"[FAIL] {out_path.name}: request error: {exc}")
        return target, False

    # TTS-LABS returns audio_data as base64 string
    audio_b64 = data.get("audio_data")
    sample_rate = data.get("sample_rate") or 24000  # ElevenLabs default

    if not audio_b64:
        print(f"[FAIL] {out_path.name}: no audio_data in response")
        return target, False

    try:
        pcm_bytes = base64.b64decode(audio_b64)
    except Exception as exc:
        print(f"[FAIL] {out_path.name}: base64 decode error: {exc}")
        return target, False

    # Ensure output directory exists
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Wrap raw 16-bit PCM mono into a WAV container
    try:
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)  # Mono
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm_bytes)
    except Exception as exc:
        print(f"[FAIL] {out_path.name}: failed to write WAV: {exc}")
        return target, False

    duration_ms = len(pcm_bytes) / (int(sample_rate) * 2) * 1000.0
    print(f"[OK]   {out_path.name}  ({duration_ms:.0f} ms, sr={sample_rate})")
    return target, True


async def main_async(lang: str, overwrite: bool = False) -> None:
    targets = load_dialogue_targets(lang)
    if not targets:
        print(f"No dialogue entries with audio_file found for lang={lang}")
        return

    print(f"Using TTS endpoint: {TTS_SYNTH_ENDPOINT}")
    print(f"Found {len(targets)} dialogue entries with audio_file for lang={lang}")
    print(f"Output directory: {AUDIO_DIR}")
    print(f"Overwrite existing: {overwrite}")
    print(f"Voice ID: {VOICE_IDS.get(lang[:2], VOICE_IDS['en'])}")
    print("-" * 60)

    async with aiohttp.ClientSession() as session:
        results = []
        # Sequential for simplicity & stability; can be parallelized if needed
        for target in targets:
            res = await synthesize_one(session, target, overwrite=overwrite)
            results.append(res)

    successes = sum(1 for _t, ok in results if ok)
    failures = len(results) - successes
    print("-" * 60)
    print(f"Completed. Success: {successes}, Failed: {failures}")


def parse_args(argv: Optional[List[str]] = None) -> Tuple[str, bool]:
    """
    Minimal CLI arg parsing.

    Supported:
      --lang en|de   (default: en)
      --overwrite    (overwrite existing WAV files)
    """
    if argv is None:
        argv = sys.argv[1:]

    lang = "en"
    overwrite = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--lang" and i + 1 < len(argv):
            lang = argv[i + 1]
            i += 2
        elif arg == "--overwrite":
            overwrite = True
            i += 1
        else:
            print(f"Unknown argument: {arg}")
            i += 1

    return lang, overwrite


def main() -> None:
    lang, overwrite = parse_args()
    try:
        asyncio.run(main_async(lang, overwrite))
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()



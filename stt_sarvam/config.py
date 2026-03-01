
"""
Configuration for Sarvam AI Speech-to-Text service.

Key advantage over Groq STT: Sarvam provides native WebSocket streaming
with server-side VAD, eliminating the need for micro-chunking.
"""

import os
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SarvamSTTConfig:
    # ── Sarvam API ──────────────────────────────────────────────
    api_key: str = ""
    base_ws_url: str = ""
    model: str = "saaras:v2.5"
    
    # ── Sarvam Mode (transcribe, translate, verbatim, translit, codemix) ─
    mode: str = "transcribe"
    # Endpoint routing mode:
    # - auto: /speech-to-text/ws for transcribe/verbatim/translit/codemix, /speech-to-text-translate/ws for translate
    # - stt: force /speech-to-text/ws
    # - stt_translate: force /speech-to-text-translate/ws
    endpoint_mode: str = "auto"
    
    # ── Language code (BCP-47) ──────────────────────────────────
    # unknown = auto-detect, te-IN = Telugu, hi-IN = Hindi, etc.
    language_code: str = "unknown"

    # ── Audio format ────────────────────────────────────────────
    sample_rate: int = 16000
    input_audio_codec: str = "wav"  # API strictly requires audio/wav chunks
    bytes_per_sample: int = 2  # 16-bit = 2 bytes

    # ── Sarvam streaming behaviour ──────────────────────────────
    enable_vad_signals: bool = True        # Get START_SPEECH / END_SPEECH from server
    high_vad_sensitivity: bool = True      # More responsive speech detection
    enable_flush_signal: bool = True       # Flush buffer on speech end for instant finals

    # ── Audio chunking (client → Sarvam forwarding) ─────────────
    # How often we batch-forward PCM to Sarvam. Smaller = lower latency.
    forward_interval_ms: int = 100  # 100 ms chunks → 3200 bytes at 16 kHz
    min_audio_bytes: int = 640      # Minimum bytes to bother sending (20 ms)
    flush_timeout_ms: int = 1200    # Wait this long for final after flush before fallback-final

    @property
    def forward_chunk_bytes(self) -> int:
        return int(self.sample_rate * (self.forward_interval_ms / 1000) * self.bytes_per_sample)

    # ── Local VAD fallback ──────────────────────────────────────
    # Used as safety-net when Sarvam VAD signals are delayed or missing.
    local_vad_energy_threshold: int = 500
    local_vad_silence_ms: int = 800        # ms of silence before local flush
    local_vad_min_speech_ms: int = 150     # ignore very short blips

    @property
    def silence_chunks_threshold(self) -> int:
        """Number of consecutive silent forward-chunks before local VAD triggers."""
        return max(1, int(self.local_vad_silence_ms / self.forward_interval_ms))

    # ── Sarvam connection management ────────────────────────────
    reconnect_delay_initial: float = 0.5
    reconnect_delay_max: float = 10.0
    reconnect_max_attempts: int = 5
    connection_timeout: float = 10.0
    ping_interval: float = 20.0
    ping_timeout: float = 10.0

    # ── Orchestrator integration ────────────────────────────────
    orchestrator_ws_url: str = ""
    skip_ssl_verify: bool = True

    # ── Service ─────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8002
    debug: bool = False
    log_level: str = "INFO"

    # ── Context / prompt ────────────────────────────────────────
    context_prompt: str = ""  # Optional prompt to improve ASR accuracy

    def endpoint_path(self) -> str:
        em = (self.endpoint_mode or "auto").strip().lower()
        mode = (self.mode or "transcribe").strip().lower()
        if em == "stt":
            return "/speech-to-text/ws"
        if em in {"stt_translate", "translate"}:
            return "/speech-to-text-translate/ws"
        # auto mode
        if mode == "translate":
            return "/speech-to-text-translate/ws"
        return "/speech-to-text/ws"

    def resolved_ws_url(self) -> str:
        if self.base_ws_url:
            return self.base_ws_url
        return f"wss://api.sarvam.ai{self.endpoint_path()}"

    @classmethod
    def from_env(cls) -> "SarvamSTTConfig":
        cfg = cls(
            api_key=os.getenv("SARVAM_API_KEY", ""),
            base_ws_url=os.getenv("SARVAM_BASE_WS_URL", ""),  # Auto-selected based on mode if empty
            model=os.getenv("SARVAM_MODEL", "saaras:v2.5"),
            mode=os.getenv("SARVAM_MODE", "transcribe"),
            endpoint_mode=os.getenv("SARVAM_ENDPOINT_MODE", "auto"),
            language_code=os.getenv("SARVAM_LANGUAGE_CODE", "unknown"),
            sample_rate=int(os.getenv("SARVAM_SAMPLE_RATE", "16000")),
            input_audio_codec=os.getenv("SARVAM_AUDIO_CODEC", "pcm_s16le"),
            enable_vad_signals=os.getenv("SARVAM_VAD_SIGNALS", "true").lower() == "true",
            high_vad_sensitivity=os.getenv("SARVAM_HIGH_VAD", "true").lower() == "true",
            enable_flush_signal=os.getenv("SARVAM_FLUSH_SIGNAL", "true").lower() == "true",
            forward_interval_ms=int(os.getenv("SARVAM_FORWARD_INTERVAL_MS", "100")),
            flush_timeout_ms=int(os.getenv("SARVAM_FLUSH_TIMEOUT_MS", "1200")),
            local_vad_energy_threshold=int(os.getenv("SARVAM_VAD_ENERGY", "500")),
            local_vad_silence_ms=int(os.getenv("SARVAM_VAD_SILENCE_MS", "800")),
            orchestrator_ws_url=os.getenv("ORCHESTRATOR_WS_URL", ""),
            skip_ssl_verify=os.getenv("SKIP_SSL_VERIFY", "true").lower() == "true",
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8002")),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            context_prompt=os.getenv("SARVAM_CONTEXT_PROMPT", ""),
        )
        cfg._validate()
        return cfg

    def _validate(self):
        if not self.api_key:
            logger.warning("SARVAM_API_KEY not set — Sarvam connections will fail")
        if self.sample_rate not in (8000, 16000):
            logger.warning(f"Sample rate {self.sample_rate} may not be supported; prefer 16000 or 8000")

        # Normalize/validate mode.
        valid_modes = {"transcribe", "translate", "verbatim", "translit", "codemix"}
        if self.mode not in valid_modes:
            logger.warning(f"Invalid SARVAM_MODE='{self.mode}', defaulting to 'transcribe'")
            self.mode = "transcribe"

        # Translate endpoint can only output translated English reliably; warn if misconfigured.
        resolved_path = self.endpoint_path()
        if resolved_path.endswith("/speech-to-text-translate/ws") and self.mode != "translate":
            logger.warning(
                "Endpoint/mode mismatch: using speech-to-text-translate/ws with non-translate mode. "
                "This may still produce English translation on some keys."
            )

    def build_ws_url(self) -> str:
        params = [
            f"model={self.model}",
            f"sample_rate={self.sample_rate}",
            f"input_audio_codec={self.input_audio_codec}",
            f"mode={self.mode}",
            f"language-code={self.language_code}",
        ]
        if self.enable_vad_signals:
            params.append("vad_signals=true")
        if self.high_vad_sensitivity:
            params.append("high_vad_sensitivity=true")
        if self.enable_flush_signal:
            params.append("flush_signal=true")
        return f"{self.resolved_ws_url()}?{'&'.join(params)}"

    def log_config(self):
        masked_key = f"{self.api_key[:8]}...{self.api_key[-4:]}" if len(self.api_key) > 12 else "***"
        logger.info(f"  Sarvam STT Config:")
        logger.info(f"    API key  : {masked_key}")
        logger.info(f"    Model    : {self.model}")
        logger.info(f"    Mode     : {self.mode}")
        logger.info(f"    Endpoint : {self.endpoint_path()} ({self.endpoint_mode})")
        logger.info(f"    Language : {self.language_code}")
        logger.info(f"    Sample   : {self.sample_rate} Hz, codec={self.input_audio_codec}")
        logger.info(f"    VAD      : server={self.enable_vad_signals}, high_sens={self.high_vad_sensitivity}")
        logger.info(f"    Flush    : {self.enable_flush_signal}")
        logger.info(f"    Flush TO : {self.flush_timeout_ms}ms")
        logger.info(f"    Forward  : {self.forward_interval_ms}ms chunks ({self.forward_chunk_bytes} bytes)")
        logger.info(f"    Local VAD: energy>{self.local_vad_energy_threshold}, silence={self.local_vad_silence_ms}ms")
        logger.info(f"    Service  : {self.host}:{self.port}")
        if self.orchestrator_ws_url:
            logger.info(f"    Orch URL : {self.orchestrator_ws_url}")

"""
Configuration for the Groq Whisper STT microservice.

Loads environment variables with GROQ_* and STT_* prefixes to drive
REST API calls, audio processing, and transcription settings.
"""

import os
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GroqWhisperConfig:
    """
    Unified configuration for Groq Whisper STT microservice.
    
    Handles REST API connectivity, audio format, micro-chunking strategy, and VAD settings.
    """
    
    # Groq API settings
    api_key: str = os.getenv("GROQ_API_KEY", "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")
    base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    model: str = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")
    
    # Audio format settings
    sample_rate: int = int(os.getenv("GROQ_SAMPLE_RATE", "16000"))
    audio_format: str = os.getenv("GROQ_AUDIO_FORMAT", "wav")  # wav, flac, mp3
    
    # Micro-chunking settings (ultra-low latency per user preference)
    enable_micro_chunking: bool = os.getenv("ENABLE_MICRO_CHUNKING", "false").lower() == "true"  # Toggle chunking mode
    chunk_duration_ms: int = int(os.getenv("CHUNK_DURATION_MS", "120"))  # larger chunks improve noise robustness
    overlap_duration_ms: int = int(os.getenv("OVERLAP_DURATION_MS", "60"))  # overlap for continuity
    context_window_segments: int = int(os.getenv("CONTEXT_WINDOW_SEGMENTS", "3"))  # larger context reduces drift
    
    # Base prompt for spelling guidance (e.g., proper nouns, acronyms)
    base_prompt: str = os.getenv("GROQ_BASE_PROMPT", "")
    
    # VAD settings (local pre-filtering)
    vad_energy_threshold: int = int(os.getenv("VAD_ENERGY_THRESHOLD", "850"))  # RMS energy threshold
    min_speech_duration_ms: int = int(os.getenv("MIN_SPEECH_DURATION_MS", "250"))
    min_silence_duration_ms: int = int(os.getenv("MIN_SILENCE_DURATION_MS", "700"))
    final_silence_padding_ms: int = int(os.getenv("FINAL_SILENCE_PADDING_MS", "500"))
    pre_speech_padding_ms: int = int(os.getenv("PRE_SPEECH_PADDING_MS", "500"))
    
    # Groq API parameters
    language: Optional[str] = os.getenv("GROQ_LANGUAGE", "")  # ISO-639-1 for faster processing
    response_format: str = os.getenv("GROQ_RESPONSE_FORMAT", "verbose_json")  # json, verbose_json, text
    temperature: float = float(os.getenv("GROQ_TEMPERATURE", "0.0"))
    include_word_timestamps: bool = os.getenv("GROQ_WORD_TIMESTAMPS", "true").lower() == "true"
    
    # Service settings
    host: str = os.getenv("STT_GROQ_HOST", "0.0.0.0")
    port: int = int(os.getenv("STT_GROQ_PORT", "8002"))
    debug: bool = os.getenv("GROQ_DEBUG", "false").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Redis settings (optional, for backward compatibility)
    redis_host: str = os.getenv("REDIS_HOST", "redis")
    redis_port: int = 6379  # Default
    redis_db: int = int(os.getenv("REDIS_DB", "0"))
    
    # Production: Direct orchestrator WebSocket
    orchestrator_ws_url: Optional[str] = os.getenv("ORCHESTRATOR_WS_URL", None)
    skip_ssl_verify: bool = os.getenv("STT_SKIP_SSL_VERIFY", "true").lower() == "true"
    
    # Timeout settings
    api_timeout_seconds: float = float(os.getenv("GROQ_API_TIMEOUT", "5.0"))
    max_retries: int = int(os.getenv("GROQ_MAX_RETRIES", "3"))
    
    @property
    def chunk_bytes(self) -> int:
        """Calculate bytes per chunk (16-bit PCM, mono)"""
        return int(self.sample_rate * self.chunk_duration_ms / 1000 * 2)
    
    @property
    def overlap_bytes(self) -> int:
        """Calculate overlap bytes"""
        return int(self.sample_rate * self.overlap_duration_ms / 1000 * 2)
    
    @property
    def transcription_url(self) -> str:
        """Get the transcription endpoint URL"""
        return f"{self.base_url}/audio/transcriptions"
    
    def __post_init__(self):
        """Validate configuration and handle port parsing"""
        # Handle REDIS_PORT which might be set to tcp://... by Kubernetes
        env_port = os.getenv("REDIS_PORT", "6379")
        if env_port.startswith("tcp://"):
            try:
                self.redis_port = int(env_port.split(":")[-1])
            except ValueError:
                self.redis_port = 6379
        else:
            try:
                self.redis_port = int(env_port)
            except ValueError:
                self.redis_port = 6379

        if not self.api_key:
            raise ValueError(
                "GROQ_API_KEY must be set. "
                "Set GROQ_API_KEY environment variable with your Groq API key."
            )
        
        # Log API key status (masked for security)
        masked_key = f"{self.api_key[:8]}...{self.api_key[-4:]}" if len(self.api_key) > 12 else "***"
        logger.info(f"🔑 Groq API key configured: {masked_key}")
        
        if self.model not in ["whisper-large-v3-turbo", "whisper-large-v3"]:
            logger.warning(f"Unknown model '{self.model}', using 'whisper-large-v3-turbo'")
            self.model = "whisper-large-v3-turbo"
        
        if self.audio_format not in ["wav", "flac", "mp3", "webm", "ogg"]:
            logger.warning(f"Invalid audio_format '{self.audio_format}', using 'wav'")
            self.audio_format = "wav"
        
        # Log chunk configuration
        if self.enable_micro_chunking:
            logger.info(f"⏱️ Mode: MICRO-CHUNKING ({self.chunk_duration_ms}ms chunks, {self.chunk_bytes} bytes)")
            logger.info(f"🔄 Overlap: {self.overlap_duration_ms}ms")
            logger.info(f"📊 Context window: {self.context_window_segments} segments")
        else:
            logger.info(f"⏱️ Mode: FULL TRANSCRIPTION (buffer until speech end)")
        
        if self.base_prompt:
            logger.info(f"📝 Base prompt: '{self.base_prompt[:50]}...'")
    
    @staticmethod
    def from_env() -> "GroqWhisperConfig":
        """Load configuration from environment variables"""
        return GroqWhisperConfig()

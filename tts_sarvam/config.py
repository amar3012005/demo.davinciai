"""
Configuration for TTS-Sarvam Microservice

Loads environment variables for Sarvam API, audio settings, and connection pooling.
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class SarvamConfig:
    """
    Configuration for Sarvam TTS microservice.
    
    Handles API connectivity, audio format, and connection pool settings.
    """
    
    # Sarvam API settings
    api_key: str = field(default_factory=lambda: os.getenv("SARVAM_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("SARVAM_MODEL", "bulbul:v3-beta"))
    voice_id: str = field(default_factory=lambda: os.getenv("SARVAM_VOICE_ID", "aditya"))
    
    # WebSocket settings
    websocket_url: str = "wss://api.sarvam.ai/text-to-speech/ws"
    
    # Audio format settings
    sample_rate: int = field(default_factory=lambda: int(os.getenv("SARVAM_SAMPLE_RATE", "24000")))
    output_audio_codec: str = field(default_factory=lambda: os.getenv("SARVAM_OUTPUT_CODEC", "linear16").strip())
    
    # Voice settings
    language: str = field(default_factory=lambda: os.getenv("SARVAM_LANGUAGE", "en-IN").strip())
    
    # Connection pool settings (for robustness)
    pool_size: int = field(default_factory=lambda: int(os.getenv("CONNECTION_POOL_SIZE", "3")))
    max_reconnect_attempts: int = 5
    reconnect_base_delay_ms: int = 500  # Exponential backoff base
    
    # Timeouts
    connection_timeout_seconds: float = 10.0
    ping_interval_seconds: float = 20.0
    ping_timeout_seconds: float = 10.0
    
    # Service settings
    host: str = field(default_factory=lambda: os.getenv("TTS_SARVAM_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("TTS_SARVAM_PORT", "8001")) if os.getenv("TTS_SARVAM_PORT", "8001").isdigit() else 8001)
    debug: bool = field(default_factory=lambda: os.getenv("TTS_SARVAM_DEBUG", "false").lower() == "true")
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    
    def __post_init__(self):
        """Validate configuration"""
        if not self.api_key:
            logger.warning("SARVAM_API_KEY not set. API calls will fail unless provided by the environment later.")
        
        valid_models = ["bulbul:v2", "bulbul:v3-beta"]
        if self.model not in valid_models:
            logger.warning(f"Unknown model '{self.model}', using 'bulbul:v3-beta'")
            self.model = "bulbul:v3-beta"
            
        valid_sample_rates = [8000, 16000, 22050, 24000]
        if self.sample_rate not in valid_sample_rates:
            logger.warning(f"Invalid sample rate '{self.sample_rate}', forcing '24000'")
            self.sample_rate = 24000
        
        masked_key = f"{self.api_key[:8]}...{self.api_key[-4:]}" if len(self.api_key) > 12 else "***"
        logger.info(f"🔑 Sarvam API key configured: {masked_key}")
        logger.info(f"🎤 Model: {self.model}")
        logger.info(f"🔊 Speaker: {self.voice_id}")
        logger.info(f"📊 Sample rate: {self.sample_rate}Hz")
        logger.info(f"🔄 Connection pool size: {self.pool_size}")
    
    def get_websocket_url(self) -> str:
        """Get full WebSocket URL"""
        return self.websocket_url
    
    def get_connection_config(self) -> Dict[str, Any]:
        """Get the payload for the configure connection command"""
        return {
            "model": self.model,
            "target_language_code": self.language,
            "speaker": self.voice_id,
            "speech_sample_rate": str(self.sample_rate),
            "output_audio_codec": self.output_audio_codec
        }
    
    @staticmethod
    def from_env() -> "SarvamConfig":
        """Load configuration from environment variables"""
        return SarvamConfig()

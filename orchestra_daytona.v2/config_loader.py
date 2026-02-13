"""
Configuration Loader for Orchestrator

Loads and validates YAML configuration file with structured settings for
services, dialogues, and multi-language support.
Supports environment variable overrides for Docker deployment.
"""

import os
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Server configuration"""
    host: str = "0.0.0.0"
    port: int = 8004
    log_level: str = "INFO"
    skip_ssl_verify: bool = False


@dataclass
class STTConfig:
    """STT service configuration"""
    url: str = "http://tara-task-stt-vad:8001"
    type: str = "stt-vad"
    streaming: bool = True
    language_detection: bool = True


@dataclass
class TTSVoiceConfig:
    """TTS voice configuration for a language"""
    voice_id: str = "default"
    language: str = "en-US"


@dataclass
class TTSConfig:
    """TTS service configuration"""
    url: str = "http://tara-task-tts-labs:8006"
    type: str = "elevenlabs"
    streaming: bool = True
    streaming_mode: str = "continuous"  # "buffered" or "continuous"
    voices: Dict[str, TTSVoiceConfig] = field(default_factory=dict)


@dataclass
class RAGConfig:
    """RAG service configuration"""
    url: str = "http://rag-service:8003"
    stream_endpoint: str = "/api/v1/stream_query"
    knowledge_base_path: str = ""
    top_k: int = 5
    similarity_threshold: float = 0.7
    enable_incremental: bool = True
    language: str = "german"


@dataclass
class IntentConfig:
    """Intent service configuration"""
    url: str = "http://intent-service:8002"
    enabled: bool = False


@dataclass
class RedisConfig:
    """Redis configuration"""
    url: str = "redis://redis:6379/0"
    host: str = "redis"
    port: int = 6379
    db: int = 0
    enabled: bool = True


@dataclass
class ServicesConfig:
    """All service configurations"""
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    intent: IntentConfig = field(default_factory=IntentConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    davinciai_backend_url: str = "https://api.enterprise.davinciai.eu:8450/api/webhooks/session"


@dataclass
class LanguageConfig:
    """Language configuration"""
    default: str = "en"
    supported: List[str] = field(default_factory=lambda: ["en", "de"])
    auto_detect: bool = True
    stream_out: str = "auto"  # "en", "de", or "auto" - Controls agent's TTS language and audio file selection
    disable_pregenerated_audio: bool = True  # If True, forces TTS for all dialogues


@dataclass
class OrganizationConfig:
    """Organization configuration"""
    name: str = "General Agent"
    full_name: str = "DaVinci AI Assistant"
    knowledge_base_path: str = ""
    # Identity (Env Var Overrides)
    agent_id: str = "agent-demo-001"
    agent_name: str = "demo"
    tenant_id: str = "5fc3fa72-d15d-48dc-812c-5c845b5172eb"


@dataclass
class SessionConfig:
    """Session configuration"""
    timeout_seconds: float = 30.0
    max_timeout_prompts: int = 3
    ttl_seconds: int = 3600
    ignore_stt_while_speaking: bool = True


@dataclass
class PerformanceConfig:
    """Performance configuration"""
    enable_incremental_rag: bool = True
    prewarm_on_vad: bool = False
    max_concurrent_sessions: int = 100
    enable_fillers: bool = True  # If False, suppresses all filler/latency audio


@dataclass
class OrchestratorConfig:
    """Complete configuration for the Orchestrator"""
    server: ServerConfig = field(default_factory=ServerConfig)
    organization: OrganizationConfig = field(default_factory=OrganizationConfig)
    languages: LanguageConfig = field(default_factory=LanguageConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    dialogue: Dict[str, Dict[str, List[Dict[str, Any]]]] = field(default_factory=dict)
    session: SessionConfig = field(default_factory=SessionConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)


class ConfigLoader:
    """Loads and validates YAML configuration"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize config loader
        
        Args:
            config_path: Path to config.yaml file. If None, looks for config.yaml
                         in the same directory as this file.
        """
        if config_path is None:
            # Default to config.yaml in orchestra_daytona directory
            script_dir = Path(__file__).parent
            config_path = script_dir / "config.yaml"
        
        self.config_path = Path(config_path)
        self.config: Optional[OrchestratorConfig] = None
    
    def load(self) -> OrchestratorConfig:
        """Load configuration from YAML file"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        
        logger.info(f"Loading configuration from: {self.config_path}")
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        if not data:
            raise ValueError("Configuration file is empty or invalid")
        
        # Parse configuration
        self.config = self._parse_config(data)
        
        # Validate configuration
        self._validate_config(self.config)
        
        logger.info("Configuration loaded successfully")
        logger.info(f"  Organization: {self.config.organization.name}")
        logger.info(f"  Default Language: {self.config.languages.default}")
        logger.info(f"  Supported Languages: {', '.join(self.config.languages.supported)}")
        logger.info(f"  STT Service: {self.config.services.stt.url}")
        logger.info(f"  TTS Service: {self.config.services.tts.url}")
        logger.info(f"  RAG Service: {self.config.services.rag.url}")
        
        return self.config
    
    def _parse_config(self, data: Dict[str, Any]) -> OrchestratorConfig:
        """Parse YAML data into configuration objects with environment variable overrides"""
        config = OrchestratorConfig()
        
        # Server config (with env var overrides)
        if "server" in data:
            server_data = data["server"]
            config.server = ServerConfig(
                host=os.getenv("SERVER_HOST", server_data.get("host", "0.0.0.0")),
                port=int(os.getenv("SERVER_PORT", server_data.get("port", 8004))),
                log_level=os.getenv("LOG_LEVEL", server_data.get("log_level", "INFO")),
                # FORCE DEFAULT TRUE for dev environment robustness
                skip_ssl_verify=os.getenv("ORCHESTRATOR_SKIP_SSL_VERIFY", str(server_data.get("skip_ssl_verify", "true"))).lower() == "true"
            )
        
        # Organization config (with env var overrides)
        if "organization" in data:
            org_data = data["organization"]
            config.organization = OrganizationConfig(
                name=os.getenv("ORGANIZATION_NAME", org_data.get("name", "General Agent")),
                full_name=os.getenv("ORGANIZATION_FULL_NAME", org_data.get("full_name", "DaVinci AI Assistant")),
                knowledge_base_path=os.getenv("KNOWLEDGE_BASE_PATH", org_data.get("knowledge_base_path", "")),
                # New identity fields with env overrides
                agent_id=os.getenv("AGENT_ID", org_data.get("agent_id", "davinci-demo-agent-001")),
                agent_name=os.getenv("AGENT_NAME", org_data.get("agent_name", "demo")),
                tenant_id=os.getenv("TENANT_ID", org_data.get("tenant_id", "5fc3fa72-d15d-48dc-812c-5c845b5172eb"))
            )
        
        # Language config (with env var overrides)
        if "languages" in data:
            lang_data = data["languages"]
            supported_langs = os.getenv("SUPPORTED_LANGUAGES", ",".join(lang_data.get("supported", ["en", "de"])))
            stream_out = os.getenv("STREAM_OUT", lang_data.get("stream_out", "auto")).lower()
            if stream_out not in ["en", "de", "auto"]:
                logger.warning(f"Invalid stream_out value '{stream_out}', defaulting to 'auto'")
                stream_out = "auto"
            config.languages = LanguageConfig(
                default=os.getenv("DEFAULT_LANGUAGE", lang_data.get("default", "en")),
                supported=[lang.strip() for lang in supported_langs.split(",")],
                auto_detect=os.getenv("AUTO_DETECT_LANGUAGE", str(lang_data.get("auto_detect", True))).lower() == "true",
                stream_out=stream_out,
                disable_pregenerated_audio=os.getenv("DISABLE_PREGENERATED_AUDIO", str(lang_data.get("disable_pregenerated_audio", True))).lower() == "true"
            )
        
        # Services config
        if "services" in data:
            services_data = data["services"]
            
            # STT config (with env var overrides)
            if "stt" in services_data:
                stt_data = services_data["stt"]
                stt_url = os.getenv("STT_SERVICE_URL", stt_data.get("url", "http://tara-task-stt-vad:8001"))
                # FORCE FIX: Ensure internal STT uses HTTPS
                if "stt:8002" in stt_url and stt_url.startswith("http:"):
                    stt_url = stt_url.replace("http:", "https:")
                    logger.info(f"🔧 Auto-corrected STT URL to: {stt_url}")
                    
                config.services.stt = STTConfig(
                    url=stt_url,
                    type=os.getenv("STT_TYPE", stt_data.get("type", "stt-vad")),
                    streaming=os.getenv("STT_STREAMING", str(stt_data.get("streaming", True))).lower() == "true",
                    language_detection=os.getenv("STT_LANGUAGE_DETECTION", str(stt_data.get("language_detection", True))).lower() == "true"
                )
            
            # TTS config (with env var overrides)
            if "tts" in services_data:
                tts_data = services_data["tts"]
                voices = {}
                if "voices" in tts_data:
                    for lang, voice_data in tts_data["voices"].items():
                        voices[lang] = TTSVoiceConfig(
                            voice_id=voice_data.get("voice_id", "default"),
                            language=voice_data.get("language", f"{lang}-{lang.upper()}")
                        )
                
                config.services.tts = TTSConfig(
                    url=os.getenv("TTS_SERVICE_URL", tts_data.get("url", "http://tara-task-tts-labs:8006")),
                    type=os.getenv("TTS_TYPE", tts_data.get("type", "elevenlabs")),
                    streaming=os.getenv("TTS_STREAMING", str(tts_data.get("streaming", True))).lower() == "true",
                    streaming_mode=os.getenv("TTS_STREAMING_MODE", tts_data.get("streaming_mode", "continuous")),
                    voices=voices
                )
            
            # RAG config (with env var overrides)
            if "rag" in services_data:
                rag_data = services_data["rag"]
                rag_url = os.getenv("RAG_SERVICE_URL", rag_data.get("url", "http://rag-service:8003"))
                # FORCE FIX: Ensure internal RAG uses HTTPS
                if "rag:8003" in rag_url and rag_url.startswith("http:"):
                    rag_url = rag_url.replace("http:", "https:")
                    logger.info(f"🔧 Auto-corrected RAG URL to: {rag_url}")
                    
                config.services.rag = RAGConfig(
                    url=rag_url,
                    stream_endpoint=os.getenv("RAG_STREAM_ENDPOINT", rag_data.get("stream_endpoint", "/api/v1/stream_query")),
                    knowledge_base_path=os.getenv("KNOWLEDGE_BASE_PATH", rag_data.get("knowledge_base_path", "")),
                    top_k=int(os.getenv("RAG_TOP_K", rag_data.get("top_k", 5))),
                    similarity_threshold=float(os.getenv("RAG_SIMILARITY_THRESHOLD", rag_data.get("similarity_threshold", 0.7))),
                    enable_incremental=os.getenv("ENABLE_INCREMENTAL_RAG", str(rag_data.get("enable_incremental", True))).lower() == "true",
                    language=os.getenv("RAG_LANGUAGE", rag_data.get("language", "german"))
                )
            
            # Intent config (with env var overrides)
            if "intent" in services_data:
                intent_data = services_data["intent"]
                config.services.intent = IntentConfig(
                    url=os.getenv("INTENT_SERVICE_URL", intent_data.get("url", "http://intent-service:8002")),
                    enabled=os.getenv("INTENT_ENABLED", str(intent_data.get("enabled", False))).lower() == "true"
                )
            
            # Redis config (with env var overrides)
            if "redis" in services_data:
                redis_data = services_data["redis"]
                redis_url = os.getenv("REDIS_URL", redis_data.get("url", "redis://redis:6379/0"))
                config.services.redis = RedisConfig(
                    url=redis_url,
                    host=os.getenv("REDIS_HOST", redis_data.get("host", "redis")),
                    port=int(os.getenv("REDIS_PORT", redis_data.get("port", 6379))),
                    db=int(os.getenv("REDIS_DB", redis_data.get("db", 0))),
                    enabled=os.getenv("REDIS_ENABLED", str(redis_data.get("enabled", True))).lower() == "true"
                )
            
            # Davinciai Backend config (with env var overrides)
            config.services.davinciai_backend_url = os.getenv(
                "DAVINCIAI_BACKEND_URL", 
                services_data.get("davinciai_backend_url", config.services.davinciai_backend_url)
            )
        
        # Dialogue config (keep as raw dict for flexibility)
        if "dialogue" in data:
            config.dialogue = data["dialogue"]
        
        # Session config (with env var overrides)
        if "session" in data:
            session_data = data["session"]
            config.session = SessionConfig(
                timeout_seconds=float(os.getenv("SESSION_TIMEOUT_SECONDS", session_data.get("timeout_seconds", 10.0))),
                max_timeout_prompts=int(os.getenv("MAX_TIMEOUT_PROMPTS", session_data.get("max_timeout_prompts", 3))),
                ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", session_data.get("ttl_seconds", 3600))),
                ignore_stt_while_speaking=os.getenv("IGNORE_STT_WHILE_SPEAKING", str(session_data.get("ignore_stt_while_speaking", True))).lower() == "true"
            )
        
        # Performance config (with env var overrides)
        if "performance" in data:
            perf_data = data["performance"]
            config.performance = PerformanceConfig(
                enable_incremental_rag=os.getenv("ENABLE_INCREMENTAL_RAG", str(perf_data.get("enable_incremental_rag", True))).lower() == "true",
                prewarm_on_vad=os.getenv("PREWARM_ON_VAD", str(perf_data.get("prewarm_on_vad", False))).lower() == "true",
                max_concurrent_sessions=int(os.getenv("MAX_CONCURRENT_SESSIONS", perf_data.get("max_concurrent_sessions", 100))),
                enable_fillers=os.getenv("ENABLE_FILLERS", str(perf_data.get("enable_fillers", "true"))).lower() == "true"
            )
        
        return config
    
    def _validate_config(self, config: OrchestratorConfig):
        """Validate configuration"""
        # Validate default language is in supported languages
        if config.languages.default not in config.languages.supported:
            raise ValueError(
                f"Default language '{config.languages.default}' not in supported languages: "
                f"{config.languages.supported}"
            )
        
        # Validate TTS voices exist for all supported languages
        for lang in config.languages.supported:
            if lang not in config.services.tts.voices:
                logger.warning(
                    f"No TTS voice configured for language '{lang}'. "
                    f"Using default voice."
                )
        
        # Validate dialogue entries exist for all supported languages
        for lang in config.languages.supported:
            if lang not in config.dialogue:
                logger.warning(
                    f"No dialogue configuration found for language '{lang}'. "
                    f"Some features may not work correctly."
                )
        
        # Validate knowledge base path if provided
        if config.organization.knowledge_base_path:
            kb_path = Path(config.organization.knowledge_base_path)
            if not kb_path.exists():
                logger.warning(
                    f"Knowledge base path does not exist: {kb_path}. "
                    f"RAG service may not work correctly."
                )
        
        # Validate RAG configuration
        if config.services.rag.similarity_threshold < 0.0 or config.services.rag.similarity_threshold > 1.0:
            raise ValueError(f"RAG similarity_threshold must be between 0.0 and 1.0 (got {config.services.rag.similarity_threshold})")
        if config.services.rag.top_k <= 0:
            raise ValueError(f"RAG top_k must be greater than 0 (got {config.services.rag.top_k})")

        # Validate Session configuration
        if config.session.timeout_seconds <= 0:
            raise ValueError(f"Session timeout_seconds must be positive (got {config.session.timeout_seconds})")
            
        # Validate Server configuration
        if config.server.port < 1 or config.server.port > 65535:
            raise ValueError(f"Server port must be between 1 and 65535 (got {config.server.port})")

        logger.info("Configuration validation passed")


def load_config(config_path: Optional[str] = None) -> OrchestratorConfig:
    """
    Convenience function to load configuration
    
    Args:
        config_path: Path to config.yaml file. If None, uses default location.
    
    Returns:
        OrchestratorConfig instance
    """
    loader = ConfigLoader(config_path)
    return loader.load()




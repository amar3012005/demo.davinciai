"""
Multi-Language Dialogue Manager for Orchestra-daytona

Manages pre-defined dialogues (intro, exit, fillers, timeouts) for multiple languages.
Loads configuration from YAML config file.
"""

import os
import json
import random
import logging
from enum import Enum
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class DialogueType(Enum):
    """Types of pre-defined dialogues"""
    INTRO = "intro"
    EXIT = "exit"
    FILLER_IMMEDIATE = "filler_immediate"
    FILLER_LATENCY = "filler_latency"
    TIMEOUT = "timeout"
    UNCLEAR = "unclear"
    POST_RESPONSE = "post_response"


@dataclass
class DialogueAsset:
    """Represents a dialogue asset with text, emotion, and optional audio"""
    text: str
    emotion: str = "helpful"
    audio_path: Optional[str] = None
    trigger: Optional[str] = None  # e.g., 'immediate', 'delay_ms:1500', 'timeout_ms:10000'
    keywords: Optional[List[str]] = None

    def has_audio(self) -> bool:
        """Check if audio file exists"""
        if not self.audio_path:
            return False
        return os.path.exists(self.audio_path)


class MultiLangDialogueManager:
    """
    Manages pre-defined dialogues for multiple languages.
    
    Configuration is loaded from YAML config file (dialogue section).
    Supports both pre-synthesized audio files and text-to-speech fallback.
    """

    def __init__(self, dialogue_config: Optional[Dict[str, Dict[str, List[Dict[str, Any]]]]] = None, 
                 assets_dir: Optional[str] = None,
                 disable_pregenerated_audio: bool = False):
        """
        Initialize Multi-Language Dialogue Manager
        
        Args:
            dialogue_config: Dialogue configuration dict from YAML (keyed by language).
                            If None, loads from JSON files in assets directory.
            assets_dir: Base assets directory for audio files (optional)
        """
        # Determine base assets directory
        if assets_dir:
            self.base_assets_dir = Path(assets_dir)
        else:
            # Default to orchestra_daytona/assets/
            script_dir = Path(__file__).parent.parent
            self.base_assets_dir = script_dir / "assets"
        
        # Audio assets live under assets/audio
        self.audio_dir = self.base_assets_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        
        # Load dialogue config from JSON files if not provided
        if dialogue_config is None:
            dialogue_config = self._load_from_json_files()
        
        self.dialogue_config = dialogue_config
        self.disable_pregenerated_audio = disable_pregenerated_audio
        
        # Cache for parsed dialogue assets
        self._cache: Dict[str, Dict[DialogueType, List[DialogueAsset]]] = {}
        
        # Exit keywords (collected from exit dialogues)
        self.exit_keywords: Dict[str, List[str]] = {}
        
        # Parse all languages
        self._parse_all_languages()
        
        logger.info(f"MultiLangDialogueManager initialized")
        logger.info(f"  Assets directory: {self.base_assets_dir}")
        logger.info(f"  Audio directory: {self.audio_dir}")
        logger.info(f"  Languages: {', '.join(self.dialogue_config.keys())}")
    
    def _load_from_json_files(self) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """Load dialogue configurations from JSON files"""
        dialogue_config = {}
        
        # Try to load English and German JSON files
        for lang in ["en", "de"]:
            json_path = self.base_assets_dir / f"dialogues_{lang}.json"
            if json_path.exists():
                try:
                    with json_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                        dialogue_config[lang] = data.get("dialogues", {})
                        logger.info(f"Loaded dialogue config for language '{lang}' from {json_path}")
                except Exception as e:
                    logger.warning(f"Failed to load {json_path}: {e}")
            else:
                logger.warning(f"Dialogue JSON not found: {json_path}")
        
        return dialogue_config

    def _parse_all_languages(self):
        """Parse dialogue configuration for all languages"""
        for lang, lang_dialogues in self.dialogue_config.items():
            self._cache[lang] = {}
            self.exit_keywords[lang] = []
            
            # Parse each dialogue type
            for dialogue_type_str, items in lang_dialogues.items():
                try:
                    dialogue_type = DialogueType(dialogue_type_str)
                except ValueError:
                    logger.warning(f"Unknown dialogue type: {dialogue_type_str}")
                    continue
                
                assets = []
                for item in items:
                    # Resolve audio file path
                    audio_path = None
                    if "audio_file" in item:
                        audio_file = item["audio_file"]
                        # Try relative to audio_dir first
                        audio_path = self.audio_dir / audio_file
                        if not audio_path.exists():
                            # Try absolute path
                            audio_path = Path(audio_file) if os.path.isabs(audio_file) else None
                    
                    # Set audio_path only if file exists and not disabled
                    final_audio_path = None
                    if not self.disable_pregenerated_audio and audio_path and audio_path.exists():
                        final_audio_path = str(audio_path)
                    elif audio_path and not self.disable_pregenerated_audio:
                        logger.debug(f"Audio file not found: {audio_path} (expected for {dialogue_type_str})")
                    
                    asset = DialogueAsset(
                        text=item.get("text", ""),
                        emotion=item.get("emotion", "helpful"),
                        audio_path=final_audio_path,
                        trigger=item.get("trigger"),
                        keywords=item.get("keywords")
                    )
                    assets.append(asset)
                    
                    # Collect exit keywords
                    if dialogue_type == DialogueType.EXIT and asset.keywords:
                        self.exit_keywords[lang].extend(asset.keywords)
                
                self._cache[lang][dialogue_type] = assets
            
            # Remove duplicates from exit keywords
            self.exit_keywords[lang] = list(set(self.exit_keywords[lang]))
            
            logger.debug(f"Parsed {len(self._cache[lang])} dialogue types for language '{lang}'")
            
            # Log audio file availability for debugging
            for dialogue_type, assets in self._cache[lang].items():
                assets_with_audio = sum(1 for a in assets if a.has_audio())
                total_assets = len(assets)
                if total_assets > 0:
                    logger.debug(f"  {dialogue_type.value}: {assets_with_audio}/{total_assets} assets have audio files")

    def get_asset(self, dialogue_type: DialogueType, language: str = "en") -> Optional[DialogueAsset]:
        """
        Get a random dialogue asset of the specified type and language
        
        Args:
            dialogue_type: Type of dialogue
            language: Language code (default: 'en')
        
        Returns:
            DialogueAsset or None if not found
        """
        if language not in self._cache:
            logger.warning(f"Language '{language}' not found, falling back to 'en'")
            language = "en"
        
        if dialogue_type not in self._cache[language]:
            logger.warning(f"Dialogue type '{dialogue_type.value}' not found for language '{language}'")
            return None
        
        assets = self._cache[language][dialogue_type]
        if not assets:
            return None
        
        return random.choice(assets)

    def get_intro(self, language: str = "en") -> Optional[DialogueAsset]:
        """
        Get intro greeting for language.
        Prefers intro assets with audio files available.
        """
        if language not in self._cache:
            language = "en"
        
        intros = self._cache.get(language, {}).get(DialogueType.INTRO, [])
        if not intros:
            return None
        
        # Filter to only intros that have audio files available
        intros_with_audio = [i for i in intros if i.audio_path and i.has_audio()]
        
        # If we have intros with audio, randomly select one
        if intros_with_audio:
            selected_intro = random.choice(intros_with_audio)
            logger.debug(f"Selected intro with audio: {selected_intro.text[:30]}...")
            return selected_intro
        
        # Fallback: use any intro (even without audio)
        selected_intro = random.choice(intros)
        logger.debug(f"Selected intro without audio: {selected_intro.text[:30]}...")
        return selected_intro

    def get_exit(self, language: str = "en") -> Optional[DialogueAsset]:
        """
        Get exit message for language.
        Prefers exit assets with audio files available.
        """
        if language not in self._cache:
            language = "en"
        
        exits = self._cache.get(language, {}).get(DialogueType.EXIT, [])
        if not exits:
            return None
        
        # Filter to only exits that have audio files available
        exits_with_audio = [e for e in exits if e.audio_path and e.has_audio()]
        
        # If we have exits with audio, randomly select one
        if exits_with_audio:
            selected_exit = random.choice(exits_with_audio)
            logger.debug(f"Selected exit with audio: {selected_exit.text[:30]}...")
            return selected_exit
        
        # Fallback: use any exit (even without audio)
        selected_exit = random.choice(exits)
        logger.debug(f"Selected exit without audio: {selected_exit.text[:30]}...")
        return selected_exit

    def get_immediate_filler(self, language: str = "en") -> Optional[DialogueAsset]:
        """
        Get immediate filler phrase for language.
        Randomly selects from ALL available fillers to ensure variety.
        Audio files are preferred when available, but all fillers get equal chance.
        """
        if language not in self._cache:
            language = "en"
        
        fillers = self._cache.get(language, {}).get(DialogueType.FILLER_IMMEDIATE, [])
        if not fillers:
            return None
        
        # Randomize from ALL available fillers to ensure variety
        # This ensures all phrases are used, not just ones with audio files
        selected_filler = random.choice(fillers)
        
        if selected_filler.has_audio():
            logger.debug(f"Selected immediate filler with audio: {selected_filler.text[:30]}...")
        else:
            logger.debug(f"Selected immediate filler (will use TTS): {selected_filler.text[:30]}...")
        
        return selected_filler

    def get_latency_filler(self, language: str = "en") -> Optional[DialogueAsset]:
        """
        Get latency filler phrase for language.
        Randomly selects from ALL available fillers to ensure variety.
        Audio files are preferred when available, but all fillers get equal chance.
        """
        if language not in self._cache:
            language = "en"
        
        fillers = self._cache.get(language, {}).get(DialogueType.FILLER_LATENCY, [])
        if not fillers:
            return None
        
        # Randomize from ALL available fillers to ensure variety
        # This ensures all phrases are used, not just ones with audio files
        selected_filler = random.choice(fillers)
        
        if selected_filler.has_audio():
            logger.debug(f"Selected latency filler with audio: {selected_filler.text[:30]}...")
        else:
            logger.debug(f"Selected latency filler (will use TTS): {selected_filler.text[:30]}...")
        
        return selected_filler

    def get_timeout_prompt(self, language: str = "en") -> Optional[DialogueAsset]:
        """
        Get timeout prompt for language.
        Prefers timeout assets with audio files available.
        """
        if language not in self._cache:
            language = "en"
        
        timeouts = self._cache.get(language, {}).get(DialogueType.TIMEOUT, [])
        if not timeouts:
            return None
        
        # Filter to only timeouts that have audio files available
        timeouts_with_audio = [t for t in timeouts if t.audio_path and t.has_audio()]
        
        # If we have timeouts with audio, randomly select one
        if timeouts_with_audio:
            selected_timeout = random.choice(timeouts_with_audio)
            logger.debug(f"Selected timeout with audio: {selected_timeout.text[:30]}...")
            return selected_timeout
        
        # Fallback: use any timeout (even without audio)
        selected_timeout = random.choice(timeouts)
        logger.debug(f"Selected timeout without audio: {selected_timeout.text[:30]}...")
        return selected_timeout

    def get_unclear_prompt(self, language: str = "en") -> Optional[DialogueAsset]:
        """
        Get unclear prompt for language.
        Prefers unclear assets with audio files available.
        """
        if language not in self._cache:
            language = "en"
        
        unclear_prompts = self._cache.get(language, {}).get(DialogueType.UNCLEAR, [])
        if not unclear_prompts:
            return None
        
        # Filter to only unclear prompts that have audio files available
        unclear_with_audio = [u for u in unclear_prompts if u.audio_path and u.has_audio()]
        
        # If we have unclear prompts with audio, randomly select one
        if unclear_with_audio:
            selected_unclear = random.choice(unclear_with_audio)
            logger.debug(f"Selected unclear prompt with audio: {selected_unclear.text[:30]}...")
            return selected_unclear
        
        # Fallback: use any unclear prompt (even without audio)
        selected_unclear = random.choice(unclear_prompts)
        logger.debug(f"Selected unclear prompt without audio: {selected_unclear.text[:30]}...")
        return selected_unclear

    def get_post_response_prompt(self, language: str = "en") -> Optional[DialogueAsset]:
        """
        Get post-response prompt for language (e.g., "Do you need anything else?").
        Prefers prompts with audio files available.
        """
        if language not in self._cache:
            language = "en"
        
        post_response_prompts = self._cache.get(language, {}).get(DialogueType.POST_RESPONSE, [])
        if not post_response_prompts:
            return None
        
        # Filter to only prompts that have audio files available
        prompts_with_audio = [p for p in post_response_prompts if p.audio_path and p.has_audio()]
        
        # If we have prompts with audio, randomly select one
        if prompts_with_audio:
            selected_prompt = random.choice(prompts_with_audio)
            logger.debug(f"Selected post-response prompt with audio: {selected_prompt.text[:30]}...")
            return selected_prompt
        
        # Fallback: use any prompt (even without audio)
        selected_prompt = random.choice(post_response_prompts)
        logger.debug(f"Selected post-response prompt without audio: {selected_prompt.text[:30]}...")
        return selected_prompt

    def check_exit_keywords(self, text: str, language: str = "en") -> bool:
        """
        Check if text contains exit keywords
        
        Args:
            text: Text to check
            language: Language code
        
        Returns:
            True if exit keywords found
        """
        if language not in self.exit_keywords:
            language = "en"
        
        text_lower = text.lower().strip()
        keywords = self.exit_keywords.get(language, [])
        
        return any(keyword.lower() in text_lower for keyword in keywords)

    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages"""
        return list(self.dialogue_config.keys())




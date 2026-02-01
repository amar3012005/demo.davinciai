"""
Language Detection Utility for Orchestra-daytona

Detects language from text using simple heuristics or external libraries.
"""

import re
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# Common German words and patterns
GERMAN_PATTERNS = [
    r'\b(der|die|das|und|ist|für|auf|mit|zu|von|sich|nicht|kann|wird|haben|sind|eine|einen|einem|einer)\b',
    r'\b(Universität|Hannover|Studium|Student|Studierende|Fakultät|Semester|Prüfung|Vorlesung)\b',
    r'\b(bitte|danke|hallo|guten|Tag|Morgen|Abend|wie|was|wo|wann|warum|wie|können|möchte)\b',
]

# Common English words and patterns
ENGLISH_PATTERNS = [
    r'\b(the|and|is|for|on|with|to|of|a|an|in|that|have|are|as|be|this|from|or|one|had|by|word|but|not|what|all|were|we|when|your|can|said|there|each|which|she|do|how|their|if|will|up|other|about|out|many|then|them|these|so|some|her|would|make|like|into|him|has|two|more|go|no|way|could|my|than|first|been|call|who|its|now|find|long|down|day|did|get|come|made|may|part)\b',
]


def detect_language(text: str, supported_languages: List[str] = None) -> str:
    """
    Detect language from text using pattern matching
    
    Args:
        text: Input text to analyze
        supported_languages: List of supported language codes (default: ['en', 'de'])
    
    Returns:
        Language code ('en' or 'de')
    """
    if supported_languages is None:
        supported_languages = ['en', 'de']
    
    if not text or not text.strip():
        return supported_languages[0] if supported_languages else 'en'
    
    text_lower = text.lower()
    
    # FIRST: Check for explicit language switch requests
    if 'de' in supported_languages:
        german_requests = [
            'in german', 'auf deutsch', 'switch to german', 'to german',
            'can you speak german', 'repeat in german', 'say that in german',
            'translate to german', 'in deutsch', 'wechsel zu deutsch'
        ]
        if any(req in text_lower for req in german_requests):
            logger.info(f"🔄 Explicit German language switch detected in: '{text[:50]}'")
            return 'de'
    
    if 'en' in supported_languages:
        english_requests = [
            'in english', 'auf englisch', 'switch to english', 'to english',
            'can you speak english', 'repeat in english', 'say that in english',
            'translate to english', 'wechsel zu englisch'
        ]
        if any(req in text_lower for req in english_requests):
            logger.info(f"🔄 Explicit English language switch detected in: '{text[:50]}'")
            return 'en'
    
    # THEN: Count matches for each language (word-based detection)
    german_score = 0
    english_score = 0
    
    # Check German patterns
    if 'de' in supported_languages:
        for pattern in GERMAN_PATTERNS:
            matches = len(re.findall(pattern, text_lower, re.IGNORECASE))
            german_score += matches
    
    # Check English patterns
    if 'en' in supported_languages:
        for pattern in ENGLISH_PATTERNS:
            matches = len(re.findall(pattern, text_lower, re.IGNORECASE))
            english_score += matches
    
    # Check for German-specific characters
    if 'de' in supported_languages:
        if re.search(r'[äöüÄÖÜß]', text):
            german_score += 5
    
    # Determine language
    if german_score > english_score and 'de' in supported_languages:
        detected = 'de'
    elif english_score > 0 and 'en' in supported_languages:
        detected = 'en'
    else:
        # Default to first supported language
        detected = supported_languages[0] if supported_languages else 'en'
    
    logger.debug(f"Language detection: text='{text[:50]}...' | scores: de={german_score}, en={english_score} | detected={detected}")
    
    return detected


def detect_language_from_metadata(metadata: dict) -> Optional[str]:
    """
    Extract language from STT metadata if available
    
    Args:
        metadata: STT metadata dict
    
    Returns:
        Language code if found, None otherwise
    """
    if not metadata:
        return None
    
    # Check common metadata fields
    lang = metadata.get('language') or metadata.get('lang') or metadata.get('detected_language')
    
    if lang:
        # Normalize language code
        lang = lang.lower()
        if lang.startswith('de'):
            return 'de'
        elif lang.startswith('en'):
            return 'en'
    
    return None











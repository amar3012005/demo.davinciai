"""
Simple Appointment FSM (Expert Assistant)

A lightweight 3-question appointment booking flow:
1. Name (with spelling confirmation)
2. Email (with spelling confirmation)
3. Query/Topic (what they want to discuss)

Features:
- 2 retry fallbacks per field
- Spelling confirmation (yes/no)
- LEXI persona (friendly, expert)
- JSON storage
- Schema-driven configuration (optional)
"""

import json
import logging
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


# Default V1 Schema (used if no schema provided)
DEFAULT_V1_SCHEMA = {
    "fields": {
        "name": {
            "required": True,
            "collect_prompt": "Please spell out your name letter by letter. For example, 'J-O-H-N S-M-I-T-H'.",
            "confirm_prompt": "Is your name correct?",
            "min_length": 2,
            "max_length": 50,
            "validation_regex": r"^[a-zA-Z\s\-']+$"
        },
        "email": {
            "required": True,
            "collect_prompt": "Please spell out your email address. For example, 'J-O-H-N at G-M-A-I-L dot C-O-M'.",
            "confirm_prompt": "Is your email correct?",
            "validation_regex": r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        },
        "topic": {
            "required": True,
            "collect_prompt": "What would you like to discuss with our expert? Just a brief description is fine.",
            "confirm_prompt": "Is this topic correct?",
            "min_length": 5,
            "max_length": 500
        }
    },
    "cancel_keywords": ["cancel", "stop", "nevermind", "forget it", "quit", "exit"],
    "max_retries": 3,
    "fallback_messages": {
        "retry": "Could you please repeat that?",
        "max_retries": "I'm having trouble understanding. No worries! Feel free to ask me anything, or we can try booking an appointment later."
    }
}


class AppointmentState(Enum):
    """FSM states for appointment booking"""
    INIT = "init"
    COLLECT_NAME = "collect_name"
    CONFIRM_NAME = "confirm_name"
    COLLECT_EMAIL = "collect_email"
    CONFIRM_EMAIL = "confirm_email"
    COLLECT_QUERY = "collect_query"
    CONFIRM_QUERY = "confirm_query"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass
class AppointmentData:
    """Data collected during appointment booking"""
    id: str = field(default_factory=lambda: f"apt_{uuid.uuid4().hex[:8]}")
    name: Optional[str] = None
    email: Optional[str] = None
    query: Optional[str] = None
    timestamp: Optional[str] = None
    status: str = "pending"
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SimpleAppointmentFSM:
    """
    Simple 3-question FSM for booking expert appointments.
    
    Flow:
    1. Greeting → Collect Name → Confirm Name (spell it out)
    2. Collect Email → Confirm Email (spell it out)
    3. Collect Query → Confirm Query
    4. Complete → Save to JSON
    """
    
    MAX_RETRIES = 3
    DATA_FILE = "/app/data/appointments.json"
    
    # Cancel keywords
    CANCEL_KEYWORDS = ["cancel", "stop", "nevermind", "forget it", "quit", "exit"]
    
    def __init__(self, schema: Optional[Dict[str, Any]] = None):
        # Merge user-provided schema over defaults (shallow merge by top-level keys, per-field merge below)
        self.schema = deepcopy(DEFAULT_V1_SCHEMA)
        if schema and isinstance(schema, dict):
            if "fields" in schema and isinstance(schema["fields"], dict):
                for key, value in schema["fields"].items():
                    if key not in self.schema["fields"]:
                        self.schema["fields"][key] = value
                    elif isinstance(value, dict):
                        self.schema["fields"][key].update(value)
            for key in ("cancel_keywords", "max_retries", "fallback_messages"):
                if key in schema and schema[key]:
                    self.schema[key] = schema[key]

        self.state = AppointmentState.INIT
        self.data = AppointmentData()
        self.retry_counts: Dict[str, int] = {}
        self.max_retries: int = int(self.schema.get("max_retries", self.MAX_RETRIES))
        self.cancel_keywords: list = list(self.schema.get("cancel_keywords", self.CANCEL_KEYWORDS))
        
        logger.info("🗓️ SimpleAppointmentFSM initialized (schema-driven)")

    def _field_cfg(self, field: str) -> Dict[str, Any]:
        return self.schema.get("fields", {}).get(field, {})

    def _fallback_message(self, key: str, default: str) -> str:
        return self.schema.get("fallback_messages", {}).get(key, default)
    
    def process_input(self, user_input: str) -> Dict[str, Any]:
        """
        Process user input based on current FSM state.
        
        Returns:
            Dict with: response, state, complete, cancelled, data
        """
        previous_state = self.state
        user_input = user_input.strip()
        
        # Check for cancellation
        if any(keyword in user_input.lower() for keyword in self.cancel_keywords):
            self.state = AppointmentState.CANCELLED
            return {
                "response": "No problem! If you'd like to book an appointment later, just let me know. Feel free to ask me anything.",
                "state": self.state.value,
                "complete": False,
                "cancelled": True,
                "data": None
            }
        
        try:
            # Route to appropriate handler
            if self.state == AppointmentState.INIT:
                response = self._handle_init()
            elif self.state == AppointmentState.COLLECT_NAME:
                response = self._handle_collect_name(user_input)
            elif self.state == AppointmentState.CONFIRM_NAME:
                response = self._handle_confirm_name(user_input)
            elif self.state == AppointmentState.COLLECT_EMAIL:
                response = self._handle_collect_email(user_input)
            elif self.state == AppointmentState.CONFIRM_EMAIL:
                response = self._handle_confirm_email(user_input)
            elif self.state == AppointmentState.COLLECT_QUERY:
                response = self._handle_collect_query(user_input)
            elif self.state == AppointmentState.CONFIRM_QUERY:
                response = self._handle_confirm_query(user_input)
            elif self.state == AppointmentState.COMPLETE:
                response = "Your appointment request is already submitted! Is there anything else I can help you with?"
            else:
                response = "Let me start over with the appointment booking."
                self.state = AppointmentState.INIT
            
            return {
                "response": response,
                "state": self.state.value,
                "complete": self.state == AppointmentState.COMPLETE,
                "cancelled": self.state == AppointmentState.CANCELLED,
                "data": self.data.to_dict() if self.state == AppointmentState.COMPLETE else None
            }
            
        except Exception as e:
            logger.error(f"FSM error in state {self.state}: {e}")
            return {
                "response": "Oops, something went wrong. Could you repeat that?",
                "state": self.state.value,
                "complete": False,
                "cancelled": False,
                "data": None
            }
    
    # =========================================================================
    # State Handlers
    # =========================================================================
    
    def _handle_init(self) -> str:
        """Start the appointment booking flow"""
        self.state = AppointmentState.COLLECT_NAME
        name_prompt = self._field_cfg("name").get(
            "collect_prompt",
            "First, please spell out your name letter by letter. For example, 'J-O-H-N S-M-I-T-H'."
        )
        return (
            "Great! I'd be happy to help you connect with our experts. "
            "I just need a few quick details. You can say 'cancel' anytime to stop.\n\n"
            f"{name_prompt}"
        )
    
    def _handle_collect_name(self, user_input: str) -> str:
        """Collect user's name with progressive retry prompts"""
        # Clean input
        name = user_input.strip()
        
        # Handle spelled-out names like "A-M-A-R" or "A M A R" or "A, M, A, R"
        # Only treat as spelled-out if every alpha token is a single character.
        name_tokens = re.findall(r"[A-Za-z]+", name)
        if name_tokens and len(name_tokens) >= 2 and all(len(token) == 1 for token in name_tokens):
            name = ''.join(name_tokens).capitalize()
            logger.info(f"🔤 Detected spelled name: {name}")
        
        # Remove common prefixes (case-insensitive matching)
        prefixes = ["my name is", "i'm", "this is", "i am", "call me", "it's", "its", "it is", "name is"]
        name_lower = name.lower()
        for prefix in prefixes:
            if name_lower.startswith(prefix):
                name = name[len(prefix):].strip()
                logger.info(f"📝 Stripped prefix '{prefix}' -> remaining: '{name}'")
                break  # Only strip one prefix
        
        field_cfg = self._field_cfg("name")
        min_len = int(field_cfg.get("min_length", 2))
        max_len = int(field_cfg.get("max_length", 50))
        regex = field_cfg.get("validation_regex", r'^[a-zA-Z\s\-\']+$')

        # Validate: min/max and pattern
        if len(name) < min_len or len(name) > max_len or not re.match(regex, name):
            self.retry_counts['name'] = self.retry_counts.get('name', 0) + 1
            retry_num = self.retry_counts['name']
            
            if retry_num >= self.max_retries:
                self.state = AppointmentState.CANCELLED
                return self._fallback_message(
                    "max_retries",
                    "I'm having trouble understanding. No worries! Feel free to ask me anything , or we can try booking an appointment later."
                )
            
            # Progressive prompts based on retry attempt
            if retry_num == 1:
                return self._fallback_message("retry", "I didn't quite catch that. Could you tell me your name? For example, 'John Smith'.")
            elif retry_num == 2:
                return "Sorry, I'm still having trouble. Could you speak a bit slower? Just your first and last name please."
            else:
                return "One more try! Could you spell out your name letter by letter? For example, 'J-O-H-N'."
        
        # Store and confirm
        self.data.name = name.title()  # Capitalize properly
        self.retry_counts['name'] = 0
        self.state = AppointmentState.CONFIRM_NAME
        
        # Spell out name for confirmation
        spelled = self._spell_out(self.data.name)
        return f"Got it! Just to confirm, your name is {spelled}. Is that correct?"
    
    def _handle_confirm_name(self, user_input: str) -> str:
        """Confirm the collected name"""
        response_type = self._parse_yes_no(user_input)
        
        if response_type is True:
            # Confirmed - move to email
            self.state = AppointmentState.COLLECT_EMAIL
            email_prompt = self._field_cfg("email").get(
                "collect_prompt",
                "Now please spell out your email address. For example, 'J-O-H-N at G-M-A-I-L dot C-O-M'."
            )
            return f"Perfect, {self.data.name.split()[0]}! {email_prompt}"
        
        elif response_type is False:
            # Rejected - ask to spell it
            self.data.name = None
            self.state = AppointmentState.COLLECT_NAME
            return "No problem! Could you spell your name for me?"
        
        else:
            # Unclear - ask again
            spelled = self._spell_out(self.data.name)
            return f"I need a yes or no. Is your name {spelled}?"
    
    def _handle_collect_email(self, user_input: str) -> str:
        """Collect user's email"""
        # Clean input
        email = user_input.strip().lower()
        prefixes = ["my email is", "it's", "email:", "it is", "my email address is"]
        for prefix in prefixes:
            if email.startswith(prefix):
                email = email[len(prefix):].strip()
        
        # Parse spelled-out email like "J-O-H-N at G-M-A-I-L dot C-O-M"
        # Also handles: "john at gmail dot com", "j o h n at gmail dot com"
        email = self._parse_spelled_email(email)
        logger.info(f"📧 Parsed email: '{email}'")
        
        email_regex = self._field_cfg("email").get("validation_regex", r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
        # Simple email validation
        if not re.match(email_regex, email):
            self.retry_counts['email'] = self.retry_counts.get('email', 0) + 1
            
            if self.retry_counts['email'] >= self.max_retries:
                self.state = AppointmentState.CANCELLED
                return self._fallback_message(
                    "max_retries",
                    "I'm having trouble with the email format. No worries! Feel free to reach out anytime, or we can try again later."
                )
            
            return self._fallback_message(
                "retry",
                "That doesn't look like a valid email. Please spell it out letter by letter. For example, 'J-O-H-N at G-M-A-I-L dot C-O-M'."
            )
        
        # Store and confirm
        self.data.email = email
        self.retry_counts['email'] = 0
        self.state = AppointmentState.CONFIRM_EMAIL
        
        # Spell out email for confirmation
        spelled = self._spell_out_email(email)
        return f"Great! So your email is {spelled}. Is that correct?"
    
    def _handle_confirm_email(self, user_input: str) -> str:
        """Confirm the collected email"""
        response_type = self._parse_yes_no(user_input)
        
        if response_type is True:
            # Confirmed - move to query
            self.state = AppointmentState.COLLECT_QUERY
            topic_prompt = self._field_cfg("topic").get(
                "collect_prompt",
                "what would you like to discuss with our expert? Just a brief description is fine."
            )
            return f"Awesome! Last question: {topic_prompt}"
        
        elif response_type is False:
            # Rejected - ask to spell it
            self.data.email = None
            self.state = AppointmentState.COLLECT_EMAIL
            return "No problem! Could you spell your email for me?"
        
        else:
            # Unclear - ask again
            spelled = self._spell_out_email(self.data.email)
            return f"I need a yes or no. Is your email {spelled}?"
    
    def _handle_collect_query(self, user_input: str) -> str:
        """Collect the user's query/topic"""
        query = user_input.strip()
        
        field_cfg = self._field_cfg("topic")
        min_len = int(field_cfg.get("min_length", 5))
        max_len = int(field_cfg.get("max_length", 500))

        # Basic validation
        if len(query) < min_len:
            self.retry_counts['query'] = self.retry_counts.get('query', 0) + 1
            
            if self.retry_counts['query'] >= self.max_retries:
                self.state = AppointmentState.CANCELLED
                return self._fallback_message(
                    "max_retries",
                    "I need a bit more detail about what you'd like to discuss. No worries! Feel free to reach out when you're ready."
                )
            
            return self._fallback_message(
                "retry",
                "Could you tell me a bit more about what you'd like to discuss?"
            )
        
        # Truncate if too long
        if len(query) > max_len:
            query = query[:max_len]
        
        # Store and confirm
        self.data.query = query
        self.retry_counts['query'] = 0
        self.state = AppointmentState.CONFIRM_QUERY
        
        preview = query[:100] + "..." if len(query) > 100 else query
        return f"Got it! You want to discuss: '{preview}'. Is that correct?"
    
    def _handle_confirm_query(self, user_input: str) -> str:
        """Confirm the collected query"""
        response_type = self._parse_yes_no(user_input)
        
        if response_type is True:
            # Complete - save appointment
            self.data.timestamp = datetime.now().isoformat()
            self.state = AppointmentState.COMPLETE
            
            # Save to JSON
            self._save_appointment()
            
            return (
                f"Excellent! Your appointment request is submitted! 🎉\n\n"
                f"Here's a summary:\n"
                f"• Name: {self.data.name}\n"
                f"• Email: {self.data.email}\n"
                f"• Topic: {self.data.query[:50]}{'...' if len(self.data.query) > 50 else ''}\n\n"
                f"One of our experts will reach out to you at {self.data.email} within 24 hours.\n\n"
                f"Is there anything else I can help you with?"
            )
        
        elif response_type is False:
            # Rejected - re-collect query
            self.data.query = None
            self.state = AppointmentState.COLLECT_QUERY
            return "No problem! What would you like to discuss with our expert?"
        
        else:
            # Unclear - ask again
            preview = self.data.query[:50] + "..." if len(self.data.query) > 50 else self.data.query
            return f"I need a yes or no. Is your topic: '{preview}'?"
    
    # =========================================================================
    # Helper Methods
    # =========================================================================
    
    def _parse_yes_no(self, user_input: str) -> Optional[bool]:
        """Parse yes/no from user input"""
        text = user_input.lower().strip()
        
        yes_words = ["yes", "yeah", "yep", "correct", "right", "sure", "ok", "okay", "yup", "affirmative", "that's right", "that is right"]
        no_words = ["no", "nope", "wrong", "incorrect", "not right", "nah", "negative"]
        
        if any(word in text for word in yes_words):
            return True
        if any(word in text for word in no_words):
            return False
        return None
    
    def _spell_out(self, text: str) -> str:
        """Spell out text letter by letter for clarity"""
        # For names, just say it clearly
        return text
    
    def _spell_out_email(self, email: str) -> str:
        """Format email for clear reading"""
        # Replace @ and . with words
        return email.replace("@", " at ").replace(".", " dot ")
    
    def _parse_spelled_email(self, email_input: str) -> str:
        """
        Parse spelled-out email like 'J-O-H-N at G-M-A-I-L dot C-O-M' to 'john@gmail.com'
        Also handles: 'john at gmail dot com', 'j o h n at gmail dot com'
        """
        text = email_input.lower().strip()
        
        # Replace spoken words with symbols
        text = re.sub(r'\s+at\s+', '@', text)
        text = re.sub(r'\s+dot\s+', '.', text)
        
        # Remove spaces, dashes, and other separators from spelled letters
        # But preserve @ and .
        parts = text.split('@')
        if len(parts) == 2:
            # Clean user part (before @) - remove separators between letters
            user_part = re.sub(r'[\s\-,\.]+', '', parts[0])
            # Clean domain part (after @) - only remove separators between letters, keep dots
            domain_part = parts[1]
            # Handle spelled domains like "g-m-a-i-l dot c-o-m"
            domain_parts = domain_part.split('.')
            cleaned_domain = '.'.join(re.sub(r'[\s\-,]+', '', p) for p in domain_parts)
            text = f"{user_part}@{cleaned_domain}"
        else:
            # No @ found, try to clean the whole thing
            text = re.sub(r'[\s\-,]+', '', text)
        
        logger.info(f"🔤 Parsed spelled email: '{email_input}' -> '{text}'")
        return text
    
    def _save_appointment(self) -> None:
        """Save appointment to JSON file"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.DATA_FILE), exist_ok=True)
            
            # Load existing appointments
            appointments = {"appointments": []}
            if os.path.exists(self.DATA_FILE):
                with open(self.DATA_FILE, 'r') as f:
                    appointments = json.load(f)
            
            # Add new appointment
            appointments["appointments"].append(self.data.to_dict())
            
            # Save back
            with open(self.DATA_FILE, 'w') as f:
                json.dump(appointments, f, indent=2)
            
            logger.info(f"📝 Appointment saved: {self.data.id}")
            
        except Exception as e:
            logger.error(f"Failed to save appointment: {e}")

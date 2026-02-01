"""
History Management for Conversation Context

Manages conversation history for context-aware RAG responses.
Stores user/assistant turns and provides formatted context windows.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """
    Represents a single conversation turn.

    Attributes:
        role: 'user' or 'assistant'
        text: The spoken/written text
        timestamp: Unix timestamp when the turn occurred
        metadata: Additional context (language, intent, etc.)
    """
    role: str  # 'user' or 'assistant'
    text: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class HistoryManager:
    """
    Manages conversation history for a single session.

    Provides efficient storage and retrieval of conversation context
    for use in RAG prompts and response generation.
    """

    def __init__(self, max_turns: int = 10):
        """
        Initialize history manager.

        Args:
            max_turns: Maximum number of turns to keep in memory
        """
        self.max_turns = max_turns
        self.turns: deque = deque(maxlen=max_turns)
        self.session_start_time = time.time()
        self.form_data = {}  # Store appointment data across turns

    def add_user_turn(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add a user turn to the conversation history.

        Args:
            text: User's spoken/written text
            metadata: Additional context (language, detected intent, etc.)
        """
        if not text.strip():
            return

        turn = Turn(
            role="user",
            text=text.strip(),
            metadata=metadata or {}
        )

        self.turns.append(turn)
        logger.debug(f"Added user turn: '{text[:50]}...' ({len(self.turns)} turns total)")

    def add_agent_turn(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add an agent turn to the conversation history.

        Args:
            text: Agent's response text
            metadata: Additional context (sources, confidence, etc.)
        """
        if not text.strip():
            return

        turn = Turn(
            role="assistant",
            text=text.strip(),
            metadata=metadata or {}
        )

        self.turns.append(turn)
        logger.debug(f"Added agent turn: '{text[:50]}...' ({len(self.turns)} turns total)")

    def should_escalate(self) -> bool:
        """
        Determine if the conversation should be escalated to a human.
        This is a placeholder implementation that can be expanded.
        """
        # Simple heuristic: check if user asked for a human in recent turns
        # or if there have been multiple error responses

        # Check last 3 user turns
        recent_turns = list(self.turns)[-6:]
        user_turns = [t for t in recent_turns if t.role == "user"]

        escalation_keywords = ["human", "agent", "person", "operator", "support", "help"]

        for turn in user_turns:
            text = turn.text.lower()
            if any(keyword in text for keyword in escalation_keywords):
                # Simple check: "talk to human", "speak with agent", etc.
                if "talk" in text or "speak" in text or "transfer" in text or "connect" in text:
                    return True

        return False

        """
        Add an agent turn to the conversation history.

        Args:
            text: Agent's response text
            metadata: Additional context (sources, confidence, etc.)
        """
        if not text.strip():
            return

        turn = Turn(
            role="assistant",
            text=text.strip(),
            metadata=metadata or {}
        )

        self.turns.append(turn)
        logger.debug(f"Added agent turn: '{text[:50]}...' ({len(self.turns)} turns total)")

    def get_context_window(self, max_turns: Optional[int] = None) -> str:
        """
        Get formatted conversation history for LLM context.

        Args:
            max_turns: Maximum turns to include (None = all available)

        Returns:
            Formatted conversation history as string
        """
        if not self.turns:
            return ""

        # Get the most recent turns
        turns_to_include = list(self.turns)
        if max_turns and len(turns_to_include) > max_turns:
            turns_to_include = turns_to_include[-max_turns:]

        # Format as conversation
        context_lines = []
        for turn in turns_to_include:
            role_label = "User" if turn.role == "user" else "Assistant"
            context_lines.append(f"{role_label}: {turn.text}")

        return "\n".join(context_lines)

    def get_recent_turns(self, count: int = 5) -> List[Turn]:
        """
        Get the most recent N turns.

        Args:
            count: Number of recent turns to return

        Returns:
            List of recent Turn objects
        """
        return list(self.turns)[-count:] if self.turns else []

    def get_turns_by_role(self, role: str) -> List[Turn]:
        """
        Get all turns by a specific role.

        Args:
            role: 'user' or 'assistant'

        Returns:
            List of Turn objects for that role
        """
        return [turn for turn in self.turns if turn.role == role]

    def get_last_user_turn(self) -> Optional[Turn]:
        """Get the most recent user turn."""
        user_turns = self.get_turns_by_role("user")
        return user_turns[-1] if user_turns else None

    def get_last_agent_turn(self) -> Optional[Turn]:
        """Get the most recent agent turn."""
        agent_turns = self.get_turns_by_role("assistant")
        return agent_turns[-1] if agent_turns else None

    def clear(self) -> None:
        """Clear all conversation history."""
        self.turns.clear()
        logger.info("Conversation history cleared")

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the conversation history."""
        user_turns = len(self.get_turns_by_role("user"))
        agent_turns = len(self.get_turns_by_role("assistant"))

        return {
            "total_turns": len(self.turns),
            "user_turns": user_turns,
            "agent_turns": agent_turns,
            "max_turns": self.max_turns,
            "session_duration_seconds": time.time() - self.session_start_time,
            "oldest_turn_timestamp": self.turns[0].timestamp if self.turns else None,
            "newest_turn_timestamp": self.turns[-1].timestamp if self.turns else None
        }

    def to_dict(self) -> List[Dict[str, Any]]:
        """
        Serialize history to list of dictionaries.

        Useful for persistence or debugging.
        """
        return [
            {
                "role": turn.role,
                "text": turn.text,
                "timestamp": turn.timestamp,
                "metadata": turn.metadata
            }
            for turn in self.turns
        ]

    @classmethod
    def from_dict(cls, data: List[Dict[str, Any]], max_turns: int = 10) -> 'HistoryManager':
        """
        Create HistoryManager from serialized data.

        Args:
            data: List of turn dictionaries
            max_turns: Maximum turns to store

        Returns:
            HistoryManager instance
        """
        manager = cls(max_turns=max_turns)
        for turn_data in data:
            turn = Turn(
                role=turn_data["role"],
                text=turn_data["text"],
                timestamp=turn_data.get("timestamp", time.time()),
                metadata=turn_data.get("metadata", {})
            )
            manager.turns.append(turn)

        return manager

    def __len__(self) -> int:
        """Return number of turns in history."""
        return len(self.turns)

    def __str__(self) -> str:
        """String representation for debugging."""
        return f"HistoryManager(turns={len(self.turns)}, max={self.max_turns})"

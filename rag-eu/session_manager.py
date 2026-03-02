"""
TARA v4 Session Manager — Persistent Session State via Redis

Provides TaraSession and all supporting dataclasses for the Conscious Visual
Co-Pilot architecture.  Sessions are serialised to Redis with a 30-minute
sliding TTL so they survive individual turn boundaries while auto-expiring
after inactivity.

Usage:
    from session_manager import SessionManager, TaraSession
    sm = SessionManager(redis_client)
    session = await sm.get_or_create(session_id, client_id)
    ...
    await sm.save(session)
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

SESSION_TTL = 1800  # 30 minutes sliding window
SESSION_PREFIX = "tara:session:"


# ── Sub-Goal & Goal Plan ─────────────────────────────────────────────────────

@dataclass
class SubGoal:
    description: str        # "Enter 'Berlin' in the location search field"
    type: str               # "navigate" | "input" | "filter" | "verify" | "select"
    status: str = "pending" # "pending" | "active" | "done" | "skipped" | "blocked"
    success_signal: str = ""  # What DOM change indicates success
    attempts: int = 0
    requires_reasoning: bool = False  # v5.1: True for dropdowns, dynamic forms, obstacle steps


@dataclass
class GoalPlan:
    subgoals: List[SubGoal] = field(default_factory=list)
    original_utterance: str = ""


# ── Page State ────────────────────────────────────────────────────────────────

@dataclass
class PageState:
    page_type: str = "unknown"   # "landing"|"search"|"results"|"detail"|"form"|"modal"|"error"|"loading"|"auth"
    has_modal: bool = False
    has_search: bool = False
    primary_nav_ids: List[str] = field(default_factory=list)
    form_fields: List[str] = field(default_factory=list)
    scroll_position: str = "top"    # "top"|"middle"|"bottom"
    content_density: str = "normal" # "sparse"|"normal"|"dense"


# ── Internal Monologue ────────────────────────────────────────────────────────

@dataclass
class MonologueEntry:
    step: int
    thought: str            # "Clicked 'Search' but modal appeared. Need to dismiss."
    confidence: str = "medium"  # "high"|"medium"|"low"
    page_type: str = ""
    timestamp: float = 0.0


# ── Action Ledger ─────────────────────────────────────────────────────────────

@dataclass
class ActionRecord:
    step: int
    action_type: str             # "click"|"type_text"|"scroll"|etc.
    target_id: str = ""
    target_text: str = ""        # Human-readable label
    expected_outcome: str = ""   # From the sub-goal's success_signal
    actual_outcome: str = ""     # "page_changed"|"modal_appeared"|"nothing"|"url_changed"|"error"
    dom_changed: bool = False
    url_changed: bool = False
    new_elements_count: int = 0
    duration_ms: int = 0


# ── Reflexion Memory (v5 — Self-Correction) ──────────────────────────────────

@dataclass
class ReflexionEntry:
    step: int
    failed_action: str
    what_happened: str
    self_critique: str
    alternative_strategy: str


@dataclass
class HistoryManager:
    interaction_count: int = 0
    # Add other fields if discovered later, for now this satisfies the crash.



class ReflexionMemory:
    """Verbal self-correction memory. Carried across steps within a mission."""

    def __init__(self, max_entries: int = 3):
        self.entries: List[ReflexionEntry] = []
        self.max_entries = max_entries

    def add_failure(self, step: int, action: str, outcome: str):
        """Generate self-critique using simple template (no LLM needed)."""
        critique, alternative = self._generate_critique(action, outcome)
        entry = ReflexionEntry(
            step=step,
            failed_action=action,
            what_happened=outcome,
            self_critique=critique,
            alternative_strategy=alternative,
        )
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            self.entries.pop(0)

    def _generate_critique(self, action: str, outcome: str):
        """Template-based self-critique (zero LLM cost)."""
        outcome_lower = outcome.lower()
        if "same page" in outcome_lower or "url unchanged" in outcome_lower or "no effect" in outcome_lower:
            return (
                f"Action '{action}' did not navigate — the URL stayed the same.",
                "Try a different link, or use the sidebar navigation instead of content buttons.",
            )
        elif "element not found" in outcome_lower or "not found" in outcome_lower:
            return (
                f"Element from '{action}' doesn't exist on this page.",
                "The page may have changed. Look for alternative elements with similar text.",
            )
        elif "already clicked" in outcome_lower or "already tried" in outcome_lower:
            return (
                f"'{action}' was already tried and didn't work.",
                "Try a completely different approach: search bar, different nav path, or direct URL.",
            )
        elif "stagnation" in outcome_lower or "unchanged" in outcome_lower:
            return (
                f"Action '{action}' caused no DOM change — page is stagnant.",
                "The element may be decorative. Try an interactive element from a different section.",
            )
        else:
            return (
                f"Action '{action}' failed: {outcome}",
                "Try an alternative approach.",
            )

    def format_for_prompt(self) -> str:
        if not self.entries:
            return "(No prior failures — this is a fresh attempt)"
        lines = []
        for e in self.entries:
            lines.append(f"  Step {e.step}: Tried '{e.failed_action}' → FAILED ({e.what_happened})")
            lines.append(f"    Self-correction: {e.self_critique}")
            lines.append(f"    Better strategy: {e.alternative_strategy}")
        return "\n".join(lines)

    def to_dict(self) -> list:
        return [
            {
                "step": e.step,
                "failed_action": e.failed_action,
                "what_happened": e.what_happened,
                "self_critique": e.self_critique,
                "alternative_strategy": e.alternative_strategy,
            }
            for e in self.entries
        ]

    @classmethod
    def from_list(cls, data: list, max_entries: int = 3) -> "ReflexionMemory":
        mem = cls(max_entries=max_entries)
        for d in (data or []):
            mem.entries.append(ReflexionEntry(**d))
        return mem


# ── Explorer Mode: Site Skeleton ──────────────────────────────────────────────

@dataclass
class SiteNode:
    url: str
    page_type: str = ""
    key_elements: List[str] = field(default_factory=list)
    discovered_from: str = ""
    timestamp: float = 0.0


@dataclass
class SiteSkeleton:
    domain: str = ""
    nodes: Dict[str, SiteNode] = field(default_factory=dict)
    primary_nav: List[Dict[str, Any]] = field(default_factory=list)
    search_elements: List[Dict[str, Any]] = field(default_factory=list)
    main_headings: List[Dict[str, Any]] = field(default_factory=list)
    page_count: int = 0
    discovered_urls: List[str] = field(default_factory=list)


# ── Main Session Object ──────────────────────────────────────────────────────

@dataclass
class TaraSession:
    # Identity
    session_id: str = ""
    client_id: str = ""
    created_at: float = 0.0

    # Mission State
    goal_raw: str = ""
    goal_plan: GoalPlan = field(default_factory=GoalPlan)
    current_subgoal_index: int = 0
    mission_status: str = "idle"  # "idle"|"active"|"completed"|"stuck"|"clarifying"

    # Page Awareness
    page_state: PageState = field(default_factory=PageState)
    site_skeleton: Optional[SiteSkeleton] = None
    domain: str = ""
    hivemind_mode: str = "explorer"  # "mapped"|"explorer"
    map_hints: str = ""

    # Reasoning Memory
    internal_monologue: List[MonologueEntry] = field(default_factory=list)
    action_ledger: List[ActionRecord] = field(default_factory=list)
    reflexion_memory: Optional[ReflexionMemory] = field(default_factory=ReflexionMemory)

    # Stagnation Detection
    dom_hash: int = 0
    stagnation_count: int = 0
    consecutive_failures: int = 0

    # v5: Previous-step state for deterministic validation
    last_active_states: Optional[Dict[str, Any]] = None
    last_has_modal: bool = False
    last_element_ids: Optional[List[str]] = None  # Element IDs from previous DOM snapshot

    # Turn Metadata
    step_number: int = 0
    last_url: str = ""
    turn_timestamps: List[float] = field(default_factory=list)

    # Explorer config (set on first contact for explorer mode)
    explorer_config: Dict[str, Any] = field(default_factory=dict)

    # Speech tracking
    silent_action_count: int = 0  # Consecutive actions without speech
    interaction_mode: str = "interactive" # "interactive" | "turbo"
    
    # Legacy Support (fix for ws_handler.py crash)
    history_manager: HistoryManager = field(default_factory=HistoryManager)

    def current_subgoal(self) -> Optional[SubGoal]:
        """Return the active sub-goal, or None if no plan or index out of range."""
        if (self.goal_plan and self.goal_plan.subgoals
                and 0 <= self.current_subgoal_index < len(self.goal_plan.subgoals)):
            return self.goal_plan.subgoals[self.current_subgoal_index]
        return None


# ── Serialisation Helpers ─────────────────────────────────────────────────────

def _serialise_session(session: TaraSession) -> str:
    """Convert TaraSession to JSON string for Redis storage."""
    data = asdict(session)

    # SiteNode values inside nodes dict need special handling
    if data.get("site_skeleton") and data["site_skeleton"].get("nodes"):
        # Already dicts from asdict
        pass

    # ReflexionMemory needs custom serialization
    if session.reflexion_memory:
        data["reflexion_memory"] = session.reflexion_memory.to_dict()
    else:
        data["reflexion_memory"] = []

    return json.dumps(data, default=str)


def _deserialise_session(raw: str) -> TaraSession:
    """Reconstruct TaraSession from JSON string."""
    data = json.loads(raw)

    # Reconstruct nested dataclasses
    subgoals = [SubGoal(**sg) for sg in data.get("goal_plan", {}).get("subgoals", [])]
    goal_plan = GoalPlan(
        subgoals=subgoals,
        original_utterance=data.get("goal_plan", {}).get("original_utterance", ""),
    )

    page_state = PageState(**data.get("page_state", {}))

    monologue = [MonologueEntry(**m) for m in data.get("internal_monologue", [])]
    ledger = [ActionRecord(**a) for a in data.get("action_ledger", [])]
    reflexion = ReflexionMemory.from_list(data.get("reflexion_memory", []))

    skeleton = None
    skel_data = data.get("site_skeleton")
    if skel_data:
        nodes = {}
        for url, node_data in skel_data.get("nodes", {}).items():
            nodes[url] = SiteNode(**node_data)
        skeleton = SiteSkeleton(
            domain=skel_data.get("domain", ""),
            nodes=nodes,
            primary_nav=skel_data.get("primary_nav", []),
            search_elements=skel_data.get("search_elements", []),
            main_headings=skel_data.get("main_headings", []),
            page_count=skel_data.get("page_count", 0),
            discovered_urls=skel_data.get("discovered_urls", []),
        )

    hm_data = data.get("history_manager", {})
    history_manager = HistoryManager(**hm_data) if hm_data else HistoryManager()

    return TaraSession(
        session_id=data.get("session_id", ""),
        client_id=data.get("client_id", ""),
        created_at=data.get("created_at", 0.0),
        goal_raw=data.get("goal_raw", ""),
        goal_plan=goal_plan,
        current_subgoal_index=data.get("current_subgoal_index", 0),
        mission_status=data.get("mission_status", "idle"),
        page_state=page_state,
        site_skeleton=skeleton,
        domain=data.get("domain", ""),
        hivemind_mode=data.get("hivemind_mode", "explorer"),
        map_hints=data.get("map_hints", ""),
        internal_monologue=monologue,
        action_ledger=ledger,
        reflexion_memory=reflexion,
        dom_hash=data.get("dom_hash", 0),
        stagnation_count=data.get("stagnation_count", 0),
        consecutive_failures=data.get("consecutive_failures", 0),
        last_active_states=data.get("last_active_states"),
        last_has_modal=data.get("last_has_modal", False),
        last_element_ids=data.get("last_element_ids"),
        step_number=data.get("step_number", 0),
        last_url=data.get("last_url", ""),
        turn_timestamps=data.get("turn_timestamps", []),
        explorer_config=data.get("explorer_config", {}),
        silent_action_count=data.get("silent_action_count", 0),
        interaction_mode=data.get("interaction_mode", "interactive"),
        history_manager=history_manager,
    )


# ── Session Manager ───────────────────────────────────────────────────────────

class SessionManager:
    """Redis-backed session persistence for TaraSession objects."""

    def __init__(self, redis_client):
        self.redis = redis_client

    def _key(self, session_id: str) -> str:
        return f"{SESSION_PREFIX}{session_id}"

    async def get(self, session_id: str) -> Optional[TaraSession]:
        """Load session from Redis. Returns None if not found."""
        if not self.redis:
            return None
        try:
            raw = await self.redis.get(self._key(session_id))
            if raw is None:
                return None
            session = _deserialise_session(raw)
            # Refresh TTL on read (sliding window)
            await self.redis.expire(self._key(session_id), SESSION_TTL)
            logger.debug(f"Session loaded: {session_id} (step {session.step_number})")
            return session
        except Exception as e:
            logger.warning(f"Failed to load session {session_id}: {e}")
            return None

    async def save(self, session: TaraSession) -> bool:
        """Persist session to Redis with sliding TTL."""
        if not self.redis:
            return False
        try:
            raw = _serialise_session(session)
            await self.redis.set(
                self._key(session.session_id),
                raw,
                ex=SESSION_TTL,
            )
            logger.debug(f"Session saved: {session.session_id} (step {session.step_number})")
            return True
        except Exception as e:
            logger.warning(f"Failed to save session {session.session_id}: {e}")
            return False

    async def get_or_create(self, session_id: str, client_id: str = "") -> TaraSession:
        """Load existing session or create a fresh one."""
        session = await self.get(session_id)
        if session:
            return session
        session = TaraSession(
            session_id=session_id,
            client_id=client_id,
            created_at=time.time(),
        )
        await self.save(session)
        logger.info(f"New session created: {session_id}")
        return session

    async def delete(self, session_id: str) -> bool:
        """Remove session from Redis."""
        if not self.redis:
            return False
        try:
            await self.redis.delete(self._key(session_id))
            return True
        except Exception as e:
            logger.warning(f"Failed to delete session {session_id}: {e}")
            return False

    async def cleanup_all(self) -> bool:
        """Wipe all Visual Copilot sessions (FLUSH equivalent for this namespace)."""
        if not self.redis:
            return False
        try:
            pattern = f"{SESSION_PREFIX}*"
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
            logger.info(f"🧹 Redis Cleanup: Deleted {len(keys)} session keys.")
            return True
        except Exception as e:
            logger.error(f"Failed to cleanup Redis sessions: {e}")
            return False

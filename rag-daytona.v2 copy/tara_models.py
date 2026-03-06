"""
tara_models.py

PURPOSE: Centralized data models and schemas for the TARA Visual Agent system.
         Provides type-safe data structures for intent parsing, DOM mirroring,
         mission tracking, and decision-making across all modules.

DEPENDENCIES:
    - dataclasses: For structured data containers
    - typing: For type hints (List, Dict, Optional, etc.)
    - enum: For ActionIntent and ConstraintStatus enums
    - time: For timestamp generation

USED BY:
    - mind_reader.py: TacticalSchema for output
    - live_graph.py: GraphNode, DomDelta for DOM storage
    - hive_interface.py: StrategyHint, VisualHint, HiveResponse
    - semantic_detective.py: ScoredCandidate, DetectiveReport
    - mission_brain.py: MissionState, Constraint for state tracking

MIGRATION STATUS: [NEW] - Core foundation for Ultimate TARA architecture

Example:
    from tara_models import TacticalSchema, ActionIntent, GraphNode
    
    schema = TacticalSchema(
        action=ActionIntent.PURCHASE,
        target_entity="T-shirt",
        domain="zalando.com",
        constraints={"color": "white", "size": None}
    )
    print(schema.missing_constraints())  # ['size']
    print(schema.to_query_string())  # "action:purchase domain:zalando.com color:white"
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Literal, Any
from enum import Enum
import time
import json


# ═══════════════════════════════════════════════════════════════
# INPUT SCHEMA (From Mind Reader)
# ═══════════════════════════════════════════════════════════════

class ActionIntent(Enum):
    """
    Enum representing the type of user intent.
    Used by mind_reader.py to categorize user requests.
    """
    NAVIGATION = "navigation"       # Go somewhere (e.g., "go to settings")
    EXTRACTION = "extraction"       # Read/find data (e.g., "show my usage")
    INTERACTION = "interaction"     # Fill form, click button (e.g., "click submit")
    PURCHASE = "purchase"           # E-commerce flow (e.g., "buy a shirt")
    SEARCH = "search"               # Search for items (e.g., "find white shirts")


@dataclass(frozen=True)
class TacticalSchema:
    """
    Structured representation of user intent.
    Output of mind_reader.py, consumed by hive_interface.py and mission_brain.py.
    
    Attributes:
        action: Type of task to perform
        target_entity: What the user is asking about
        domain: Website domain (extracted from URL)
        constraints: Filters or requirements (color, size, etc.)
        raw_utterance: Original user input for reference
        timestamp: When the request was made
    
    Example:
        schema = TacticalSchema(
            action=ActionIntent.PURCHASE,
            target_entity="shirt",
            domain="zalando.com",
            constraints={"size": "medium", "color": None}
        )
        print(schema.missing_constraints())  # ['color']
    """
    action: ActionIntent
    target_entity: str
    domain: str
    constraints: Dict[str, Optional[str]]
    raw_utterance: str
    timestamp: float = field(default_factory=lambda: time.time())

    def missing_constraints(self) -> List[str]:
        """
        Returns list of constraint keys that are None (not provided by user).
        Used by mission_brain.py to check if action is blocked.
        
        Returns:
            List of constraint names that are missing values
            
        Example:
            schema = TacticalSchema(
                action=ActionIntent.PURCHASE,
                target_entity="shirt",
                domain="zalando.com",
                constraints={"color": "white", "size": None, "quantity": None}
            )
            schema.missing_constraints()  # ['size', 'quantity']
        """
        return [k for k, v in self.constraints.items() if v is None]

    def has_all_constraints(self) -> bool:
        """
        Check if all constraints have values (none are None).
        
        Returns:
            True if all constraints are filled, False otherwise
        """
        return len(self.missing_constraints()) == 0

    def to_query_string(self) -> str:
        """
        Converts schema to Qdrant query string format.
        Used by hive_interface.py for vector database queries.
        
        Returns:
            Query string in format: "action:purchase domain:zalando.com color:white"
            
        Example:
            schema = TacticalSchema(
                action=ActionIntent.PURCHASE,
                target_entity="shirt",
                domain="zalando.com",
                constraints={"color": "white"}
            )
            schema.to_query_string()  # "action:purchase domain:zalando.com color:white"
        """
        parts = [f"action:{self.action.value}", f"domain:{self.domain}"]
        for k, v in self.constraints.items():
            if v:  # Only include filled constraints
                parts.append(f"{k}:{v}")
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize to dictionary for JSON/Redis storage.
        
        Returns:
            Dictionary representation of the schema
        """
        return {
            "action": self.action.value,
            "target_entity": self.target_entity,
            "domain": self.domain,
            "constraints": self.constraints,
            "raw_utterance": self.raw_utterance,
            "timestamp": self.timestamp
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TacticalSchema":
        """
        Deserialize from dictionary.
        
        Args:
            data: Dictionary with schema fields
            
        Returns:
            TacticalSchema instance
        """
        return cls(
            action=ActionIntent(data["action"]),
            target_entity=data["target_entity"],
            domain=data["domain"],
            constraints=data.get("constraints", {}),
            raw_utterance=data["raw_utterance"],
            timestamp=data.get("timestamp", time.time())
        )


# ═══════════════════════════════════════════════════════════════
# HIVE MEMORY OUTPUTS
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class StrategyHint:
    """
    High-level navigation sequence from Hive.
    Used by mission_brain.py for planning sub-goals.
    
    Attributes:
        sequence: Ordered list of steps (e.g., ["Search", "Select Item", "Add to Cart"])
        constraints_order: Order in which constraints should be filled
        blocking_rules: Map of actions to their required constraints
        confidence: Score from Qdrant (0.0-1.0)
        source_url: Example URL this strategy came from
    
    Example:
        strategy = StrategyHint(
            sequence=["Search", "Select Size", "Add to Bag"],
            constraints_order=["color", "size"],
            blocking_rules={"Add to Bag": ["size", "color"]},
            confidence=0.85,
            source_url="https://zalando.com/example"
        )
    """
    sequence: List[str]
    constraints_order: List[str]
    blocking_rules: Dict[str, List[str]]
    confidence: float
    source_url: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for JSON storage."""
        return {
            "sequence": self.sequence,
            "constraints_order": self.constraints_order,
            "blocking_rules": self.blocking_rules,
            "confidence": self.confidence,
            "source_url": self.source_url
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategyHint":
        """Deserialize from dictionary."""
        return cls(
            sequence=data.get("sequence", []),
            constraints_order=data.get("constraints_order", []),
            blocking_rules=data.get("blocking_rules", {}),
            confidence=data.get("confidence", 0.0),
            source_url=data.get("source_url", "")
        )


@dataclass(frozen=True)
class VisualHint:
    """
    Low-level element identifiers from Hive.
    Used by semantic_detective.py for element scoring.
    
    Attributes:
        selector: CSS selector or ID pattern
        element_type: Type of element (dropdown, button, input)
        text_pattern: Expected text pattern (regex-like)
        zone: Page zone (product_card, nav, sidebar)
        confidence: Score from Qdrant (0.0-1.0)
    
    Example:
        hint = VisualHint(
            selector="#size-picker",
            element_type="dropdown",
            text_pattern="Choose size",
            zone="product_card",
            confidence=0.92
        )
    """
    selector: str
    element_type: str
    text_pattern: Optional[str]
    zone: str
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for JSON storage."""
        return {
            "selector": self.selector,
            "element_type": self.element_type,
            "text_pattern": self.text_pattern,
            "zone": self.zone,
            "confidence": self.confidence
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisualHint":
        """Deserialize from dictionary."""
        return cls(
            selector=data.get("selector", ""),
            element_type=data.get("element_type", ""),
            text_pattern=data.get("text_pattern"),
            zone=data.get("zone", "main"),
            confidence=data.get("confidence", 0.0)
        )


@dataclass
class HiveResponse:
    """
    Combined output from hive_interface.py.
    Contains both strategy (for Planner) and visual hints (for Detective).
    
    Attributes:
        strategy: High-level navigation plan (or None if not found)
        visual_hints: List of element selectors
        cached: Whether response was from cache
        query_time_ms: Time taken to query Qdrant
    
    Example:
        response = HiveResponse(
            strategy=strategy_hint,
            visual_hints=[visual_hint1, visual_hint2],
            cached=False,
            query_time_ms=45
        )
    """
    strategy: Optional[StrategyHint]
    visual_hints: List[VisualHint]
    cached: bool
    query_time_ms: int
    cross_domain_target: Optional[str] = None  # Bridge domain for cross-domain navigation

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for JSON storage."""
        result = {
            "strategy": self.strategy.to_dict() if self.strategy else None,
            "visual_hints": [h.to_dict() for h in self.visual_hints],
            "cached": self.cached,
            "query_time_ms": self.query_time_ms
        }
        if self.cross_domain_target:
            result["cross_domain_target"] = self.cross_domain_target
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HiveResponse":
        """Deserialize from dictionary."""
        strategy = None
        if data.get("strategy"):
            strategy = StrategyHint.from_dict(data["strategy"])
        
        visual_hints = [
            VisualHint.from_dict(h) for h in data.get("visual_hints", [])
        ]
        
        return cls(
            strategy=strategy,
            visual_hints=visual_hints,
            cached=data.get("cached", False),
            query_time_ms=data.get("query_time_ms", 0),
            cross_domain_target=data.get("cross_domain_target")
        )


# ═══════════════════════════════════════════════════════════════
# LIVE GRAPH NODE (DOM Mirror)
# ═══════════════════════════════════════════════════════════════

@dataclass
class GraphNode:
    """
    Represents a single interactive element in the Live Graph.
    Stored in Redis as JSON by live_graph.py.
    
    Attributes:
        id: Stable hash identifier (e.g., "tara-abc123")
        tag: HTML tag name (button, a, input)
        text: Visible text content
        role: ARIA role for accessibility
        zone: Page zone (nav, main, modal, sidebar)
        interactive: Whether element is interactive
        visible: Whether element is currently visible
        rect: Bounding box {x, y, w, h}
        parent_id: Parent node ID (for hierarchy)
        depth: DOM depth level
        state: Element state (active, disabled, focused, "")
        aria_selected: ARIA selected state
        aria_expanded: ARIA expanded state
        timestamp: When node was added to graph
    
    Example:
        node = GraphNode(
            id="tara-abc123",
            tag="button",
            text="Add to Cart",
            role="button",
            zone="product_card",
            interactive=True,
            visible=True,
            rect={"x": 100, "y": 200, "w": 120, "h": 40},
            parent_id="tara-xyz789",
            depth=3,
            state="",
            aria_selected=None,
            aria_expanded=None,
            timestamp=time.time()
        )
    """
    id: str
    tag: str
    text: str
    role: str
    zone: str
    interactive: bool
    visible: bool
    rect: Dict[str, float]
    parent_id: Optional[str]
    depth: int
    state: str
    aria_selected: Optional[str]
    aria_expanded: Optional[str]
    timestamp: float

    def to_redis_dict(self) -> Dict[str, Any]:
        """
        Serializes to Redis-compatible dictionary.
        
        Returns:
            Dictionary suitable for Redis JSON storage
        """
        return {
            "id": self.id,
            "tag": self.tag,
            "text": self.text,
            "role": self.role,
            "zone": self.zone,
            "interactive": self.interactive,
            "visible": self.visible,
            "rect": self.rect,
            "parent_id": self.parent_id or "",
            "depth": self.depth,
            "state": self.state,
            "aria_selected": self.aria_selected,
            "aria_expanded": self.aria_expanded,
            "timestamp": self.timestamp
        }

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_redis_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GraphNode":
        """
        Deserialize from dictionary.
        
        Args:
            data: Dictionary with node fields
            
        Returns:
            GraphNode instance
        """
        return cls(
            id=data.get("id", ""),
            tag=data.get("tag") or data.get("type", ""),

            text=data.get("text", ""),
            role=data.get("role", ""),
            zone=data.get("zone", "main"),
            interactive=data.get("interactive", False),
            visible=data.get("visible", True),
            rect=data.get("rect", {}),
            parent_id=data.get("parent_id") or None,
            depth=data.get("depth", 0),
            state=data.get("state", ""),
            aria_selected=data.get("aria_selected"),
            aria_expanded=data.get("aria_expanded"),
            timestamp=data.get("timestamp", time.time())
        )

    @classmethod
    def from_json(cls, json_str: str) -> "GraphNode":
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))


# ═══════════════════════════════════════════════════════════════
# MISSION STATE
# ═══════════════════════════════════════════════════════════════

class ConstraintStatus(Enum):
    """Status of a mission constraint."""
    MISSING = "MISSING"       # Not yet provided by user
    FILLED = "FILLED"         # Value provided
    INVALID = "INVALID"       # Value provided but invalid


@dataclass
class Constraint:
    """
    Tracks the status of a single constraint.
    Used by mission_brain.py for constraint auditing.
    
    Attributes:
        name: Constraint name (e.g., "size", "color")
        value: Current value (None if missing)
        status: Current status (MISSING/FILLED/INVALID)
    
    Example:
        constraint = Constraint(
            name="size",
            value="medium",
            status=ConstraintStatus.FILLED
        )
    """
    name: str
    value: Optional[str]
    status: ConstraintStatus

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "name": self.name,
            "value": self.value,
            "status": self.status.value
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Constraint":
        """Deserialize from dictionary."""
        return cls(
            name=data.get("name", ""),
            value=data.get("value"),
            status=ConstraintStatus(data.get("status", "MISSING"))
        )


@dataclass
class MissionState:
    """
    Persistent mission tracking in Redis.
    Managed by mission_brain.py.
    
    Attributes:
        mission_id: Unique mission identifier
        session_id: Parent session ID
        schema: TacticalSchema from mind_reader
    
        status: Current mission status
        current_subgoal_index: Index of active sub-goal
        subgoals: List of sub-goal descriptions
    
        constraints: Map of constraint name to Constraint object
        visited_urls: URLs visited during mission
        action_history: Compact action log
        ambiguity_count: Number of ambiguous situations encountered
    
        created_at: Mission creation timestamp
        updated_at: Last update timestamp
    
    Example:
        mission = MissionState(
            mission_id="mission-123",
            session_id="session-456",
            schema=tactical_schema,
            status="in_progress",
            current_subgoal_index=0,
            subgoals=["Navigate to products", "Select white shirt", "Choose size"],
            constraints={"size": size_constraint},
            visited_urls=["https://shop.com"],
            action_history=["click_nav_products"],
            ambiguity_count=0,
            created_at=time.time(),
            updated_at=time.time()
        )
    """
    mission_id: str
    session_id: str
    schema: TacticalSchema
    status: Literal["idle", "in_progress", "blocked", "completed", "failed"]
    current_subgoal_index: int
    subgoals: List[str]
    constraints: Dict[str, Constraint]
    visited_urls: List[str]
    action_history: List[str]
    ambiguity_count: int
    created_at: float
    updated_at: float
    pending_action: Optional[Dict[str, Any]] = None
    last_url: Optional[str] = None
    last_dom_signature: Optional[str] = None
    pending_verify_attempts: int = 0
    main_goal: Optional[str] = None
    phase: str = "strategy"
    last_mile_started_at: Optional[float] = None
    last_mile_runtime: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for Redis storage."""
        return {
            "mission_id": self.mission_id,
            "session_id": self.session_id,
            "schema": self.schema.to_dict(),
            "status": self.status,
            "current_subgoal_index": self.current_subgoal_index,
            "subgoals": self.subgoals,
            "constraints": {k: v.to_dict() for k, v in self.constraints.items()},
            "visited_urls": self.visited_urls,
            "action_history": self.action_history,
            "ambiguity_count": self.ambiguity_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pending_action": self.pending_action,
            "last_url": self.last_url,
            "last_dom_signature": self.last_dom_signature,
            "pending_verify_attempts": self.pending_verify_attempts,
            "main_goal": self.main_goal,
            "phase": self.phase,
            "last_mile_started_at": self.last_mile_started_at,
            "last_mile_runtime": self.last_mile_runtime,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MissionState":
        """Deserialize from dictionary."""
        schema = TacticalSchema.from_dict(data["schema"])
        constraints = {
            k: Constraint.from_dict(v)
            for k, v in data.get("constraints", {}).items()
        }
        
        return cls(
            mission_id=data.get("mission_id", ""),
            session_id=data.get("session_id", ""),
            schema=schema,
            status=data.get("status", "idle"),
            current_subgoal_index=data.get("current_subgoal_index", 0),
            subgoals=data.get("subgoals", []),
            constraints=constraints,
            visited_urls=data.get("visited_urls", []),
            action_history=data.get("action_history", []),
            ambiguity_count=data.get("ambiguity_count", 0),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            pending_action=data.get("pending_action"),
            last_url=data.get("last_url"),
            last_dom_signature=data.get("last_dom_signature"),
            pending_verify_attempts=data.get("pending_verify_attempts", 0),
            main_goal=data.get("main_goal"),
            phase=data.get("phase", "strategy"),
            last_mile_started_at=data.get("last_mile_started_at"),
            last_mile_runtime=data.get("last_mile_runtime"),
        )

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> "MissionState":
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))


# ═══════════════════════════════════════════════════════════════
# DETECTIVE OUTPUT
# ═══════════════════════════════════════════════════════════════

@dataclass
class ScoredCandidate:
    """
    A single candidate element with hybrid score.
    Output of semantic_detective.py investigation.
    
    Attributes:
        node_id: Graph node identifier
        text: Element text content
        tag: HTML tag
        zone: Page zone
    
        semantic_score: Vector similarity score (0.0-1.0)
        hive_score: Hint match score (0.0-1.0)
        hybrid_score: Weighted combination
    
        matched_hint: VisualHint that matched (if any)
        reasons: List of scoring reasons
    
    Example:
        candidate = ScoredCandidate(
            node_id="tara-abc123",
            text="Add to Cart",
            tag="button",
            zone="product_card",
            semantic_score=0.85,
            hive_score=0.92,
            hybrid_score=0.88,
            matched_hint=visual_hint,
            reasons=["text_match", "zone_match", "hint_selector_match"]
        )
    """
    node_id: str
    text: str
    tag: str
    zone: str
    semantic_score: float
    hive_score: float
    hybrid_score: float
    matched_hint: Optional[VisualHint]
    reasons: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "node_id": self.node_id,
            "text": self.text,
            "tag": self.tag,
            "zone": self.zone,
            "semantic_score": self.semantic_score,
            "hive_score": self.hive_score,
            "hybrid_score": self.hybrid_score,
            "matched_hint": self.matched_hint.to_dict() if self.matched_hint else None,
            "reasons": self.reasons
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScoredCandidate":
        """Deserialize from dictionary."""
        matched_hint = None
        if data.get("matched_hint"):
            matched_hint = VisualHint.from_dict(data["matched_hint"])
        
        return cls(
            node_id=data.get("node_id", ""),
            text=data.get("text", ""),
            tag=data.get("tag", ""),
            zone=data.get("zone", "main"),
            semantic_score=data.get("semantic_score", 0.0),
            hive_score=data.get("hive_score", 0.0),
            hybrid_score=data.get("hybrid_score", 0.0),
            matched_hint=matched_hint,
            reasons=data.get("reasons", [])
        )


@dataclass
class DetectiveReport:
    """
    Output of semantic_detective.py investigation.
    Contains scored candidates and page analysis.
    
    Attributes:
        candidates: List of scored candidate elements
        best_match: Top candidate (highest score)
    
        is_ambiguous: True if multiple candidates within 0.1 score
        ambiguous_count: Number of similarly-scored candidates
    
        has_obstacle: True if modal/cookie banner detected
        obstacle_type: Type of obstacle (modal, cookie_banner)
        dismiss_button_id: ID of dismiss button if found
    
        page_type: Page classification (form, nav, data, search)
    
        recommended_action: Suggested action (click, type, scroll, dismiss)
        confidence: Confidence level (high, medium, low)
    
    Example:
        report = DetectiveReport(
            candidates=[candidate1, candidate2],
            best_match=candidate1,
            is_ambiguous=False,
            ambiguous_count=1,
            has_obstacle=False,
            obstacle_type=None,
            dismiss_button_id=None,
            page_type="product",
            recommended_action="click",
            confidence="high"
        )
    """
    candidates: List[ScoredCandidate]
    best_match: Optional[ScoredCandidate]
    is_ambiguous: bool
    ambiguous_count: int
    has_obstacle: bool
    obstacle_type: Optional[str]
    dismiss_button_id: Optional[str]
    page_type: str
    recommended_action: str
    confidence: Literal["high", "medium", "low"]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "best_match": self.best_match.to_dict() if self.best_match else None,
            "is_ambiguous": self.is_ambiguous,
            "ambiguous_count": self.ambiguous_count,
            "has_obstacle": self.has_obstacle,
            "obstacle_type": self.obstacle_type,
            "dismiss_button_id": self.dismiss_button_id,
            "page_type": self.page_type,
            "recommended_action": self.recommended_action,
            "confidence": self.confidence
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DetectiveReport":
        """Deserialize from dictionary."""
        candidates = [
            ScoredCandidate.from_dict(c) for c in data.get("candidates", [])
        ]
        
        best_match = None
        if data.get("best_match"):
            best_match = ScoredCandidate.from_dict(data["best_match"])
        
        return cls(
            candidates=candidates,
            best_match=best_match,
            is_ambiguous=data.get("is_ambiguous", False),
            ambiguous_count=data.get("ambiguous_count", 0),
            has_obstacle=data.get("has_obstacle", False),
            obstacle_type=data.get("obstacle_type"),
            dismiss_button_id=data.get("dismiss_button_id"),
            page_type=data.get("page_type", "unknown"),
            recommended_action=data.get("recommended_action", "click"),
            confidence=data.get("confidence", "medium")
        )


# ═══════════════════════════════════════════════════════════════
# DELTA MESSAGES (Browser → Server)
# ═══════════════════════════════════════════════════════════════

@dataclass
class DomDelta:
    """
    Incremental DOM update from tara_sensor.js.
    Represents changes to the DOM rather than full snapshot.
    
    Attributes:
        delta_type: Type of delta (add, remove, update, full_scan)
        nodes: List of graph nodes being added/updated
        removed_ids: List of node IDs being removed
        url: Current page URL
        timestamp: When delta was created
    
    Example:
        delta = DomDelta(
            delta_type="update",
            nodes=[new_node],
            removed_ids=[],
            url="https://shop.com/products",
            timestamp=time.time()
        )
    """
    delta_type: Literal["add", "remove", "update", "full_scan"]
    nodes: List[GraphNode]
    removed_ids: List[str]
    url: str
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for WebSocket transmission."""
        return {
            "delta_type": self.delta_type,
            "nodes": [n.to_redis_dict() for n in self.nodes],
            "removed_ids": self.removed_ids,
            "url": self.url,
            "timestamp": self.timestamp
        }

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DomDelta":
        """Deserialize from dictionary."""
        nodes = [GraphNode.from_dict(n) for n in data.get("nodes", [])]
        
        return cls(
            delta_type=data.get("delta_type", "update"),
            nodes=nodes,
            removed_ids=data.get("removed_ids", []),
            url=data.get("url", ""),
            timestamp=data.get("timestamp", time.time())
        )

    @classmethod
    def from_json(cls, json_str: str) -> "DomDelta":
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))


# ═══════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def serialize_for_redis(obj: Any) -> str:
    """
    Generic serialization helper for Redis storage.
    Handles dataclasses with to_dict() methods.
    
    Args:
        obj: Object to serialize (must have to_dict() or be JSON-serializable)
        
    Returns:
        JSON string for Redis storage
    """
    if hasattr(obj, 'to_dict'):
        return json.dumps(obj.to_dict())
    return json.dumps(obj, default=str)


def deserialize_from_json(json_str: str, cls: type) -> Any:
    """
    Generic deserialization helper.
    Reconstructs objects from JSON using from_dict() classmethod.
    
    Args:
        json_str: JSON string from Redis
        cls: Target class (must have from_dict() classmethod)
        
    Returns:
        Deserialized object instance
    """
    data = json.loads(json_str)
    if hasattr(cls, 'from_dict'):
        return cls.from_dict(data)
    return cls(**data)

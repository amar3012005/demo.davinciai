from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MappedTerminalContext:
    """Structured context for known-site mapped mode terminal extraction.

    When PageIndex has high confidence (>0.8) about the current node and target,
    this context is passed to last_mile to enable deterministic terminal extraction
    with pre-validated control groups and capabilities.

    CONTRACT-FIRST DESIGN:
    - Site map node defines the contract (expected_controls, control_groups)
    - Vision hints are advisory (confidence boosters, not source of truth)
    - DOM-grounded control resolution matches site map terms to live elements
    """
    mapped_mode: bool = False
    expected_node_id: str = ""
    mapped_terminal_node: str = ""
    required_controls: List[str] = field(default_factory=list)
    control_groups: Dict[str, List[str]] = field(default_factory=dict)  # {"date_filters": ["Date Picker", ...], ...}
    task_modes: List[str] = field(default_factory=list)  # ["read_extract", "filter_view"]
    completion_contract: Dict[str, List[str]] = field(default_factory=dict)  # {"read_extract": ["entity_visible", ...], ...}
    allowed_terminal_capabilities: List[str] = field(default_factory=list)

    def is_valid_terminal(self) -> bool:
        """Check if this context represents a valid terminal node for extraction."""
        return self.mapped_mode and bool(self.expected_node_id)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for passing through pipeline."""
        return {
            "mapped_mode": self.mapped_mode,
            "expected_node_id": self.expected_node_id,
            "mapped_terminal_node": self.mapped_terminal_node,
            "required_controls": self.required_controls,
            "control_groups": self.control_groups,
            "task_modes": self.task_modes,
            "completion_contract": self.completion_contract,
            "allowed_terminal_capabilities": self.allowed_terminal_capabilities,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MappedTerminalContext":
        """Deserialize from dict."""
        if not data:
            return cls()
        return cls(
            mapped_mode=bool(data.get("mapped_mode", False)),
            expected_node_id=str(data.get("expected_node_id", "")),
            mapped_terminal_node=str(data.get("mapped_terminal_node", "")),
            required_controls=list(data.get("required_controls", [])),
            control_groups=dict(data.get("control_groups", {})),
            task_modes=list(data.get("task_modes", [])),
            completion_contract=dict(data.get("completion_contract", {})),
            allowed_terminal_capabilities=list(data.get("allowed_terminal_capabilities", [])),
        )

    @classmethod
    def from_site_map(cls, node: Dict[str, Any]) -> "MappedTerminalContext":
        """Create context from a site map node.

        Args:
            node: Site map node dictionary with contract fields

        Returns:
            MappedTerminalContext with all contract fields populated
        """
        if not node:
            return cls()
        return cls(
            mapped_mode=True,
            expected_node_id=node.get("node_id", ""),
            mapped_terminal_node=node.get("title", ""),
            required_controls=list(node.get("required_controls", []) or []),
            control_groups=dict(node.get("control_groups", {}) or {}),
            task_modes=list(node.get("task_modes", []) or []),
            completion_contract=dict(node.get("completion_contract", {}) or {}),
            allowed_terminal_capabilities=list(node.get("terminal_capabilities", []) or []),
        )


@dataclass
class PlanningContext:
    app: Any
    session_id: str
    goal: str
    current_url: str
    step_number: int
    action_history: list[str] = field(default_factory=list)
    previous_goal: Optional[str] = None
    mission_id: Optional[str] = None
    trace_id: str = ""
    mapped_terminal_context: Optional[MappedTerminalContext] = None


@dataclass
class RoutingDecision:
    decision: str
    reason: str
    module: str
    confidence: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    module: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    success: bool
    response: Dict[str, Any]
    decision: Optional[RoutingDecision] = None

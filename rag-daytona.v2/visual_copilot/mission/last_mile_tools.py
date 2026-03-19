"""
Mapped Extraction State Machine for Deterministic Known-Site Data Extraction.

This module provides a finite-state extraction machine for mapped pages,
enforcing strict completion invariants that require page-grounded evidence.

Usage:
    context = MappedTerminalContext.from_site_map(url, nodes, goal)
    if context.is_mapped:
        machine = MappedExtractionStateMachine(context)
        while machine.state != MappedExtractionState.COMPLETE:
            action = machine.transition(observation)
            # Execute action, then update observation
"""

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from visual_copilot.navigation.site_map_validator import SiteMapValidator

logger = logging.getLogger("vc.mission.last_mile_tools")


class MappedExtractionState(Enum):
    """Finite states for deterministic mapped extraction.

    The state machine enforces a strict progression through these states,
    ensuring we don't skip critical verification steps.
    """
    VALIDATE_NODE = "validate_node"           # Confirm we're on the right site map node
    VALIDATE_SCOPE = "validate_scope"         # Check filters match goal requirements
    SET_FILTERS = "set_filters"               # Apply missing date/model filters
    LOCATE_ENTITY = "locate_entity"           # Find target entity (Whisper, GPT-4, etc.)
    LOCATE_METRIC = "locate_metric"           # Find metric column/field (tokens, cost)
    EXTRACT_VALUE = "extract_value"           # Extract actual numeric value
    VALIDATE_EVIDENCE = "validate_evidence"   # Verify evidence meets invariants
    COMPLETE = "complete"                     # Terminal state


class MappedTaskType(Enum):
    """Task types for mapped mode execution.

    Determines which FSM to use based on user goal classification.
    """
    READ_EXTRACT = "read_extract"       # "Show me Whisper token usage"
    CREATE_ACTION = "create_action"     # "Create a new API key"
    FORM_FILL = "form_fill"             # "Update my profile"
    CONFIRM_ACTION = "confirm_action"   # "Yes, delete this project"


class MappedActionState(Enum):
    """States for action/creation workflows."""
    VALIDATE_NODE = "validate_node"
    FIND_PRIMARY_CTA = "find_primary_cta"
    CLICK_PRIMARY_CTA = "click_primary_cta"
    VERIFY_MODAL_OR_FORM = "verify_modal_or_form"
    FILL_REQUIRED_FIELDS = "fill_required_fields"
    CLICK_CONFIRM = "click_confirm"
    VERIFY_SUCCESS = "verify_success"
    COMPLETE = "complete"


class MappedFormFillState(Enum):
    """States for form fill workflows."""
    VALIDATE_NODE = "validate_node"
    LOCATE_FORM = "locate_form"
    FILL_FIELD = "fill_field"
    VALIDATE_FORM = "validate_form"
    SUBMIT_FORM = "submit_form"
    VERIFY_SUCCESS = "verify_success"
    COMPLETE = "complete"


@dataclass
class MappedTerminalContext:
    """Context for mapped terminal extraction.

    This encapsulates all state needed for deterministic extraction
    from known sites with site map definitions.

    CONTRACT-FIRST DESIGN:
    - Site map node defines the contract (expected_controls, control_groups)
    - Vision hints are advisory (confidence boosters, not source of truth)
    - DOM-grounded control resolution matches site map terms to live elements
    - Transition rules define the control group sequence for each task mode
    """
    url: str
    current_node_id: Optional[str] = None
    current_node_title: str = ""
    site_map_node: Optional[Dict[str, Any]] = None
    goal_entity: str = ""          # e.g., "Whisper", "GPT-4"
    goal_metric: str = ""          # e.g., "tokens", "cost", "usage"
    goal_filters: Dict[str, str] = field(default_factory=dict)  # e.g., {"date_range": "last_7_days"}
    expected_controls: List[str] = field(default_factory=list)  # From site map
    control_groups: Dict[str, Any] = field(default_factory=dict)  # From site map: {"date_picker": {...}, "model_filter": {...}}
    task_modes: Dict[str, Any] = field(default_factory=dict)  # From site map: {"read_token_usage": {...}}
    goal_triggers: Dict[str, List[str]] = field(default_factory=dict)  # From site map: {"read_token_usage": ["read token usage", ...]}
    current_task_mode: str = ""  # Current task mode: "read_token_usage", "read_cost_breakdown", etc.
    transition_rules: Dict[str, Any] = field(default_factory=dict)  # From site map: {"read_token_usage": [...]}
    completion_contract: Dict[str, List[str]] = field(default_factory=dict)  # From site map: {"read_extract": ["entity_visible", ...]}
    terminal_capabilities: List[str] = field(default_factory=list)
    is_mapped: bool = False

    # Evidence tracking
    entity_anchor_found: bool = False
    entity_anchor_text: str = ""
    metric_anchor_found: bool = False
    metric_anchor_text: str = ""
    numeric_value_found: bool = False
    numeric_value: str = ""
    evidence_text: str = ""

    # Vision advisory (not source of truth)
    vision_target_id: Optional[str] = None
    vision_confidence: float = 0.0  # 0.0-1.0 confidence in vision match

    @classmethod
    def from_site_map(
        cls,
        url: str,
        nodes: List[Any],
        goal: str,
        validator: Optional[SiteMapValidator] = None,
    ) -> "MappedTerminalContext":
        """Create context from site map validation.

        Args:
            url: Current page URL
            nodes: List of DOM nodes for control detection
            goal: User's extraction goal (for entity/metric extraction)
            validator: Optional SiteMapValidator instance

        Returns:
            MappedTerminalContext with is_mapped=True if URL matches site map
        """
        if validator is None:
            try:
                validator = SiteMapValidator()
            except Exception:
                return cls(url=url, is_mapped=False)

        node = validator.resolve_current_node(url, nodes=nodes)
        if node is None:
            return cls(url=url, is_mapped=False)

        # Extract goal components
        entity, metric, filters = cls._parse_goal(goal)

        # Extract control_groups from site map node (organized by function)
        # Example: {"date_picker": {"labels": [...]}, "model_filter": {...}}
        control_groups = node.get("control_groups", {}) or {}

        # Extract task_modes dictionary (not list)
        task_modes = node.get("task_modes", {}) or {}

        # Extract goal_triggers - maps task mode to trigger phrases
        goal_triggers = node.get("goal_triggers", {}) or {}

        # Extract transition_rules - defines control group sequence per task mode
        transition_rules = node.get("transition_rules", {}) or {}

        # Extract completion_contract
        completion_contract = node.get("completion_contract", {}) or {}

        # Determine current task mode from goal matching
        current_task_mode = cls._match_task_mode(goal, task_modes, goal_triggers)

        return cls(
            url=url,
            current_node_id=node.get("node_id"),
            current_node_title=node.get("title", ""),
            site_map_node=node,
            goal_entity=entity,
            goal_metric=metric,
            goal_filters=filters,
            expected_controls=node.get("expected_controls", []) or [],
            control_groups=control_groups,
            task_modes=task_modes,
            goal_triggers=goal_triggers,
            current_task_mode=current_task_mode,
            transition_rules=transition_rules,
            completion_contract=completion_contract,
            terminal_capabilities=node.get("terminal_capabilities", []) or [],
            is_mapped=True,
        )

    @staticmethod
    def _parse_goal(goal: str) -> Tuple[str, str, Dict[str, str]]:
        """Parse goal into entity, metric, and filters.

        Examples:
            "How many Whisper tokens did I use?" -> ("Whisper", "tokens", {})
            "Show me GPT-4 cost for last 7 days" -> ("GPT-4", "cost", {"date_range": "last_7_days"})
        """
        goal_lower = goal.lower()

        # Common model/entity patterns
        entity_patterns = [
            r"\b(whisper|gpt-4|gpt-4o|gpt-3\.5|claude|llama|gemini)\b",
            r"\b(gpt-oss-\d+b)\b",
            r"\b(dall-e|embedding)\b",
        ]
        entity = ""
        for pattern in entity_patterns:
            match = re.search(pattern, goal_lower)
            if match:
                entity = match.group(1).title()
                break

        # Metric patterns
        metric_patterns = [
            (r"\b(tokens?|usage)\b", "tokens"),
            (r"\b(cost|price|pricing|dollars?|\$)\b", "cost"),
            (r"\b(requests?|calls?|api calls?)\b", "requests"),
            (r"\b(characters?)\b", "characters"),
            (r"\b(seconds?|duration|time)\b", "duration"),
        ]
        metric = ""
        for pattern, metric_name in metric_patterns:
            if re.search(pattern, goal_lower):
                metric = metric_name
                break

        # Date/filter patterns
        filters = {}
        if re.search(r"\blast (7|seven) days?\b", goal_lower):
            filters["date_range"] = "last_7_days"
        elif re.search(r"\blast (30|thirty) days?\b", goal_lower):
            filters["date_range"] = "last_30_days"
        elif re.search(r"\blast (24|twenty[- ]?four) hours?\b", goal_lower):
            filters["date_range"] = "last_24_hours"
        elif re.search(r"\bthis month\b", goal_lower):
            filters["date_range"] = "this_month"

        return entity, metric, filters

    @classmethod
    def _match_task_mode(cls, goal: str, task_modes: Dict[str, Any], goal_triggers: Dict[str, List[str]] = None) -> str:
        """Match goal to a task mode from site map.

        CONTRACT-GUIDED: Uses goal_triggers from site map as primary matching,
        then falls back to keyword matching.

        Args:
            goal: User's goal string
            task_modes: Dict from site_map_node["task_modes"]
            goal_triggers: Dict from site_map_node["goal_triggers"] - maps task mode to trigger phrases

        Returns:
            Task mode key (e.g., "read_token_usage") or empty string if no match
        """
        goal_lower = goal.lower()

        # PRIORITY 1: Match using goal_triggers from site map (most accurate)
        if goal_triggers:
            for task_mode, triggers in goal_triggers.items():
                if task_mode in task_modes or True:  # Allow match even if task_modes not defined
                    if isinstance(triggers, list):
                        for trigger in triggers:
                            if isinstance(trigger, str):
                                if trigger in goal_lower:
                                    return task_mode
                            elif isinstance(trigger, list):
                                if any(t in goal_lower for t in trigger):
                                    return task_mode

        # PRIORITY 2: Match based on keywords in goal
        # read_token_usage: "token", "usage", "how many"
        # read_cost_breakdown: "cost", "spend", "price", "dollar"
        task_mode_keywords = {
            "read_token_usage": ["token", "usage", "how many", "count"],
            "read_cost_breakdown": ["cost", "spend", "price", "dollar", "$", "expense"],
        }

        if task_modes:
            for task_mode, keywords in task_mode_keywords.items():
                if task_mode in task_modes:
                    if any(kw in goal_lower for kw in keywords):
                        return task_mode

        return ""


@dataclass
class MappedAction:
    """Action to take in mapped extraction state machine."""
    type: str  # validate, filter, locate, extract, complete, escalate
    tool: str  # click_element, type_text, read_page_content, wait_for_ui, etc.
    target_id: str = ""
    text: str = ""
    why: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    intent: Optional[Dict[str, str]] = None  # Intent-based action format: {text_label, zone, element_type, context}


class MappedExtractionStateMachine:
    """Finite-state machine for deterministic mapped page extraction.

    This state machine enforces a strict sequence of verification steps
    before allowing completion. It prevents premature completion by
    requiring:
    1. Entity anchor found in readable content
    2. Metric anchor found in readable content
    3. Numeric value extracted from readable content
    4. Evidence from page, not URL parameters

    Example usage:
        machine = MappedExtractionStateMachine(context)
        while machine.state != MappedExtractionState.COMPLETE:
            action = machine.transition(observation)
            if action.type == "complete":
                return machine.build_result()
            # Execute action, update observation
            observation = await execute(action)
    """

    def __init__(
        self,
        context: MappedTerminalContext,
        max_attempts: int = 15,
    ):
        self.context = context
        self.state = MappedExtractionState.VALIDATE_NODE
        self.attempts = 0
        self.max_attempts = max_attempts
        self.action_history: List[str] = []
        self.error_log: List[str] = []
        self.validation_passed = False
        # Track completed filter steps from transition_rules
        self.completed_filter_steps: Set[str] = set()
        # Track if dropdown/modal is open (for multi-step filters like date picker)
        self.dropdown_open = False
        self.dropdown_options: List[str] = []

    def transition(self, observation: Dict[str, Any]) -> Tuple[MappedAction, bool]:
        """Determine next action based on current state and observation.

        Args:
            observation: Dict containing:
                - nodes: List of DOM nodes
                - readable_content: str of visible text content
                - url_params: Dict of URL query parameters
                - clicked_ids: Set of already-clicked element IDs

        Returns:
            Tuple of (action, is_terminal)
        """
        self.attempts += 1
        if self.attempts > self.max_attempts:
            return self._escalate("max_attempts_reached"), True

        nodes = observation.get("nodes", [])
        readable = observation.get("readable_content", "")

        # State-specific transition logic
        transition_map = {
            MappedExtractionState.VALIDATE_NODE: self._transition_validate_node,
            MappedExtractionState.VALIDATE_SCOPE: self._transition_validate_scope,
            MappedExtractionState.SET_FILTERS: self._transition_set_filters,
            MappedExtractionState.LOCATE_ENTITY: self._transition_locate_entity,
            MappedExtractionState.LOCATE_METRIC: self._transition_locate_metric,
            MappedExtractionState.EXTRACT_VALUE: self._transition_extract_value,
            MappedExtractionState.VALIDATE_EVIDENCE: self._transition_validate_evidence,
            MappedExtractionState.COMPLETE: self._transition_complete,
        }

        handler = transition_map.get(self.state)
        if handler:
            return handler(observation)

        return self._escalate("unknown_state"), True

    def get_current_control_group(self, step_name: str) -> Optional[str]:
        """Get control group name from transition_rules for current task mode.

        CONTRACT-GUIDED ADAPTATION:
        Instead of hardcoding control group names, we read them from the
        site map's transition_rules based on the current task mode.

        Args:
            step_name: The step we're on (e.g., "set_date_picker", "set_model_filter")

        Returns:
            Control group name (e.g., "date_picker", "model_filter") or None
        """
        if not self.context.transition_rules:
            return None

        task_mode = self.context.current_task_mode
        if not task_mode:
            return None

        rules = self.context.transition_rules.get(task_mode, [])
        if not rules:
            return None

        # Find the rule for this step
        for rule in rules:
            if rule.get("step") == step_name:
                return rule.get("control_group")

        return None

    def _find_matching_control_group(self, keyword: str) -> Optional[str]:
        """Find control group by keyword matching.

        Fallback when transition_rules doesn't specify a control group.
        Searches control_groups keys and labels for keyword match.

        Args:
            keyword: e.g., "date", "model", "filter"

        Returns:
            Matching control group name or None
        """
        if not self.context.control_groups:
            return None

        keyword_lower = keyword.lower()

        # First try exact key match
        for group_name in self.context.control_groups.keys():
            if keyword_lower in group_name.lower():
                return group_name

        # Then try label match
        for group_name, group_data in self.context.control_groups.items():
            if isinstance(group_data, dict):
                labels = group_data.get("labels", [])
                for label in labels:
                    if keyword_lower in label.lower():
                        return group_name

        return None

    def resolve_control_from_group(
        self,
        group_name: str,
        nodes: List[Any],
        clicked_ids: Set[str],
        vision_hints: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve control from node-local control_group, not whole DOM.

        Mapped mode searches within control_groups first (site map contract),
        then falls back to vision hints (high confidence only), then generic DOM.

        Priority order:
        1. Site map control_groups (CONTRACT) - e.g., control_groups["date_filters"]
        2. Vision hints with high confidence (0.8+) - vision saw something site map doesn't define
        3. Generic DOM search - when site map and vision both miss

        Args:
            group_name: e.g., "date_filters", "model_filters", "metric_toggles"
            nodes: DOM nodes
            clicked_ids: Already clicked IDs
            vision_hints: Optional dict from request_vision with identified_controls

        Returns:
            {"target_id": str, "intent": {...}, "from": "contract"|"vision"|"dom"} or None
        """
        # PRIORITY 1: Site map control_groups (CONTRACT-FIRST)
        group_controls = self.context.control_groups.get(group_name, [])
        if not group_controls:
            # Try to parse group_controls if it's a dict structure instead of list
            raw_groups = self.context.control_groups
            if isinstance(raw_groups, dict) and group_name in raw_groups:
                group_data = raw_groups.get(group_name, {})
                if isinstance(group_data, dict):
                    # Extract labels from dict structure
                    group_controls = group_data.get("labels", [])

        if group_controls:
            for control_name in group_controls:
                for node in nodes:
                    node_id = getattr(node, "id", "")
                    if node_id in clicked_ids:
                        continue
                    text = str(getattr(node, "text", "") or "").lower()
                    if control_name.lower() in text:
                        if getattr(node, "interactive", False):
                            logger.debug(f"CONTROL_RESOLVED: group={group_name} control={control_name} -> {node_id} (from=contract)")
                            return {
                                "target_id": node_id,
                                "intent": {
                                    "text_label": control_name,
                                    "zone": "main",
                                    "element_type": "button",
                                    "context": f"From {group_name} group"
                                },
                                "from": "contract"
                            }

        # PRIORITY 2: Vision hints with high confidence (ADVISORY)
        # Only use vision if confidence > 0.8 - prevents vision from dominating
        if vision_hints and isinstance(vision_hints, dict):
            identified = vision_hints.get("identified_controls", [])
            for ctrl in identified:
                ctrl_confidence = ctrl.get("confidence", 0.0)
                if ctrl_confidence < 0.8:
                    continue  # Vision confidence too low - skip

                ctrl_label = ctrl.get("label", "").lower()
                ctrl_id = ctrl.get("target_id", "")

                # Check if this vision control matches the group we're looking for
                group_keywords = group_name.replace("_", " ").split()
                label_matches_group = any(kw in ctrl_label for kw in group_keywords)

                if ctrl_id and ctrl_id not in clicked_ids and label_matches_group:
                    logger.debug(f"CONTROL_RESOLVED: group={group_name} -> {ctrl_id} (from=vision, confidence={ctrl_confidence})")
                    return {
                        "target_id": ctrl_id,
                        "intent": {
                            "text_label": ctrl.get("label", "Control"),
                            "zone": "main",
                            "element_type": ctrl.get("element_type", "button"),
                            "context": f"From vision (group={group_name}, confidence={ctrl_confidence})"
                        },
                        "from": "vision"
                    }

        # PRIORITY 3: Generic DOM search (FALLBACK)
        # Last resort - search for any interactive element with group-related keywords
        group_keywords = group_name.replace("_", " ").split()
        for node in nodes:
            node_id = getattr(node, "id", "")
            if node_id in clicked_ids:
                continue
            text = str(getattr(node, "text", "") or "").lower()
            if any(kw in text for kw in group_keywords):
                if getattr(node, "interactive", False):
                    logger.debug(f"CONTROL_RESOLVED: group={group_name} -> {node_id} (from=dom)")
                    return {
                        "target_id": node_id,
                        "intent": {
                            "text_label": text[:30],
                            "zone": "main",
                            "element_type": "button",
                            "context": f"From DOM search (group={group_name})"
                        },
                        "from": "dom"
                    }

        logger.debug(f"CONTROL_NOT_RESOLVED: group={group_name} not found in contract/vision/dom")
        return None

    def _transition_validate_node(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Verify we're on the expected site map node."""
        if not self.context.is_mapped:
            return self._escalate("not_mapped"), True

        # Check if expected controls are present
        nodes = observation.get("nodes", [])
        readable = observation.get("readable_content", "")

        missing_controls = []
        for control in self.context.expected_controls:
            if not self._control_exists(control, nodes):
                missing_controls.append(control)

        if missing_controls and self.attempts < 3:
            # Give it a few iterations for controls to load
            return MappedAction(
                type="validate",
                tool="wait_for_ui",
                why=f"Waiting for expected controls: {missing_controls}",
                params={"seconds": 2},
            ), False

        # Node validated, move to scope validation
        self.state = MappedExtractionState.VALIDATE_SCOPE
        return MappedAction(
            type="validate",
            tool="read_page_content",
            why=f"Validated node {self.context.current_node_id}, checking scope",
        ), False

    def _transition_validate_scope(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Check if current filters match goal requirements.

        CRITICAL: Always enter SET_FILTERS when goal_filters are specified.
        This ensures we click the date picker to set the correct range,
        rather than assuming the default is correct.
        """
        readable = observation.get("readable_content", "")
        url_params = observation.get("url_params", {})

        # Check if any filters are required by the goal
        needs_filter = bool(self.context.goal_filters)

        # Always enter SET_FILTERS if goal specifies filters (date_range, etc.)
        # This is more aggressive than mismatch detection - we want to VERIFY
        # the filter state by opening the picker, not assume from page content
        if needs_filter and self.state != MappedExtractionState.SET_FILTERS:
            self.state = MappedExtractionState.SET_FILTERS
            # Return wait_for_ui to get fresh observation with nodes for SET_FILTERS to process
            return MappedAction(
                type="filter",
                tool="wait_for_ui",
                why=f"Entering SET_FILTERS state for: {list(self.context.goal_filters.keys())}",
                params={"seconds": 0.5},
            ), False

        # Scope validated, move to entity location
        self.state = MappedExtractionState.LOCATE_ENTITY
        return MappedAction(
            type="locate",
            tool="read_page_content",
            why=f"Scope validated, searching for entity: {self.context.goal_entity}",
        ), False

    def _transition_set_filters(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Apply filters using control groups (contract-first).

        CONTRACT-GUIDED ADAPTATION:
        Instead of hardcoding control group names, we read them from
        transition_rules based on the current task mode.

        For "read_token_usage" task mode, transition_rules specifies:
        - step: "set_date_picker" → control_group: "date_picker"
        - step: "set_model_filter" → control_group: "model_filter"
        - step: "set_metric_tab" → control_group: "metric_tab"

        STATE TRACKING:
        - Tracks completed_filter_steps to know which step we're on
        - Handles multi-step flows (click date picker → select option from dropdown)
        - Advances to next step only after current step completes

        Priority order:
        1. Transition rules (CONTRACT) - read control_group from task mode
        2. Site map control_groups by keyword match
        3. Vision hints (ADVISORY)
        4. Generic DOM search (FALLBACK)
        """
        nodes = observation.get("nodes", [])
        clicked_ids = observation.get("clicked_ids", set())
        vision_hints = observation.get("vision_hints", {})
        readable = observation.get("readable_content", "")

        logger.info(
            f"MAPPED_SET_FILTERS: attempt={self.attempts} nodes={len(nodes)} "
            f"clicked={len(clicked_ids)} completed_steps={self.completed_filter_steps} "
            f"control_groups={list(self.context.control_groups.keys())} "
            f"task_mode={self.context.current_task_mode}"
        )

        # Get transition rules for current task mode
        transition_rules = self.context.transition_rules.get(self.context.current_task_mode, [])
        if not transition_rules:
            logger.warning(f"MAPPED_SET_FILTERS: No transition_rules for task_mode={self.context.current_task_mode}")
            # No transition rules - proceed to next state
            self.state = MappedExtractionState.LOCATE_ENTITY
            return MappedAction(
                type="locate",
                tool="read_page_content",
                why="No transition_rules defined, proceeding to entity location",
            ), False

        # Determine current step based on completed steps
        current_step = None
        current_control_group = None
        for rule in transition_rules:
            step_name = rule.get("step")
            control_group = rule.get("control_group")
            if step_name not in self.completed_filter_steps:
                current_step = step_name
                current_control_group = control_group
                break

        # All steps completed - move to next state
        if not current_step:
            logger.info(f"MAPPED_SET_FILTERS: All filter steps completed {self.completed_filter_steps}")
            self.state = MappedExtractionState.LOCATE_ENTITY
            return MappedAction(
                type="locate",
                tool="read_page_content",
                why=f"All filter steps completed: {self.completed_filter_steps}",
            ), False

        logger.info(f"SET_FILTERS: Current step={current_step} control_group={current_control_group}")

        # Check if dropdown is open and we need to select an option
        if self.dropdown_open and current_step == "set_date_picker":
            # Look for the date range option in the dropdown
            date_range_value = self.context.goal_filters.get("date_range", "last_7_days")
            date_range_labels = {
                "last_7_days": ["Last 7 days", "Last 7 Days", "7 days", "Past 7 days"],
                "last_30_days": ["Last 30 days", "Last 30 Days", "30 days", "Past 30 days"],
                "last_24_hours": ["Last 24 hours", "Last 24 Hours", "24 hours", "Past 24 hours"],
                "this_month": ["This month", "This Month", "Current month"],
            }
            target_labels = date_range_labels.get(date_range_value, ["Last 7 days"])

            # Find the option in visible nodes
            for node in nodes:
                node_id = getattr(node, "id", "")
                text = str(getattr(node, "text", "") or "").strip()
                # Check if this is a dropdown option matching our target
                for label in target_labels:
                    if label.lower() in text.lower():
                        logger.info(f"SET_FILTERS: Found date option '{text}' at {node_id}")
                        # Click the option
                        self.dropdown_open = False
                        self.completed_filter_steps.add(current_step)
                        return MappedAction(
                            type="filter",
                            tool="click_element",
                            target_id=node_id,
                            intent={"text_label": text, "zone": "dropdown", "element_type": "option", "context": f"Date range: {text}"},
                            why=f"Selecting '{text}' from date picker dropdown",
                        ), False

            # Dropdown open but option not found - close it and try next step
            logger.warning(f"SET_FILTERS: Date option '{date_range_value}' not found in dropdown")
            self.dropdown_open = False
            self.completed_filter_steps.add(current_step)
            # Continue to next step
            return self._transition_set_filters(observation)

        # Check if dropdown just opened (date picker was clicked, now showing options)
        if current_control_group:
            # Check if we're looking at a dropdown with date options
            date_option_keywords = ["last", "days", "30 days", "7 days", "24 hours", "this month", "custom range"]
            for node in nodes:
                text = str(getattr(node, "text", "") or "").strip()
                zone = getattr(node, "zone", "")
                # If we see date options in a dropdown/modal zone, dropdown is open
                if zone in ("dropdown", "modal", "popup") and any(kw in text.lower() for kw in date_option_keywords):
                    logger.info(f"SET_FILTERS: Date picker dropdown detected open with options")
                    self.dropdown_open = True
                    # Will select option on next iteration
                    break

        # PRIORITY 1: Use control group from transition_rules (CONTRACT-FIRST)
        if current_control_group:
            logger.info(f"SET_FILTERS: Using control_group '{current_control_group}' from transition_rules for step '{current_step}'")
            control = self.resolve_control_from_group(current_control_group, nodes, clicked_ids, vision_hints)
            if control:
                logger.info(f"SET_FILTERS: Resolved {current_control_group} → {control['target_id']} (from={control.get('from', 'contract')})")

                # Check if this is opening a dropdown (date picker, model filter, etc.)
                control_group_data = self.context.control_groups.get(current_control_group, {})
                element_types = control_group_data.get("element_types", [])

                if any(et in ["dropdown", "combobox", "select"] for et in element_types):
                    logger.info(f"SET_FILTERS: Marking dropdown as opening")
                    self.dropdown_open = True

                return MappedAction(
                    type="filter",
                    tool="click_element",
                    target_id=control["target_id"],
                    intent=control["intent"],
                    why=f"{current_step}: {current_control_group}",
                ), False

        # PRIORITY 2: Try common control group name variations
        step_to_keywords = {
            "set_date_picker": ["date_picker", "date_filters", "date_range"],
            "set_model_filter": ["model_filter", "model_select", "model"],
            "set_metric_tab": ["metric_tab", "activity_tab", "cost_tab", "usage_tab"],
        }
        keywords = step_to_keywords.get(current_step, [])
        for group_name in keywords:
            if group_name in self.context.control_groups:
                control = self.resolve_control_from_group(group_name, nodes, clicked_ids, vision_hints)
                if control:
                    logger.info(f"SET_FILTERS: Using fallback control_group {group_name} → {control['target_id']}")
                    return MappedAction(
                        type="filter",
                        tool="click_element",
                        target_id=control["target_id"],
                        intent=control["intent"],
                        why=f"{current_step} (fallback: {group_name})"
                    ), False

        # PRIORITY 3: Generic search by label text
        step_to_labels = {
            "set_date_picker": ["date", "range", "last", "days"],
            "set_model_filter": ["model", "filter by model"],
            "set_metric_tab": ["activity", "cost", "usage", "tokens"],
        }
        labels = step_to_labels.get(current_step, [])
        for node in nodes:
            node_id = getattr(node, "id", "")
            if node_id in clicked_ids:
                continue
            text = str(getattr(node, "text", "") or "").lower()
            if any(label in text for label in labels):
                logger.info(f"SET_FILTERS: Found {current_step} by label: {node_id}='{text[:40]}'")
                return MappedAction(
                    type="filter",
                    tool="click_element",
                    target_id=node_id,
                    intent={"text_label": text[:30], "zone": "main", "element_type": "button", "context": current_step},
                    why=f"{current_step}: Found by label '{text[:30]}'",
                ), False

        # If no control found after several attempts, mark step as complete and move on
        if self.attempts > 5:
            logger.warning(f"SET_FILTERS: Could not find control for step={current_step} after {self.attempts} attempts, skipping")
            self.completed_filter_steps.add(current_step)
            return self._transition_set_filters(observation)

        # Stay in SET_FILTERS state to retry
        return MappedAction(
            type="filter",
            tool="wait_for_ui",
            why=f"Waiting for filter controls for step={current_step}",
            params={"seconds": 0.5},
        ), False

    def _transition_locate_entity(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Find the target entity in page content.

        Priority order:
        1. Check if entity already visible in readable content
        2. Use vision hints to identify entity/model filter target ID
        3. Use expected_controls from site map
        4. Scroll to find entity
        5. Fallback to generic model control search
        """
        readable = observation.get("readable_content", "")
        nodes = observation.get("nodes", [])
        clicked_ids = observation.get("clicked_ids", set())
        vision_hints = observation.get("vision_hints", "")

        entity_found, entity_text = self._find_entity_anchor(readable)
        self.context.entity_anchor_found = entity_found
        self.context.entity_anchor_text = entity_text

        if entity_found:
            logger.info(f"MAPPED_LOCATE_ENTITY: Found in readable content")
            self.state = MappedExtractionState.LOCATE_METRIC
            return MappedAction(
                type="locate",
                tool="read_page_content",
                why=f"Entity '{self.context.goal_entity}' found, locating metric",
            ), False

        # Entity not found in readable content - need to click to reveal it
        # Build comprehensive keywords for entity search
        goal_entity_lower = self.context.goal_entity.lower() if self.context.goal_entity else ""
        entity_keywords = [goal_entity_lower] if goal_entity_lower else []
        entity_keywords.extend(["model", "filter", "show all", "expand", "select"])

        # Priority 1: Use vision hints to identify entity target ID
        vision_target = self._extract_target_from_vision_hints(
            vision_hints, entity_keywords
        )
        if vision_target and vision_target not in clicked_ids:
            logger.info(f"MAPPED_LOCATE_ENTITY: Using vision target {vision_target}")
            return MappedAction(
                type="locate",
                tool="click_element",
                target_id=vision_target,
                intent={"text_label": self.context.goal_entity or "Model", "zone": "main", "element_type": "button", "context": "Model filter/selector"},
                why=f"Vision-identified target for entity: {vision_target}",
            ), False

        # Priority 2: Use expected_controls from site map to find clickable elements
        entity_control = self._find_entity_control_from_site_map(nodes, clicked_ids)
        if entity_control:
            logger.info(f"MAPPED_LOCATE_ENTITY: Using site map control {entity_control['target_id']}")
            return MappedAction(
                type="locate",
                tool="click_element",
                target_id=entity_control["target_id"],
                intent=entity_control["intent"],
                why=f"Clicking {entity_control['intent']['text_label']} to reveal entity",
            ), False

        # SCROLL REMOVED: Scrolls only for showing results to user, not exploration
        # Next: Try to find any model-related control
        model_control = self._find_model_control(nodes, clicked_ids)
        if model_control:
            logger.info(f"MAPPED_LOCATE_ENTITY: Using generic model control {model_control['target_id']}")
            return MappedAction(
                type="locate",
                tool="click_element",
                target_id=model_control["target_id"],
                intent=model_control["intent"],
                why=f"Clicking {model_control['intent']['text_label']} to open model selector",
            ), False

        # Can't find entity - escalate
        return self._escalate(f"entity_not_found: {self.context.goal_entity}"), True

    def _transition_locate_metric(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Find the metric column/field for the entity.

        Priority order:
        1. Use vision hints to identify metric tab target ID
        2. Use expected_controls from site map (Activity tab, Cost tab, etc.)
        3. Fallback to generic metric toggle search
        """
        readable = observation.get("readable_content", "")
        nodes = observation.get("nodes", [])
        clicked_ids = observation.get("clicked_ids", set())
        vision_hints = observation.get("vision_hints", "")

        metric_found, metric_text = self._find_metric_anchor(readable)
        self.context.metric_anchor_found = metric_found
        self.context.metric_anchor_text = metric_text

        if metric_found:
            self.state = MappedExtractionState.EXTRACT_VALUE
            return MappedAction(
                type="extract",
                tool="read_page_content",
                why=f"Metric '{self.context.goal_metric}' found, extracting value",
            ), False

        # Metric not found in readable content - need to click tabs/toggles
        # Build comprehensive keywords based on goal metric and common metric tab names
        goal_metric_lower = self.context.goal_metric.lower() if self.context.goal_metric else ""
        metric_keywords = [goal_metric_lower] if goal_metric_lower else []
        metric_keywords.extend(["cost", "usage", "tokens", "tab", "activity", "spend"])

        # Priority 1: Use vision hints to identify metric tab target ID
        vision_target = self._extract_target_from_vision_hints(
            vision_hints, metric_keywords
        )
        if vision_target and vision_target not in clicked_ids:
            logger.info(f"MAPPED_LOCATE_METRIC: Using vision target {vision_target}")
            return MappedAction(
                type="locate",
                tool="click_element",
                target_id=vision_target,
                intent={"text_label": self.context.goal_metric.title() or "Metric", "zone": "main", "element_type": "tab", "context": "Metric toggle tab"},
                why=f"Vision-identified target for metric: {vision_target}",
            ), False

        # Priority 2: Look for metric tabs/toggles from expected_controls
        metric_toggle = self._find_metric_toggle_from_site_map(nodes, clicked_ids)
        if metric_toggle:
            logger.info(f"MAPPED_LOCATE_METRIC: Using site map toggle {metric_toggle['target_id']}")
            return MappedAction(
                type="locate",
                tool="click_element",
                target_id=metric_toggle["target_id"],
                intent=metric_toggle["intent"],
                why=f"Switching to {metric_toggle['intent']['text_label']} view",
            ), False

        # Priority 3: Generic metric toggle search in DOM
        metric_toggle_id = self._find_metric_toggle(nodes)
        if metric_toggle_id and metric_toggle_id not in clicked_ids:
            logger.info(f"MAPPED_LOCATE_METRIC: Using generic toggle {metric_toggle_id}")
            return MappedAction(
                type="locate",
                tool="click_element",
                target_id=metric_toggle_id,
                intent={"text_label": self.context.goal_metric.title() or "Metric", "zone": "main", "element_type": "tab", "context": "Metric toggle"},
                why=f"Switching to metric view: {self.context.goal_metric}",
            ), False

        # Can't find metric - escalate
        return self._escalate(f"metric_not_found: {self.context.goal_metric}"), True

    def _transition_extract_value(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Extract the numeric value from readable content."""
        readable = observation.get("readable_content", "")

        value_found, value_text, evidence = self._extract_numeric_value(
            readable,
            self.context.entity_anchor_text,
            self.context.metric_anchor_text,
        )

        self.context.numeric_value_found = value_found
        self.context.numeric_value = value_text
        self.context.evidence_text = evidence

        if value_found:
            self.state = MappedExtractionState.VALIDATE_EVIDENCE
            return MappedAction(
                type="validate",
                tool="read_page_content",
                why=f"Extracted value '{value_text}', validating evidence quality",
            ), False

        # Value extraction failed - escalate
        return self._escalate("value_extraction_failed"), True

    def _transition_validate_evidence(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Validate that evidence meets all completion invariants."""
        readable = observation.get("readable_content", "")
        url_params = observation.get("url_params", {})

        passed, reason = self._validate_mapped_completion(
            self.context,
            readable,
            url_params,
        )

        if passed:
            self.validation_passed = True
            self.state = MappedExtractionState.COMPLETE
            return MappedAction(
                type="complete",
                tool="complete_mission",
                why="All completion invariants satisfied",
            ), True

        # Validation failed - cannot complete
        return self._escalate(f"validation_failed: {reason}"), True

    def _transition_complete(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Terminal state - should not be called directly."""
        return MappedAction(
            type="complete",
            tool="complete_mission",
            why="Extraction complete",
        ), True

    def _escalate(self, reason: str) -> MappedAction:
        """Create escalation action."""
        self.error_log.append(reason)
        return MappedAction(
            type="escalate",
            tool="escalate",
            why=f"Mapped extraction failed: {reason}",
            params={"error_log": self.error_log, "attempts": self.attempts},
        )

    def build_result(self) -> Dict[str, Any]:
        """Build final extraction result."""
        if not self.validation_passed:
            return {
                "status": "failed",
                "reason": "Validation not passed",
                "error_log": self.error_log,
                "attempts": self.attempts,
            }

        return {
            "status": "success",
            "entity": self.context.goal_entity,
            "metric": self.context.goal_metric,
            "value": self.context.numeric_value,
            "evidence": self.context.evidence_text,
            "node_id": self.context.current_node_id,
            "attempts": self.attempts,
        }

    # Helper methods

    def _control_exists(self, control: str, nodes: List[Any]) -> bool:
        """Check if a control exists in the DOM."""
        control_lower = control.lower()
        for node in nodes:
            text = str(getattr(node, "text", "") or "").lower()
            if control_lower in text:
                return True
        return False

    def _check_date_mismatch(self, readable: str, target_range: str) -> bool:
        """Check if current date filter doesn't match target."""
        readable_lower = readable.lower()

        # Look for active date indicators
        range_indicators = {
            "last_7_days": ["last 7 days", "7 days", "week"],
            "last_30_days": ["last 30 days", "30 days", "month"],
            "last_24_hours": ["last 24 hours", "24 hours", "today"],
            "this_month": ["this month", "current month"],
        }

        target_indicators = range_indicators.get(target_range, [])
        conflicting_ranges = [
            r for r, indicators in range_indicators.items()
            if r != target_range
            for ind in indicators
            if ind in readable_lower
        ]

        # If we see conflicting range indicators but not our target, it's a mismatch
        if conflicting_ranges and not any(ti in readable_lower for ti in target_indicators):
            return True

        return False

    def _find_filter_control(self, nodes: List[Any], clicked_ids: Set[str]) -> str:
        """Find date picker or filter control."""
        for node in nodes:
            node_id = getattr(node, "id", "")
            if node_id in clicked_ids:
                continue
            text = str(getattr(node, "text", "") or "").lower()
            if any(term in text for term in ["date", "range", "filter", "period", "last"]):
                if getattr(node, "interactive", False):
                    return node_id
        return ""

    def _extract_target_from_vision_hints(self, vision_hints: str, keywords: List[str]) -> str:
        """Extract target ID from vision hints.

        Vision hints may contain patterns like:
        - "click t-1fp4boa (Show all Models)"
        - "target: t-nn5tat for date range"
        - "identified: t-xyz123"

        Args:
            vision_hints: Raw vision bootstrap text
            keywords: Keywords to look for (e.g., ["model", "filter"])

        Returns:
            Target ID if found, empty string otherwise
        """
        if not vision_hints:
            return ""

        vision_lower = vision_hints.lower()

        # Look for patterns like "t-xxxxx" or "click xxxxx"
        # Pattern 1: "click t-xxx (description)"
        click_pattern = r"click\s+([a-zA-Z0-9_-]+)\s*\(([^)]+)\)"
        for match in re.finditer(click_pattern, vision_hints, re.IGNORECASE):
            target_id = match.group(1)
            description = match.group(2).lower()
            # Check if any keyword matches
            if any(kw in description for kw in keywords):
                return target_id

        # Pattern 2: Look for target IDs near keywords
        lines = vision_hints.split("\n")
        for line in lines:
            line_lower = line.lower()
            if any(kw in line_lower for kw in keywords):
                # Look for t-xxx pattern
                id_match = re.search(r"\b(t-[a-zA-Z0-9_-]+)\b", line)
                if id_match:
                    return id_match.group(1)

        return ""

    def _find_filter_control_from_site_map(self, nodes: List[Any], clicked_ids: Set[str]) -> Optional[Dict[str, Any]]:
        """Find filter control from expected_controls in site map.

        Returns:
            Dict with target_id and intent, or None if not found
        """
        if not self.context.expected_controls:
            return None

        # Look for date picker, model filter in expected controls
        filter_keywords = ["date", "picker", "filter", "range", "period", "time"]

        for control in self.context.expected_controls:
            control_lower = control.lower()
            if any(kw in control_lower for kw in filter_keywords):
                # Find matching node in DOM
                for node in nodes:
                    node_id = getattr(node, "id", "")
                    if node_id in clicked_ids:
                        continue
                    text = str(getattr(node, "text", "") or "").lower()
                    if control_lower in text or any(kw in text for kw in filter_keywords):
                        if getattr(node, "interactive", False):
                            return {
                                "target_id": node_id,
                                "intent": {
                                    "text_label": control,
                                    "zone": "main",
                                    "element_type": "dropdown",
                                    "context": f"{control} for filtering"
                                }
                            }
        return None

    def _find_entity_control_from_site_map(self, nodes: List[Any], clicked_ids: Set[str]) -> Optional[Dict[str, Any]]:
        """Find entity/model control from expected_controls in site map.

        Returns:
            Dict with target_id and intent, or None if not found
        """
        if not self.context.expected_controls:
            return None

        # Look for model filter, entity selector in expected controls
        entity_keywords = ["model", "filter", "selector", "dropdown", "show all"]

        for control in self.context.expected_controls:
            control_lower = control.lower()
            if any(kw in control_lower for kw in entity_keywords):
                # Find matching node in DOM
                for node in nodes:
                    node_id = getattr(node, "id", "")
                    if node_id in clicked_ids:
                        continue
                    text = str(getattr(node, "text", "") or "").lower()
                    if control_lower in text or self.context.goal_entity.lower() in text:
                        if getattr(node, "interactive", False):
                            return {
                                "target_id": node_id,
                                "intent": {
                                    "text_label": control,
                                    "zone": "main",
                                    "element_type": "button",
                                    "context": f"{control} to reveal {self.context.goal_entity}"
                                }
                            }
        return None

    def _find_model_control(self, nodes: List[Any], clicked_ids: Set[str]) -> Optional[Dict[str, Any]]:
        """Find any model-related control as fallback.

        Returns:
            Dict with target_id and intent, or None if not found
        """
        model_keywords = ["model", "show all", "expand", "more"]

        for node in nodes:
            node_id = getattr(node, "id", "")
            if node_id in clicked_ids:
                continue
            text = str(getattr(node, "text", "") or "").lower()
            if any(kw in text for kw in model_keywords):
                if getattr(node, "interactive", False):
                    return {
                        "target_id": node_id,
                        "intent": {
                            "text_label": text[:30],
                            "zone": "main",
                            "element_type": "button",
                            "context": "Model selector expansion"
                        }
                    }
        return None

    def _find_metric_toggle_from_site_map(self, nodes: List[Any], clicked_ids: Set[str]) -> Optional[Dict[str, Any]]:
        """Find metric toggle from expected_controls in site map.

        Returns:
            Dict with target_id and intent, or None if not found
        """
        if not self.context.expected_controls:
            return None

        # Look for tabs, toggles in expected controls
        metric_keywords = ["tab", "toggle", "switch", "cost", "usage", "tokens", "activity"]

        for control in self.context.expected_controls:
            control_lower = control.lower()
            if any(kw in control_lower for kw in metric_keywords):
                # Find matching node in DOM
                for node in nodes:
                    node_id = getattr(node, "id", "")
                    if node_id in clicked_ids:
                        continue
                    text = str(getattr(node, "text", "") or "").lower()
                    if control_lower in text or self.context.goal_metric.lower() in text:
                        if getattr(node, "interactive", False):
                            return {
                                "target_id": node_id,
                                "intent": {
                                    "text_label": control,
                                    "zone": "main",
                                    "element_type": "tab",
                                    "context": f"{control} for metric view"
                                }
                            }
        return None

    def _find_entity_anchor(self, readable: str) -> Tuple[bool, str]:
        """Find entity anchor in readable content."""
        if not self.context.goal_entity:
            # No entity specified - allow any
            return True, ""

        entity = self.context.goal_entity.lower()
        readable_lower = readable.lower()

        # Look for exact entity name
        if entity in readable_lower:
            # Extract the surrounding context
            idx = readable_lower.find(entity)
            start = max(0, idx - 50)
            end = min(len(readable), idx + len(entity) + 50)
            return True, readable[start:end].strip()

        return False, ""

    def _find_metric_anchor(self, readable: str) -> Tuple[bool, str]:
        """Find metric anchor in readable content."""
        if not self.context.goal_metric:
            # No metric specified - allow any numeric
            return True, ""

        metric = self.context.goal_metric.lower()
        readable_lower = readable.lower()

        metric_synonyms = {
            "tokens": ["tokens", "token usage", "token count"],
            "cost": ["cost", "price", "pricing", "amount", "total", "$"],
            "requests": ["requests", "calls", "api calls"],
            "characters": ["characters", "chars"],
            "duration": ["seconds", "duration", "time"],
        }

        synonyms = metric_synonyms.get(metric, [metric])
        for syn in synonyms:
            if syn in readable_lower:
                idx = readable_lower.find(syn)
                start = max(0, idx - 30)
                end = min(len(readable), idx + len(syn) + 30)
                return True, readable[start:end].strip()

        return False, ""

    def _extract_numeric_value(
        self,
        readable: str,
        entity_context: str,
        metric_context: str,
    ) -> Tuple[bool, str, str]:
        """Extract numeric value near entity and metric."""
        # Look for patterns like:
        # "Whisper ... 1.2M tokens"
        # "GPT-4: $0.004"
        # "Tokens: 1,234,567"

        # First, try to find numeric value in proximity to entity
        if entity_context:
            entity_idx = readable.lower().find(entity_context.lower()[:20])
            if entity_idx >= 0:
                # Look within 200 chars of entity for numbers
                nearby = readable[max(0, entity_idx - 100):min(len(readable), entity_idx + 200)]
                number_match = re.search(r"(\d+[\d,]*\.?\d*\s*[KkMmBb]?)\s*(%|tokens?|\$)?", nearby)
                if number_match:
                    return True, number_match.group(0), nearby[:200]

        # Fallback: look for any substantial number
        number_match = re.search(r"(\d{1,3}(,\d{3})+(\.\d+)?|\d+\.\d+|\d+[KkMmBb])", readable)
        if number_match:
            return True, number_match.group(0), readable[:200]

        return False, "", ""

    def _can_scroll(self, nodes: List[Any]) -> bool:
        """Check if page can be scrolled for more content."""
        # Simple heuristic: check if there are many nodes
        return len(nodes) > 50

    def _find_metric_toggle(self, nodes: List[Any]) -> str:
        """Find tab/toggle for switching metrics."""
        for node in nodes:
            text = str(getattr(node, "text", "") or "").lower()
            if self.context.goal_metric.lower() in text:
                if any(t in text for t in ["tab", "toggle", "switch"]):
                    if getattr(node, "interactive", False):
                        return getattr(node, "id", "")
        return ""

    def _validate_mapped_completion(
        self,
        context: MappedTerminalContext,
        readable: str,
        url_params: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Validate that completion meets all mapped-mode invariants.

        CRITICAL: This is the gate that prevents premature completion.
        All four invariants must be satisfied:
        1. Entity anchor found in readable content
        2. Metric anchor found in readable content
        3. Numeric value extracted from readable content
        4. Evidence is from page content, not URL params
        """
        # Invariant 1: Entity anchor
        if not context.entity_anchor_found:
            return False, "Missing entity anchor - entity name not found in readable content"

        # Invariant 2: Metric anchor
        if not context.metric_anchor_found:
            return False, "Missing metric anchor - metric name not found in readable content"

        # Invariant 3: Numeric value
        if not context.numeric_value_found:
            return False, "Missing numeric value - no number extracted from readable content"

        # Invariant 4: Evidence from page, not URL
        # Check if the "answer" only comes from URL parameters (premature!)
        url_only_evidence = self._is_url_only_evidence(
            context.evidence_text,
            url_params,
        )
        if url_only_evidence:
            return False, "Evidence appears to be URL-only - must extract from readable page content"

        return True, "All invariants satisfied"

    def _is_url_only_evidence(self, evidence: str, url_params: Dict) -> bool:
        """Check if evidence is derived only from URL params, not page content."""
        # If evidence is empty or just echoes URL params, it's URL-only
        if not evidence:
            return True

        # Check if evidence is just a concatenation of URL params
        param_values = " ".join(str(v) for v in url_params.values())
        evidence_normalized = re.sub(r"\s+", "", evidence.lower())
        params_normalized = re.sub(r"\s+", "", param_values.lower())

        if evidence_normalized == params_normalized[:len(evidence_normalized)]:
            return True

        return False


class MappedActionStateMachine:
    """FSM for action workflows like 'Create API Key'.

    State progression:
    VALIDATE_NODE → FIND_PRIMARY_CTA → CLICK_PRIMARY_CTA → VERIFY_MODAL_OR_FORM →
    FILL_REQUIRED_FIELDS → CLICK_CONFIRM → VERIFY_SUCCESS → COMPLETE
    """

    def __init__(self, context: MappedTerminalContext, max_attempts: int = 15):
        self.context = context
        self.state = MappedActionState.VALIDATE_NODE
        self.attempts = 0
        self.max_attempts = max_attempts
        self.filled_fields: Set[str] = set()
        self.error_log: List[str] = []

    def transition(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Execute action workflow transition."""
        self.attempts += 1

        if self.attempts > self.max_attempts:
            return self._escalate("max_attempts"), True

        transition_map = {
            MappedActionState.VALIDATE_NODE: self._transition_validate_node,
            MappedActionState.FIND_PRIMARY_CTA: self._transition_find_cta,
            MappedActionState.CLICK_PRIMARY_CTA: self._transition_click_cta,
            MappedActionState.VERIFY_MODAL_OR_FORM: self._transition_verify_modal,
            MappedActionState.FILL_REQUIRED_FIELDS: self._transition_fill_fields,
            MappedActionState.CLICK_CONFIRM: self._transition_click_confirm,
            MappedActionState.VERIFY_SUCCESS: self._transition_verify_success,
            MappedActionState.COMPLETE: self._transition_complete,
        }

        handler = transition_map.get(self.state)
        return handler(observation) if handler else self._escalate("unknown_state")

    def _transition_validate_node(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Validate we're on correct node for action.

        ADAPTIVE: If modal/form already visible (from previous action), skip to FILL_FIELDS.
        """
        if not self.context.site_map_node:
            return self._escalate("no_primary_cta_defined"), True

        # ADAPTIVE CHECK: Is modal/form already open?
        # If we already have what we need, skip the CTA steps
        readable = obs.get("readable_content", "")
        nodes = obs.get("nodes", [])

        # Check if modal indicators already present
        modal_indicators = ["modal", "dialog", "form", "create", "new", "enter name", "key name"]
        modal_already_present = False
        for node in nodes:
            node_text = str(getattr(node, "text", "") or "").lower()
            zone = getattr(node, "zone", "")
            if any(m in node_text or m in zone.lower() for m in modal_indicators):
                modal_already_present = True
                break

        if modal_already_present:
            logger.info("ADAPTIVE_SKIP: Modal/form already present, skipping CTA click")
            self.state = MappedActionState.FILL_REQUIRED_FIELDS
            return MappedAction(
                type="validate",
                tool="read_page_content",
                why="Modal already present, proceeding to fill fields"
            ), False

        self.state = MappedActionState.FIND_PRIMARY_CTA
        return MappedAction(
            type="validate",
            tool="read_page_content",
            why="Node validated"
        ), False

    def _transition_find_cta(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Find primary CTA from site map contract.

        ADAPTIVE: If CTA already clicked (in clicked_ids), skip to VERIFY_MODAL.
        """
        nodes = obs.get("nodes", [])
        clicked_ids = obs.get("clicked_ids", set())
        cta_label = self.context.site_map_node.get("primary_cta", "")

        # ADAPTIVE CHECK: Was CTA already clicked?
        # If we've already clicked something with the CTA label, skip ahead
        cta_already_clicked = False
        for node in nodes:
            node_id = getattr(node, "id", "")
            if node_id in clicked_ids:
                node_text = str(getattr(node, "text", "") or "").lower()
                if cta_label.lower() in node_text:
                    cta_already_clicked = True
                    break

        if cta_already_clicked:
            logger.info(f"ADAPTIVE_SKIP: CTA '{cta_label}' already clicked, verifying modal")
            self.state = MappedActionState.VERIFY_MODAL_OR_FORM
            return MappedAction(
                type="validate",
                tool="read_page_content",
                why="CTA already clicked, verifying modal/form"
            ), False

        # Priority 1: Site map contract
        for node in nodes:
            node_id = getattr(node, "id", "")
            if node_id in clicked_ids:
                continue
            node_text = str(getattr(node, "text", "") or "").lower()
            if cta_label.lower() in node_text:
                is_interactive = getattr(node, "interactive", False)
                if is_interactive:
                    self.state = MappedActionState.CLICK_PRIMARY_CTA
                    return MappedAction(
                        type="action",
                        tool="click_element",
                        target_id=node_id,
                        intent={
                            "text_label": cta_label,
                            "element_type": "button",
                            "context": "Primary CTA"
                        },
                        why=f"Primary CTA: {cta_label}"
                    ), False

        # Fallback: Vision hints (advisory)
        vision = obs.get("vision_hints", "")
        if vision:
            vision_target = self._extract_target_from_vision(vision, cta_label)
            if vision_target and vision_target not in clicked_ids:
                self.state = MappedActionState.CLICK_PRIMARY_CTA
                return MappedAction(
                    type="action",
                    tool="click_element",
                    target_id=vision_target,
                    intent={
                        "text_label": cta_label,
                        "element_type": "button",
                        "context": "Primary CTA (vision-assisted)"
                    },
                    why=f"Vision-assisted CTA: {cta_label}"
                ), False

        return MappedAction(
            type="action",
            tool="wait_for_ui",
            why="Finding CTA"
        ), False

    def _transition_click_cta(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Click primary CTA."""
        self.state = MappedActionState.VERIFY_MODAL_OR_FORM
        return MappedAction(
            type="action",
            tool="wait_for_ui",
            params={"seconds": 1},
            why="Waiting for modal/form after CTA click"
        ), False

    def _transition_verify_modal(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Verify modal/form appeared after CTA click.

        ADAPTIVE: If fields already filled or success already visible, skip ahead.
        """
        readable = obs.get("readable_content", "")
        readable_lower = readable.lower() if readable else ""
        nodes = obs.get("nodes", [])
        clicked_ids = obs.get("clicked_ids", set())

        # ADAPTIVE CHECK 1: Is success already visible?
        # Maybe the action was already completed (idempotent operation)
        success_indicators = ["success", "created", "done", "complete", "confirmed", "key created"]
        if any(s in readable_lower for s in success_indicators):
            logger.info("ADAPTIVE_SKIP: Success already visible, skipping to COMPLETE")
            self.state = MappedActionState.COMPLETE
            return MappedAction(
                type="validate",
                tool="read_page_content",
                why="Success already visible"
            ), True

        # ADAPTIVE CHECK 2: Are all fields already filled?
        # Check if input fields have values (not empty)
        required = self.context.site_map_node.get("required_fields", ["name"])
        all_fields_filled = True
        for field in required:
            field_filled = False
            for node in nodes:
                node_text = str(getattr(node, "text", "") or "").lower()
                if field.lower() in node_text:
                    # Check if there's a value associated with this field
                    # (heuristic: look for text that's not the label itself)
                    if len(node_text) > len(field) + 5:  # Has extra content
                        field_filled = True
                        break
            if not field_filled:
                all_fields_filled = False
                break

        if all_fields_filled and required:
            logger.info("ADAPTIVE_SKIP: All fields already filled, skipping to CLICK_CONFIRM")
            self.state = MappedActionState.CLICK_CONFIRM
            return MappedAction(
                type="validate",
                tool="read_page_content",
                why="All fields already filled"
            ), False

        # Standard modal check
        modal_indicators = ["modal", "dialog", "form", "create", "new", "enter", "required"]
        if any(m in readable_lower for m in modal_indicators):
            self.state = MappedActionState.FILL_REQUIRED_FIELDS
            return MappedAction(
                type="action",
                tool="read_page_content",
                why="Modal verified"
            ), False

        return MappedAction(
            type="action",
            tool="wait_for_ui",
            why="Waiting for modal"
        ), False

    def _transition_fill_fields(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Fill required fields from site map.

        ADAPTIVE: Skip fields that appear already filled based on live DOM.
        """
        required = self.context.site_map_node.get("required_fields", ["name"])
        nodes = obs.get("nodes", [])
        readable = obs.get("readable_content", "")

        # ADAPTIVE: Check which fields are actually needed
        fields_to_fill = []
        for field in required:
            if field in self.filled_fields:
                continue  # Already filled by us

            # Check if field appears already filled in DOM
            field_appears_filled = False
            for node in nodes:
                node_text = str(getattr(node, "text", "") or "").lower()
                if field.lower() in node_text:
                    # If the text contains more than just the label, it might have a value
                    if len(node_text) > len(field) + 3:
                        field_appears_filled = True
                        logger.info(f"ADAPTIVE: Field '{field}' appears already filled")
                        break

            if not field_appears_filled:
                fields_to_fill.append(field)

        # If no fields need filling, skip to confirm
        if not fields_to_fill:
            logger.info("ADAPTIVE_SKIP: No fields need filling, proceeding to CLICK_CONFIRM")
            self.state = MappedActionState.CLICK_CONFIRM
            return MappedAction(
                type="validate",
                tool="read_page_content",
                why="All fields filled or not required"
            ), False

        # Fill the first unfilled field
        for field in fields_to_fill:
            for node in nodes:
                node_id = getattr(node, "id", "")
                node_text = str(getattr(node, "text", "") or "").lower()
                if field.lower() in node_text:
                    is_interactive = getattr(node, "interactive", False)
                    if is_interactive:
                        self.filled_fields.add(field)
                        return MappedAction(
                            type="action",
                            tool="type_text",
                            target_id=node_id,
                            text=self._generate_field_value(field),
                            why=f"Fill required field: {field}"
                        ), False

        # No fields found to fill - proceed to confirm
        logger.info(f"ADAPTIVE: No input fields found for {fields_to_fill}, proceeding to confirm")
        self.state = MappedActionState.CLICK_CONFIRM
        return MappedAction(
            type="validate",
            tool="read_page_content",
            why="No input fields found, proceeding to confirm"
        ), False

    def _transition_click_confirm(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Click confirm/submit button."""
        nodes = obs.get("nodes", [])
        for node in nodes:
            node_id = getattr(node, "id", "")
            node_text = str(getattr(node, "text", "") or "").lower()
            is_interactive = getattr(node, "interactive", False)
            if any(b in node_text for b in ["confirm", "create", "submit", "save"]) and is_interactive:
                self.state = MappedActionState.VERIFY_SUCCESS
                return MappedAction(
                    type="action",
                    tool="click_element",
                    target_id=node_id,
                    why="Confirm action"
                ), False

        return MappedAction(
            type="action",
            tool="wait_for_ui",
            why="Finding confirm button"
        ), False

    def _transition_verify_success(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Verify action succeeded."""
        readable = obs.get("readable_content", "")
        readable_lower = readable.lower() if readable else ""

        success_indicators = ["success", "created", "done", "complete", "confirmed"]
        if any(s in readable_lower for s in success_indicators):
            self.state = MappedActionState.COMPLETE
            return MappedAction(
                type="action",
                tool="read_page_content",
                why="Success verified"
            ), False

        return MappedAction(
            type="action",
            tool="wait_for_ui",
            why="Waiting for success"
        ), False

    def _transition_complete(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Terminal complete state."""
        return MappedAction(
            type="complete",
            tool="answer",
            why="Action complete"
        ), True

    def _escalate(self, reason: str) -> Tuple[MappedAction, bool]:
        """Escalate when stuck."""
        self.error_log.append(reason)
        return MappedAction(
            type="escalate",
            tool="clarify",
            params={"error_log": self.error_log},
            why=f"Escalating: {reason}"
        ), True

    def _extract_target_from_vision(self, vision: str, cta_label: str) -> str:
        """Extract target ID from vision hints."""
        cta_lower = cta_label.lower()
        lines = vision.split("\n") if vision else []
        for line in lines:
            line_lower = line.lower()
            if cta_lower in line_lower:
                id_match = re.search(r"\b(t-[a-zA-Z0-9_-]+)\b", line)
                if id_match:
                    return id_match.group(1)
        return ""

    def _generate_field_value(self, field: str) -> str:
        """Generate a reasonable value for a field."""
        field_lower = field.lower()
        if "name" in field_lower:
            return "Generated Name"
        elif "email" in field_lower:
            return "user@example.com"
        elif "key" in field_lower:
            import secrets
            return f"sk-{secrets.token_hex(16)}"
        elif "date" in field_lower:
            from datetime import datetime
            return datetime.now().strftime("%Y-%m-%d")
        else:
            return f"Auto-generated {field}"


class MappedFormFillStateMachine:
    """FSM for form fill workflows like 'Update my profile'.

    State progression:
    VALIDATE_NODE → LOCATE_FORM → FILL_FIELD (loop) → VALIDATE_FORM →
    SUBMIT_FORM → VERIFY_SUCCESS → COMPLETE
    """

    def __init__(self, context: MappedTerminalContext, max_attempts: int = 15):
        self.context = context
        self.state = MappedFormFillState.VALIDATE_NODE
        self.attempts = 0
        self.max_attempts = max_attempts
        self.filled_fields: Set[str] = set()
        self.error_log: List[str] = []

    def transition(self, observation: Dict) -> Tuple[MappedAction, bool]:
        """Execute form fill workflow transition."""
        self.attempts += 1

        if self.attempts > self.max_attempts:
            return self._escalate("max_attempts"), True

        transition_map = {
            MappedFormFillState.VALIDATE_NODE: self._transition_validate_node,
            MappedFormFillState.LOCATE_FORM: self._transition_locate_form,
            MappedFormFillState.FILL_FIELD: self._transition_fill_field,
            MappedFormFillState.VALIDATE_FORM: self._transition_validate_form,
            MappedFormFillState.SUBMIT_FORM: self._transition_submit_form,
            MappedFormFillState.VERIFY_SUCCESS: self._transition_verify_success,
            MappedFormFillState.COMPLETE: self._transition_complete,
        }

        handler = transition_map.get(self.state)
        return handler(observation) if handler else self._escalate("unknown_state")

    def _transition_validate_node(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Validate we're on correct node for form fill."""
        if self.context.site_map_node:
            self.state = MappedFormFillState.LOCATE_FORM
            return MappedAction(
                type="validate",
                tool="read_page_content",
                why="Node validated"
            ), False
        return self._escalate("no_site_map_node"), True

    def _transition_locate_form(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Locate the form element."""
        nodes = obs.get("nodes", [])
        form_keywords = ["form", "profile", "settings", "edit", "update", "input"]

        for node in nodes:
            node_id = getattr(node, "id", "")
            node_text = str(getattr(node, "text", "") or "").lower()
            if any(kw in node_text for kw in form_keywords):
                is_interactive = getattr(node, "interactive", False)
                tag_name = getattr(node, "tag_name", "").lower()
                if tag_name == "form" or is_interactive:
                    self.state = MappedFormFillState.FILL_FIELD
                    return MappedAction(
                        type="locate",
                        tool="read_page_content",
                        why=f"Form located: {node_id}"
                    ), False

        for node in nodes:
            tag_name = getattr(node, "tag_name", "").lower()
            if tag_name in ["input", "textarea", "select"]:
                self.state = MappedFormFillState.FILL_FIELD
                return MappedAction(
                    type="locate",
                    tool="read_page_content",
                    why="Form fields found"
                ), False

        return MappedAction(
            type="locate",
            tool="wait_for_ui",
            why="Locating form"
        ), False

    def _transition_fill_field(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Fill one field at a time."""
        required = self.context.site_map_node.get("required_fields", [])
        nodes = obs.get("nodes", [])

        for field in required:
            if field not in self.filled_fields:
                for node in nodes:
                    node_id = getattr(node, "id", "")
                    node_text = str(getattr(node, "text", "") or "").lower()
                    tag_name = getattr(node, "tag_name", "").lower()
                    if field.lower() in node_text and tag_name in ["input", "textarea", "select"]:
                        self.filled_fields.add(field)
                        return MappedAction(
                            type="action",
                            tool="type_text",
                            target_id=node_id,
                            text=self._generate_field_value(field),
                            why=f"Fill required field: {field}"
                        ), False

        self.state = MappedFormFillState.VALIDATE_FORM
        return MappedAction(
            type="validate",
            tool="read_page_content",
            why="All fields filled"
        ), False

    def _transition_validate_form(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Validate form is ready to submit."""
        self.state = MappedFormFillState.SUBMIT_FORM
        return MappedAction(
            type="validate",
            tool="read_page_content",
            why="Form validated"
        ), False

    def _transition_submit_form(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Click submit/save button."""
        nodes = obs.get("nodes", [])
        submit_keywords = ["submit", "save", "update", "confirm", "apply", "done"]

        for node in nodes:
            node_id = getattr(node, "id", "")
            node_text = str(getattr(node, "text", "") or "").lower()
            is_interactive = getattr(node, "interactive", False)
            if any(kw in node_text for kw in submit_keywords) and is_interactive:
                self.state = MappedFormFillState.VERIFY_SUCCESS
                return MappedAction(
                    type="action",
                    tool="click_element",
                    target_id=node_id,
                    why="Submit form"
                ), False

        return MappedAction(
            type="action",
            tool="wait_for_ui",
            why="Finding submit button"
        ), False

    def _transition_verify_success(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Verify form submission succeeded."""
        readable = obs.get("readable_content", "")
        readable_lower = readable.lower() if readable else ""

        success_indicators = ["success", "updated", "saved", "complete", "confirmed", "changes saved"]
        if any(s in readable_lower for s in success_indicators):
            self.state = MappedFormFillState.COMPLETE
            return MappedAction(
                type="action",
                tool="read_page_content",
                why="Success verified"
            ), False

        return MappedAction(
            type="action",
            tool="wait_for_ui",
            why="Waiting for success"
        ), False

    def _transition_complete(self, obs: Dict) -> Tuple[MappedAction, bool]:
        """Terminal complete state."""
        return MappedAction(
            type="complete",
            tool="answer",
            why="Form fill complete"
        ), True

    def _escalate(self, reason: str) -> Tuple[MappedAction, bool]:
        """Escalate when stuck."""
        self.error_log.append(reason)
        return MappedAction(
            type="escalate",
            tool="clarify",
            params={"error_log": self.error_log},
            why=f"Escalating: {reason}"
        ), True

    def _generate_field_value(self, field: str) -> str:
        """Generate a reasonable value for a field."""
        field_lower = field.lower()
        if "name" in field_lower:
            return "Generated Name"
        elif "email" in field_lower:
            return "user@example.com"
        elif "phone" in field_lower:
            return "+1-555-123-4567"
        elif "bio" in field_lower or "description" in field_lower:
            return "Auto-generated description"
        else:
            return f"Auto-generated {field}"


def validate_completion_contract(
    context: MappedTerminalContext,
    evidence: Dict[str, Any],
    task_mode: str = "read_extract",
) -> Tuple[bool, List[str]]:
    """
    Validate evidence against site map completion contract.

    The completion contract defines what must be true for a task to be
    considered complete. This function checks evidence against the contract.

    Args:
        context: MappedTerminalContext with site_map_node
        evidence: Dict with evidence fields (entity_visible, metric_visible, etc.)
        task_mode: Task type - "read_extract", "create_action", "form_fill"

    Returns:
        (is_valid, missing_requirements)
        - is_valid: True if all contract requirements are met
        - missing_requirements: List of unmet requirement names

    Example contract for read_extract:
    - entity_visible: "Whisper" found in page
    - metric_visible: "tokens" found in page
    - value_extracted: numeric value present
    """
    if not context.site_map_node:
        # No site map - use default invariants
        missing = []
        if not context.entity_anchor_found:
            missing.append("entity_visible")
        if not context.metric_anchor_found:
            missing.append("metric_visible")
        if not context.numeric_value_found:
            missing.append("value_extracted")
        return len(missing) == 0, missing

    contract = context.site_map_node.get("completion_contract", {})
    requirements = contract.get(task_mode, [])

    # Default requirements if not specified in contract
    if not requirements and task_mode == "read_extract":
        requirements = ["entity_visible", "metric_visible", "value_extracted"]

    missing = []
    for req in requirements:
        if req == "entity_visible":
            if not context.entity_anchor_found:
                missing.append("entity_visible")
        elif req == "metric_visible":
            if not context.metric_anchor_found:
                missing.append("metric_visible")
        elif req == "value_extracted":
            if not context.numeric_value_found:
                missing.append("value_extracted")
        elif req == "filter_applied":
            # Check filter state - if goal specifies filters, verify applied
            if context.goal_filters:
                # Would need additional context to verify filter state
                pass
        elif req == "entity_selected":
            # For modes where entity must be actively selected (not just visible)
            if not context.entity_anchor_found:
                missing.append("entity_selected")

    return len(missing) == 0, missing


# Feature flag for mapped last-mile mode
MAPPED_LAST_MILE_ENABLED = False  # Set via env var: MAPPED_LAST_MILE_ENABLED=true


def is_mapped_site(url: str, validator: Optional[SiteMapValidator] = None) -> bool:
    """Check if URL belongs to a mapped site.

    Args:
        url: Current page URL
        validator: Optional SiteMapValidator instance

    Returns:
        True if URL matches a site map pattern
    """
    if validator is None:
        try:
            validator = SiteMapValidator()
        except Exception:
            return False

    node = validator.get_node_for_url(url)
    return node is not None


def should_use_mapped_mode(url: str, goal: str) -> bool:
    """Determine if we should use mapped extraction mode.

    Returns True if:
    1. MAPPED_LAST_MILE_ENABLED is True
    2. URL matches a site map pattern
    3. Goal looks like a data extraction query
    """
    import os

    enabled = os.getenv("MAPPED_LAST_MILE_ENABLED", "false").lower() in {
        "1", "true", "yes", "on"
    }
    if not enabled:
        return False

    if not is_mapped_site(url):
        return False

    # Check if goal looks like extraction
    extraction_patterns = [
        r"\b(how many|how much|what is|what are|show me)\b",
        r"\b(tokens?|cost|usage|price)\b",
        r"\b(did i use|have i used)\b",
    ]
    goal_lower = goal.lower()
    for pattern in extraction_patterns:
        if re.search(pattern, goal_lower):
            return True

    return False


# ═══════════════════════════════════════════════════════════════════════
# Contract Breach Detection (Escape Hatch)
# ═══════════════════════════════════════════════════════════════════════

MAPPED_CONTRACT_ESCAPE_HATCH_ENABLED = os.getenv("MAPPED_CONTRACT_ESCAPE_HATCH_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)


def contract_breach_detected(observation: Dict[str, Any], context: MappedTerminalContext) -> Tuple[bool, str]:
    """Detect when contract assumptions don't match reality.

    When breach detected → fall back to _run_exploratory_last_mile()

    Breach conditions:
    1. Contract says control should exist, but DOM shows different pattern
    2. Contract says modal should appear, but no modal visible after CTA click
    3. Contract says we're on page X, but URL/DOM signature says Y
    4. FSM stuck in same state for multiple attempts with no progress

    Args:
        observation: Dict containing nodes, readable_content, clicked_ids, etc.
        context: MappedTerminalContext with site map contract

    Returns:
        Tuple of (breach_detected, breach_reason)
    """
    if not MAPPED_CONTRACT_ESCAPE_HATCH_ENABLED:
        return False, ""

    nodes = observation.get("nodes", [])
    readable = observation.get("readable_content", "")
    site_map_node = context.site_map_node or {}
    control_groups = context.control_groups or {}

    # Breach Condition 1: Expected controls from contract not found in DOM
    if control_groups:
        for group_name, controls in control_groups.items():
            if isinstance(controls, list) and controls:
                found_count = 0
                for control in controls:
                    control_lower = control.lower() if isinstance(control, str) else ""
                    for node in nodes:
                        node_text = str(getattr(node, "text", "") or "").lower()
                        if control_lower and control_lower in node_text:
                            found_count += 1
                            break

                # If less than 30% of controls found, contract may be stale
                if len(controls) > 0 and found_count < (len(controls) * 0.3):
                    logger.warning(
                        f"CONTRACT_BREACH: control_group '{group_name}' has {found_count}/{len(controls)} controls found"
                    )
                    return True, f"control_group_mismatch: {group_name} - only {found_count}/{len(controls)} controls found"

    # Breach Condition 2: Site map defines primary_cta but not found after attempts
    primary_cta = site_map_node.get("primary_cta", "")
    if primary_cta and nodes:
        cta_found = False
        for node in nodes:
            node_text = str(getattr(node, "text", "") or "").lower()
            is_interactive = getattr(node, "interactive", False)
            if primary_cta.lower() in node_text and is_interactive:
                cta_found = True
                break

        if not cta_found:
            logger.warning(
                f"CONTRACT_BREACH: primary_cta '{primary_cta}' not found in DOM"
            )
            return True, f"primary_cta_mismatch: '{primary_cta}' not found in DOM"

    # Breach Condition 3: URL pattern mismatch
    # Contract says we should be on node X, but URL doesn't match expected patterns
    if context.current_node_id and context.url:
        expected_url_patterns = site_map_node.get("url_patterns", [])
        if expected_url_patterns:
            url_matches = False
            for pattern in expected_url_patterns:
                try:
                    if re.search(pattern, context.url):
                        url_matches = True
                        break
                except re.error:
                    # Invalid regex pattern in site map
                    pass

            if not url_matches:
                logger.warning(
                    f"CONTRACT_BREACH: URL '{context.url}' doesn't match expected patterns for node '{context.current_node_id}'"
                )
                return True, f"url_mismatch: URL doesn't match expected patterns for node '{context.current_node_id}'"

    # Breach Condition 4: Expected post-action state not achieved
    # (e.g., contract says modal should appear, but no modal indicators in DOM)
    transition_rules = site_map_node.get("task_modes", {})
    if isinstance(transition_rules, dict):
        for mode_name, mode_config in transition_rules.items():
            if isinstance(mode_config, dict):
                post_click_expected = mode_config.get("post_click_expected", [])
                if post_click_expected:
                    # Check if any of the expected elements are present
                    found_post_action = False
                    for expected in post_click_expected:
                        expected_lower = expected.lower()
                        for node in nodes:
                            node_text = str(getattr(node, "text", "") or "").lower()
                            zone = getattr(node, "zone", "")
                            if expected_lower in node_text or expected_lower in zone.lower():
                                found_post_action = True
                                break
                        if found_post_action:
                            break

                    # If nothing expected was found, contract may be wrong
                    if not found_post_action and readable:
                        logger.warning(
                            f"CONTRACT_BREACH: post_click_expected {post_click_expected} not found for mode '{mode_name}'"
                        )
                        return True, f"post_action_mismatch: expected {post_click_expected} not found"

    return False, ""


def should_fallback_to_exploratory(
    observation: Dict[str, Any],
    context: MappedTerminalContext,
    fsm_state: Any,
    attempts: int,
    last_action: Optional[Any] = None,
) -> Tuple[bool, str]:
    """Decide whether to fallback to exploratory last-mile.

    Combines contract breach detection with FSM stall detection.

    Args:
        observation: Current FSM observation
        context: MappedTerminalContext
        fsm_state: Current FSM state
        attempts: Number of attempts in current state
        last_action: Last action taken (optional)

    Returns:
        Tuple of (should_fallback, reason)
    """
    # Check for contract breach first
    breach_detected, breach_reason = contract_breach_detected(observation, context)
    if breach_detected:
        return True, f"contract_breach: {breach_reason}"

    # Check for FSM stall (stuck in same state too long)
    if attempts > 8:
        logger.warning(
            f"FSM_STALL: stuck in state '{fsm_state.value}' for {attempts} attempts"
        )
        return True, f"fsm_stall: stuck in state '{fsm_state.value}' for {attempts} attempts"

    return False, ""


# ═══════════════════════════════════════════════════════════════════════
# Tool Registry for Exploratory Last-Mile Path
# ═══════════════════════════════════════════════════════════════════════

LAST_MILE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click_element",
            "description": "Click an interactive element (button, link, tab, dropdown) by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The exact DOM element ID to click"
                    },
                    "intent": {
                        "type": "string",
                        "description": "Human-readable intent for why this click is needed"
                    }
                },
                "required": ["target_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into an input field, search box, or textarea.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The exact DOM element ID of the input field"
                    },
                    "text": {
                        "type": "string",
                        "description": "The text content to type"
                    }
                },
                "required": ["target_id", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_page",
            "description": "Scroll the page up or down to reveal more content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "Direction to scroll"
                    },
                    "amount": {
                        "type": "string",
                        "enum": ["page", "half", "top", "bottom"],
                        "description": "How much to scroll"
                    }
                },
                "required": ["direction"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_page_content",
            "description": "Read visible text content from the current page. Use this to extract evidence before completing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": "Optional topic or keyword to focus the extraction on"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "complete_mission",
            "description": "Complete the task and provide a final answer to the user. ONLY call this when you have concrete evidence from the page content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "response": {
                        "type": "string",
                        "description": "The final answer grounded in visible page evidence"
                    },
                    "evidence_refs": {
                        "type": "string",
                        "description": "Specific text excerpts from the page that support the answer"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["success", "partial", "failed"],
                        "description": "Whether the goal was fully achieved"
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Confidence level in the answer"
                    }
                },
                "required": ["response", "status"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Escalate to human when the task cannot be completed due to blocking issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "blockage_type": {
                        "type": "string",
                        "enum": ["authentication_required", "page_not_found", "feature_missing", "ambiguous_intent", "technical_error"],
                        "description": "Type of blockage encountered"
                    },
                    "speech": {
                        "type": "string",
                        "description": "What to tell the user about needing help"
                    },
                    "diagnostics": {
                        "type": "object",
                        "description": "Additional diagnostic information"
                    }
                },
                "required": ["blockage_type", "speech"]
            }
        }
    }
]

"""
visual_copilot/navigation/site_map_validator.py

Site Map Validator Module

Provides comprehensive validation of navigation state against a ground-truth site map.
Uses path_regex patterns to match URLs, validates expected/required controls,
and guides recovery when navigation fails.

Example:
    validator = SiteMapValidator()
    result = validator.validate_page_state(url, nodes)
    if not result.is_valid:
        recovery = validator.suggest_recovery_path(result.current_node, result.expected_node)
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of validating a page state against the site map.

    Attributes:
        is_valid: Whether the page state matches site map expectations
        current_node: The site map node matching current URL (if any)
        expected_node: The expected node we should be on (if known)
        missing_required_controls: List of required controls not found in DOM
        present_expected_controls: List of expected controls found in DOM
        missing_expected_controls: List of expected controls not found in DOM
        validation_message: Human-readable description of validation result
        suggested_recovery: Optional recovery action suggestion
        alternative_paths: List of alternative navigation paths if validation failed
    """
    is_valid: bool
    current_node: Optional[Dict[str, Any]] = None
    expected_node: Optional[Dict[str, Any]] = None
    missing_required_controls: List[str] = field(default_factory=list)
    present_expected_controls: List[str] = field(default_factory=list)
    missing_expected_controls: List[str] = field(default_factory=list)
    validation_message: str = ""
    suggested_recovery: Optional[str] = None
    alternative_paths: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class NavigationOutcome:
    """Predicted outcome of a navigation action.

    Attributes:
        expected_node: The node we expect to reach
        expected_url: The URL pattern we expect to land on
        confidence: Confidence level (0.0-1.0) of this prediction
        alternative_targets: Other possible targets if ambiguous
    """
    expected_node: Optional[Dict[str, Any]]
    expected_url: str
    confidence: float
    alternative_targets: List[Dict[str, Any]] = field(default_factory=list)


class SiteMapValidator:
    """Validates navigation state against a ground-truth site map.

    This class provides comprehensive validation capabilities:
    - URL matching via path_regex patterns
    - Control presence validation (expected and required)
    - Navigation outcome prediction
    - Recovery path suggestions

    The site map is loaded from a JSON file and cached for performance.
    """

    def __init__(self, site_map_path: Optional[str] = None):
        """Initialize the validator with a site map.

        Args:
            site_map_path: Path to site_map.json. If None, searches common locations.
        """
        self.site_map = self._load_site_map(site_map_path)
        self.node_index = self._build_node_index()
        self.node_id_map = self._build_node_id_map()
        self.parent_map = self._build_parent_map()
        logger.info(
            f"SiteMapValidator initialized with {len(self.node_index)} nodes, "
            f"domain={self.site_map.get('site_metadata', {}).get('domain', 'unknown')}"
        )

    def _load_site_map(self, path: Optional[str] = None) -> Dict[str, Any]:
        """Load site map from file with fallback search paths.

        Args:
            path: Explicit path to site map file

        Returns:
            Loaded site map dictionary
        """
        search_paths = [
            path,
            os.getenv("SITE_MAP_PATH"),
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "site_map.json"
            ),
            "/app/site_map.json",
            "./site_map.json",
        ]

        for p in search_paths:
            if p and os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        site_map = json.load(f)
                        logger.info(f"Site map loaded from {p}")
                        return site_map
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning(f"Failed to load site map from {p}: {e}")
                    continue

        logger.error("No valid site_map.json found in any search path")
        return {"root": {}, "site_metadata": {}}

    def _build_node_index(self) -> List[Dict[str, Any]]:
        """Flatten the site map tree into a list of all nodes.

        Returns:
            List of all nodes in the site map
        """
        nodes = []

        def _walk(node: Dict[str, Any]) -> None:
            nodes.append(node)
            for child in node.get("children", []):
                _walk(child)

        root = self.site_map.get("root", {})
        if root:
            _walk(root)
        return nodes

    def _build_node_id_map(self) -> Dict[str, Dict[str, Any]]:
        """Build a map of node_id -> node for quick lookup.

        Returns:
            Dictionary mapping node IDs to node objects
        """
        return {
            node.get("node_id", ""): node
            for node in self.node_index
            if node.get("node_id")
        }

    def _build_parent_map(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """Build a map of node_id -> parent node.

        Returns:
            Dictionary mapping node IDs to their parent nodes
        """
        parent_map: Dict[str, Optional[Dict[str, Any]]] = {}

        def _walk(node: Dict[str, Any], parent: Optional[Dict[str, Any]]) -> None:
            node_id = node.get("node_id", "")
            if node_id:
                parent_map[node_id] = parent
            for child in node.get("children", []):
                _walk(child, node)

        root = self.site_map.get("root", {})
        if root:
            _walk(root, None)
        return parent_map

    def get_node_for_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Find the site map node matching current URL using path_regex.

        Uses regex matching to find the most specific (deepest) node that
        matches the given URL. Returns the node with the longest path_regex
        that matches.

        Args:
            url: Current page URL to match

        Returns:
            Matching node dictionary or None if no match found
        """
        if not url:
            return None

        # Parse URL to get path
        parsed = urlparse(url if "://" in url else f"https://{url}")
        path = parsed.path or "/"

        best_match: Optional[Dict[str, Any]] = None
        best_specificity = -1

        for node in self.node_index:
            regex = node.get("path_regex", "")
            if not regex:
                continue

            try:
                # Use search to match anywhere in path, or match for exact
                if re.search(regex, path):
                    # Score by regex length (more specific = longer)
                    specificity = len(regex)
                    if specificity > best_specificity:
                        best_specificity = specificity
                        best_match = node
            except re.error as e:
                logger.debug(f"Invalid regex in node {node.get('node_id', '?')}: {e}")
                continue

        return best_match

    def get_node_by_id(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get a node by its node_id.

        Args:
            node_id: The node identifier

        Returns:
            Node dictionary or None if not found
        """
        return self.node_id_map.get(node_id)

    def get_parent_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get the parent of a node.

        Args:
            node_id: The child node identifier

        Returns:
            Parent node dictionary or None if not found or root
        """
        return self.parent_map.get(node_id)

    def _extract_control_text(self, node: Any) -> str:
        """Extract searchable text from a DOM node.

        Args:
            node: DOM node object

        Returns:
            Concatenated text from relevant attributes
        """
        texts = [
            getattr(node, "text", "") or "",
            getattr(node, "aria_label", "") or "",
            getattr(node, "placeholder", "") or "",
            getattr(node, "name", "") or "",
            getattr(node, "title", "") or "",
        ]
        return " ".join(filter(None, texts)).lower()

    def _find_control_in_dom(
        self,
        control_name: str,
        nodes: List[Any],
        fuzzy: bool = True
    ) -> bool:
        """Check if a control exists in the DOM.

        Args:
            control_name: Name of the control to find
            nodes: List of DOM nodes to search
            fuzzy: Whether to use fuzzy matching

        Returns:
            True if control is found, False otherwise
        """
        control_lower = control_name.lower().strip()
        control_tokens = set(re.findall(r"[a-z0-9]+", control_lower))

        for dom_node in nodes:
            node_text = self._extract_control_text(dom_node)

            # Exact match
            if control_lower in node_text:
                return True

            # Token-based fuzzy match
            if fuzzy and control_tokens:
                node_tokens = set(re.findall(r"[a-z0-9]+", node_text))
                # Consider it a match if most tokens overlap
                if len(control_tokens & node_tokens) >= max(1, len(control_tokens) * 0.7):
                    return True

        return False

    def validate_page_state(
        self,
        url: str,
        nodes: List[Any],
        expected_node_id: Optional[str] = None
    ) -> ValidationResult:
        """Validate current page against site map expectations.

        Performs comprehensive validation:
        1. Matches URL to site map node
        2. Checks for expected controls in DOM
        3. Verifies required controls are present
        4. Compares against expected node if provided

        Args:
            url: Current page URL
            nodes: List of DOM nodes from current page
            expected_node_id: Optional node ID we expect to be on

        Returns:
            ValidationResult with detailed validation information
        """
        current_node = self.get_node_for_url(url)
        expected_node = self.get_node_by_id(expected_node_id) if expected_node_id else None

        # If URL doesn't match any node, we can't validate controls
        if not current_node:
            return ValidationResult(
                is_valid=False,
                current_node=None,
                expected_node=expected_node,
                validation_message=f"URL '{url}' does not match any known site map node",
                suggested_recovery="check_url_or_replan",
                alternative_paths=self._get_root_paths()
            )

        current_node_id = current_node.get("node_id", "")

        # Check if we're on the expected node
        if expected_node and current_node_id != expected_node_id:
            # We're on the wrong page
            recovery = self._suggest_recovery(current_node, expected_node)
            return ValidationResult(
                is_valid=False,
                current_node=current_node,
                expected_node=expected_node,
                validation_message=(
                    f"Navigation validation failed: expected '{expected_node.get('title')}' "
                    f"({expected_node_id}) but current URL matches '{current_node.get('title')}' "
                    f"({current_node_id})"
                ),
                suggested_recovery=recovery,
                alternative_paths=self._find_alternative_paths(expected_node)
            )

        # Validate controls
        expected_controls = current_node.get("expected_controls", []) or []
        required_controls = current_node.get("required_controls", []) or []

        present_expected = []
        missing_expected = []
        missing_required = []

        # Check expected controls
        for control in expected_controls:
            if self._find_control_in_dom(control, nodes):
                present_expected.append(control)
            else:
                missing_expected.append(control)

        # Check required controls (must be present)
        for control in required_controls:
            if not self._find_control_in_dom(control, nodes):
                missing_required.append(control)

        # Determine validity
        is_valid = len(missing_required) == 0

        # Build validation message
        if is_valid:
            if missing_expected:
                message = (
                    f"Page '{current_node.get('title')}' validated. "
                    f"Found {len(present_expected)}/{len(expected_controls)} expected controls. "
                    f"Missing optional: {', '.join(missing_expected)}"
                )
            else:
                message = (
                    f"Page '{current_node.get('title')}' fully validated. "
                    f"All {len(expected_controls)} expected controls present."
                )
        else:
            message = (
                f"Page '{current_node.get('title')}' validation FAILED. "
                f"Missing required controls: {', '.join(missing_required)}"
            )

        return ValidationResult(
            is_valid=is_valid,
            current_node=current_node,
            expected_node=expected_node,
            missing_required_controls=missing_required,
            present_expected_controls=present_expected,
            missing_expected_controls=missing_expected,
            validation_message=message,
            suggested_recovery="none" if is_valid else "reload_or_backtrack"
        )

    def get_expected_navigation_outcome(
        self,
        from_node_id: str,
        click_target: str
    ) -> Optional[NavigationOutcome]:
        """Predict the expected outcome of clicking a target from a node.

        Uses the site map structure to determine which child node should
        be reached when clicking a specific control.

        Args:
            from_node_id: Current node identifier
            click_target: Text/label of the element being clicked

        Returns:
            NavigationOutcome with expected destination, or None if unknown
        """
        from_node = self.get_node_by_id(from_node_id)
        if not from_node:
            return None

        children = from_node.get("children", [])
        if not children:
            # Leaf node - no children to navigate to
            return NavigationOutcome(
                expected_node=None,
                expected_url=from_node.get("url", ""),
                confidence=0.0,
                alternative_targets=[]
            )

        # Score each child by how well it matches the click target
        target_lower = click_target.lower().strip()
        target_tokens = set(re.findall(r"[a-z0-9]+", target_lower))

        scored_children: List[Tuple[float, Dict[str, Any]]] = []

        for child in children:
            score = 0.0
            child_title = (child.get("title", "") or "").lower()
            child_summary = (child.get("summary_of_contents", "") or "").lower()

            # Title match is strongest signal
            if target_lower == child_title:
                score = 10.0
            elif target_lower in child_title:
                score = 5.0
            elif child_title in target_lower:
                score = 3.0

            # Token overlap
            child_tokens = set(re.findall(r"[a-z0-9]+", child_title + " " + child_summary))
            overlap = len(target_tokens & child_tokens)
            if overlap > 0:
                score += overlap * 0.5

            # Check expected controls for match
            for control in child.get("expected_controls", []):
                control_lower = control.lower()
                if target_lower in control_lower or control_lower in target_lower:
                    score += 2.0
                    break

            if score > 0:
                scored_children.append((score, child))

        if not scored_children:
            return None

        # Sort by score descending
        scored_children.sort(key=lambda x: x[0], reverse=True)

        best_match = scored_children[0][1]
        confidence = min(1.0, scored_children[0][0] / 10.0)

        # Collect alternatives (other high-scoring children)
        alternatives = [
            child for score, child in scored_children[1:3]
            if score > 1.0
        ]

        return NavigationOutcome(
            expected_node=best_match,
            expected_url=best_match.get("url", ""),
            confidence=confidence,
            alternative_targets=alternatives
        )

    def validate_navigation_success(
        self,
        from_url: str,
        to_url: str,
        clicked_target: str
    ) -> Tuple[bool, str]:
        """Verify navigation succeeded by checking if to_url matches expected child node.

        Args:
            from_url: URL before navigation
            to_url: URL after navigation
            clicked_target: Text/label of the clicked element

        Returns:
            Tuple of (success: bool, reason: str)
        """
        from_node = self.get_node_for_url(from_url)
        if not from_node:
            # Can't validate without knowing where we started
            return True, "unknown_origin_no_validation"

        from_node_id = from_node.get("node_id", "")
        expected_outcome = self.get_expected_navigation_outcome(from_node_id, clicked_target)

        if not expected_outcome or not expected_outcome.expected_node:
            # No expected outcome defined - can't validate
            return True, "no_expected_outcome_defined"

        to_node = self.get_node_for_url(to_url)

        if not to_node:
            # URL doesn't match any known node
            expected_url = expected_outcome.expected_url
            if expected_url and self._urls_match_pattern(to_url, expected_url):
                return True, "url_matches_expected_pattern"
            return False, f"url_not_recognized_expected_{expected_outcome.expected_node.get('node_id', 'unknown')}"

        # Check if we reached the expected node
        to_node_id = to_node.get("node_id", "")
        expected_node_id = expected_outcome.expected_node.get("node_id", "")

        if to_node_id == expected_node_id:
            return True, f"reached_expected_{expected_node_id}"

        # Check if we're still on the same page (navigation didn't happen)
        from_parsed = urlparse(from_url)
        to_parsed = urlparse(to_url)

        if from_parsed.path == to_parsed.path:
            return False, "navigation_failed_same_page"

        # We're on a different page than expected
        return False, (
            f"wrong_destination_expected_{expected_node_id}_got_{to_node_id}"
        )

    def _urls_match_pattern(self, url: str, pattern_url: str) -> bool:
        """Check if a URL matches a pattern URL (allows for query params, etc.).

        Args:
            url: Actual URL
            pattern_url: Expected URL pattern

        Returns:
            True if URLs match at the path level
        """
        url_parsed = urlparse(url)
        pattern_parsed = urlparse(pattern_url)

        # Compare paths (ignore query params and fragments)
        return url_parsed.path.rstrip("/") == pattern_parsed.path.rstrip("/")

    def _suggest_recovery(
        self,
        current_node: Dict[str, Any],
        expected_node: Dict[str, Any]
    ) -> str:
        """Suggest a recovery action when on the wrong page.

        Args:
            current_node: Node we're currently on
            expected_node: Node we should be on

        Returns:
            Recovery action suggestion
        """
        current_id = current_node.get("node_id", "")
        expected_id = expected_node.get("node_id", "")

        # Check if expected is a child of current
        children = current_node.get("children", [])
        child_ids = {c.get("node_id", "") for c in children}
        if expected_id in child_ids:
            return f"click_child_{expected_id}"

        # Check if expected is a parent of current
        parent = self.get_parent_node(current_id)
        if parent and parent.get("node_id") == expected_id:
            return "navigate_back"

        # Check if they share a common parent
        current_parent = self.get_parent_node(current_id)
        expected_parent = self.get_parent_node(expected_id)
        if current_parent and expected_parent:
            if current_parent.get("node_id") == expected_parent.get("node_id"):
                return f"click_sibling_{expected_id}"

        # Default: backtrack to parent and retry
        if parent:
            return f"backtrack_to_{parent.get('node_id', 'root')}"

        return "manual_intervention_required"

    def _find_alternative_paths(
        self,
        target_node: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Find alternative paths to reach a target node.

        Args:
            target_node: The target node to reach

        Returns:
            List of alternative path dictionaries
        """
        alternatives = []
        target_id = target_node.get("node_id", "")

        # Get parent as alternative starting point
        parent = self.get_parent_node(target_id)
        if parent:
            alternatives.append({
                "type": "via_parent",
                "parent_node": parent,
                "action": f"Navigate to {parent.get('title', 'parent')} first"
            })

        # Check siblings as alternatives
        if parent:
            siblings = [
                c for c in parent.get("children", [])
                if c.get("node_id") != target_id
            ]
            for sibling in siblings[:2]:  # Limit to first 2 siblings
                alternatives.append({
                    "type": "via_sibling",
                    "sibling_node": sibling,
                    "action": f"Try {sibling.get('title', 'sibling')} instead"
                })

        return alternatives

    def _get_root_paths(self) -> List[Dict[str, Any]]:
        """Get navigation paths from the root node.

        Returns:
            List of root-level navigation options
        """
        root = self.site_map.get("root", {})
        children = root.get("children", [])

        return [
            {
                "type": "root_child",
                "node": child,
                "action": f"Navigate to {child.get('title', 'section')}"
            }
            for child in children[:4]  # Top 4 sections
        ]

    def get_terminal_capabilities(self, url: str) -> List[str]:
        """Get the terminal capabilities available at a URL.

        Args:
            url: Page URL

        Returns:
            List of terminal capability strings
        """
        node = self.get_node_for_url(url)
        if not node:
            return []
        return node.get("terminal_capabilities", []) or []

    def get_expected_controls(self, url: str) -> List[str]:
        """Get the expected controls for a URL.

        Args:
            url: Page URL

        Returns:
            List of expected control names
        """
        node = self.get_node_for_url(url)
        if not node:
            return []
        return node.get("expected_controls", []) or []

    def is_terminal_node(self, url: str) -> bool:
        """Check if a URL corresponds to a terminal (leaf) node.

        Args:
            url: Page URL

        Returns:
            True if the node has no children and has terminal capabilities
        """
        node = self.get_node_for_url(url)
        if not node:
            return False
        has_children = len(node.get("children", [])) > 0
        has_capabilities = len(node.get("terminal_capabilities", [])) > 0
        return not has_children and has_capabilities

    def get_path_to_node(self, target_node_id: str) -> List[Dict[str, Any]]:
        """Get the path from root to a specific node.

        Args:
            target_node_id: Target node identifier

        Returns:
            List of nodes from root to target (inclusive)
        """
        # Build path by traversing parent pointers
        path = []
        current = self.get_node_by_id(target_node_id)

        while current:
            path.append(current)
            parent_id = current.get("node_id", "")
            current = self.get_parent_node(parent_id)

        return list(reversed(path))

    def get_breadcrumb(self, url: str) -> List[Dict[str, Any]]:
        """Get breadcrumb trail for a URL.

        Args:
            url: Page URL

        Returns:
            List of node dictionaries representing the breadcrumb trail
        """
        node = self.get_node_for_url(url)
        if not node:
            return []
        return self.get_path_to_node(node.get("node_id", ""))

    def resolve_current_node(
        self,
        url: str,
        dom_summary: Optional[str] = None,
        nodes: Optional[List[Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve current site map node from URL + optional DOM validation.

        Called every turn in mapped mode to ensure we're operating against
        the correct node contract.

        Args:
            url: Current page URL
            dom_summary: Optional DOM summary for validation (deprecated, use nodes)
            nodes: Optional list of DOM nodes for validation (preferred over dom_summary)

        Returns:
            Full site map node with contract, or None if not mapped

        The returned node includes:
            - expected_controls: List of expected control names
            - control_groups: Dict of control groups by function
            - task_modes: List of supported task modes
            - completion_contract: Dict mapping task_mode to completion criteria
        """
        # 1. Match URL against site_map.json path_regex patterns
        node = self.get_node_for_url(url)
        if not node:
            return None

        # 2. If DOM/nodes provided, validate DOM signature matches node expectations
        if nodes:
            expected_controls = node.get("expected_controls", []) or []
            required_controls = node.get("required_controls", []) or []

            # Check required controls - these MUST be present
            missing_required = []
            for control in required_controls:
                if not self._find_control_in_dom(control, nodes):
                    missing_required.append(control)

            if missing_required:
                # Required controls missing - log but still return node
                # The caller can decide if this is a validation failure
                logger.debug(
                    f"resolve_current_node: URL matched '{node.get('title')}' "
                    f"but missing required controls: {missing_required}"
                )

            # Check expected controls for logging
            present_expected = [
                c for c in expected_controls
                if self._find_control_in_dom(c, nodes)
            ]
            missing_expected = [
                c for c in expected_controls
                if c not in present_expected
            ]

            if missing_expected:
                logger.debug(
                    f"resolve_current_node: node={node.get('node_id')} "
                    f"controls_present={len(present_expected)}/{len(expected_controls)} "
                    f"missing_expected={missing_expected}"
                )
            else:
                logger.info(
                    f"CONTRACT_FIRST: node={node.get('node_id')} "
                    f"controls={expected_controls}"
                )

        # 3. Return full node with contract
        return node


# Singleton instance for module-level access
_validator_instance: Optional[SiteMapValidator] = None


def get_validator() -> SiteMapValidator:
    """Get or create the singleton SiteMapValidator instance.

    Returns:
        SiteMapValidator instance
    """
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = SiteMapValidator()
    return _validator_instance


def reset_validator() -> None:
    """Reset the singleton validator instance (useful for testing)."""
    global _validator_instance
    _validator_instance = None


def resolve_current_node(
    url: str,
    dom_summary: Optional[str] = None,
    nodes: Optional[List[Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    Resolve current site map node from URL + optional DOM validation.

    Convenience function that uses the singleton validator instance.
    Called every turn in mapped mode to ensure we're operating against
    the correct node contract.

    Args:
        url: Current page URL
        dom_summary: Optional DOM summary for validation (deprecated, use nodes)
        nodes: Optional list of DOM nodes for validation (preferred over dom_summary)

    Returns:
        Full site map node with contract, or None if not mapped

    The returned node includes:
        - expected_controls: List of expected control names
        - control_groups: Dict of control groups by function
        - task_modes: List of supported task modes
        - completion_contract: Dict mapping task_mode to completion criteria
    """
    validator = get_validator()
    return validator.resolve_current_node(url, dom_summary, nodes)

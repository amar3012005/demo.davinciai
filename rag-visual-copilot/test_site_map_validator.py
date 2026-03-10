#!/usr/bin/env python3
"""
Test script for SiteMapValidator implementation.

Usage:
    python test_site_map_validator.py

This script tests:
1. Site map loading and node indexing
2. URL to node matching via path_regex
3. Control validation against DOM nodes
4. Navigation outcome prediction
5. Navigation success validation
"""

import sys
import os

# Add the parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import directly from file to avoid full package dependencies
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "visual_copilot", "navigation"))
from site_map_validator import (
    SiteMapValidator,
    ValidationResult,
    get_validator,
    reset_validator,
)


class MockDOMNode:
    """Mock DOM node for testing."""
    def __init__(self, node_id, text, tag="button", interactive=True, zone="main", **kwargs):
        self.id = node_id
        self.text = text
        self.tag = tag
        self.interactive = interactive
        self.zone = zone
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_site_map_loading():
    """Test that site map loads correctly."""
    print("\n=== Test: Site Map Loading ===")
    validator = SiteMapValidator()

    assert validator.site_map is not None, "Site map should be loaded"
    assert "root" in validator.site_map, "Site map should have root node"
    assert len(validator.node_index) > 0, "Node index should not be empty"

    print(f"✓ Site map loaded successfully")
    print(f"  - Domain: {validator.site_map.get('site_metadata', {}).get('domain', 'unknown')}")
    print(f"  - Total nodes: {len(validator.node_index)}")
    return True


def test_url_matching():
    """Test URL to node matching."""
    print("\n=== Test: URL Matching ===")
    validator = SiteMapValidator()

    # Test dashboard URL
    dashboard_node = validator.get_node_for_url("https://console.groq.com/dashboard")
    assert dashboard_node is not None, "Should find dashboard node"
    assert dashboard_node.get("node_id") == "dashboard_main", f"Expected dashboard_main, got {dashboard_node.get('node_id')}"
    print(f"✓ Dashboard URL matches: {dashboard_node.get('title')}")

    # Test usage URL
    usage_node = validator.get_node_for_url("https://console.groq.com/dashboard/usage")
    assert usage_node is not None, "Should find usage node"
    assert usage_node.get("node_id") == "usage_section", f"Expected usage_section, got {usage_node.get('node_id')}"
    print(f"✓ Usage URL matches: {usage_node.get('title')}")

    # Test playground URL
    playground_node = validator.get_node_for_url("https://console.groq.com/playground")
    assert playground_node is not None, "Should find playground node"
    assert playground_node.get("node_id") == "playground", f"Expected playground, got {playground_node.get('node_id')}"
    print(f"✓ Playground URL matches: {playground_node.get('title')}")

    return True


def test_control_validation():
    """Test control validation against DOM nodes."""
    print("\n=== Test: Control Validation ===")
    validator = SiteMapValidator()

    # Create mock DOM nodes for usage page
    nodes = [
        MockDOMNode("t-1", "Date Picker", "button"),
        MockDOMNode("t-2", "Model Filter", "select"),
        MockDOMNode("t-3", "Activity Tab", "button"),
        MockDOMNode("t-4", "Cost Tab", "button"),
    ]

    # Validate usage page
    result = validator.validate_page_state(
        url="https://console.groq.com/dashboard/usage",
        nodes=nodes
    )

    assert result.is_valid, f"Validation should pass: {result.validation_message}"
    assert result.current_node is not None, "Should identify current node"
    assert result.current_node.get("node_id") == "usage_section", "Should be on usage_section"

    print(f"✓ Control validation passed")
    print(f"  - Present controls: {result.present_expected_controls}")
    print(f"  - Missing controls: {result.missing_expected_controls}")

    return True


def test_navigation_outcome_prediction():
    """Test navigation outcome prediction."""
    print("\n=== Test: Navigation Outcome Prediction ===")
    validator = SiteMapValidator()

    # From dashboard, clicking "Usage" should lead to usage_section
    outcome = validator.get_expected_navigation_outcome("dashboard_main", "Usage")

    assert outcome is not None, "Should predict navigation outcome"
    assert outcome.expected_node is not None, "Should have expected node"
    assert outcome.expected_node.get("node_id") == "usage_section", \
        f"Expected usage_section, got {outcome.expected_node.get('node_id')}"

    print(f"✓ Navigation outcome predicted")
    print(f"  - From: dashboard_main")
    print(f"  - Click: Usage")
    print(f"  - Expected: {outcome.expected_node.get('title')} ({outcome.expected_node.get('node_id')})")
    print(f"  - Confidence: {outcome.confidence:.2f}")

    return True


def test_navigation_validation():
    """Test navigation success validation."""
    print("\n=== Test: Navigation Validation ===")
    validator = SiteMapValidator()

    # Test successful navigation
    is_valid, reason = validator.validate_navigation_success(
        from_url="https://console.groq.com/dashboard",
        to_url="https://console.groq.com/dashboard/usage",
        clicked_target="Usage"
    )

    assert is_valid, f"Navigation should be valid: {reason}"
    print(f"✓ Navigation validation passed: {reason}")

    # Test failed navigation (wrong destination)
    is_valid, reason = validator.validate_navigation_success(
        from_url="https://console.groq.com/dashboard",
        to_url="https://console.groq.com/dashboard/logs",  # Wrong destination
        clicked_target="Usage"
    )

    assert not is_valid, "Navigation should be invalid (wrong destination)"
    print(f"✓ Wrong destination detected: {reason}")

    # Test failed navigation (same page)
    is_valid, reason = validator.validate_navigation_success(
        from_url="https://console.groq.com/dashboard",
        to_url="https://console.groq.com/dashboard",  # Same page
        clicked_target="Usage"
    )

    assert not is_valid, "Navigation should be invalid (same page)"
    print(f"✓ Same page detected: {reason}")

    return True


def test_terminal_capabilities():
    """Test terminal capability retrieval."""
    print("\n=== Test: Terminal Capabilities ===")
    validator = SiteMapValidator()

    # Get capabilities for usage page
    caps = validator.get_terminal_capabilities("https://console.groq.com/dashboard/usage")

    assert len(caps) > 0, "Usage page should have terminal capabilities"
    print(f"✓ Terminal capabilities retrieved: {caps}")

    return True


def test_breadcrumb():
    """Test breadcrumb generation."""
    print("\n=== Test: Breadcrumb Generation ===")
    validator = SiteMapValidator()

    # Get breadcrumb for usage page
    breadcrumb = validator.get_breadcrumb("https://console.groq.com/dashboard/usage")

    assert len(breadcrumb) > 0, "Should have breadcrumb"
    titles = [n.get("title", "") for n in breadcrumb]
    print(f"✓ Breadcrumb: {' → '.join(titles)}")

    return True


def test_parent_navigation():
    """Test parent node retrieval."""
    print("\n=== Test: Parent Navigation ===")
    validator = SiteMapValidator()

    # Get parent of usage_section
    parent = validator.get_parent_node("usage_section")

    assert parent is not None, "usage_section should have a parent"
    assert parent.get("node_id") == "dashboard_main", f"Expected dashboard_main, got {parent.get('node_id')}"
    print(f"✓ Parent found: {parent.get('title')} ({parent.get('node_id')})")

    return True


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("SiteMapValidator Test Suite")
    print("=" * 60)

    tests = [
        test_site_map_loading,
        test_url_matching,
        test_control_validation,
        test_navigation_outcome_prediction,
        test_navigation_validation,
        test_terminal_capabilities,
        test_breadcrumb,
        test_parent_navigation,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
        except Exception as e:
            failed += 1
            print(f"✗ {test.__name__} failed: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"Test Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)

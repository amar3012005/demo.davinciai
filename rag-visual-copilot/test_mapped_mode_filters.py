#!/usr/bin/env python3
"""
Test Mapped Mode Filter Clicking

Validates that MappedExtractionStateMachine properly clicks date picker
and metric tabs using vision hints and site map expected_controls.

Usage:
    python3 test_mapped_mode_filters.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from visual_copilot.mission.last_mile_tools import (
    MappedTerminalContext,
    MappedExtractionStateMachine,
    MappedExtractionState,
)


def test_validate_scope_enters_set_filters():
    """Test that validate_scope enters SET_FILTERS when goal_filters specified."""
    context = MappedTerminalContext(
        url="https://console.groq.com/dashboard/usage",
        current_node_id="usage_section",
        goal_entity="Whisper",
        goal_metric="tokens",
        goal_filters={"date_range": "last_7_days"},
        is_mapped=True,
    )

    machine = MappedExtractionStateMachine(context, max_attempts=15)

    # Initial state should be VALIDATE_NODE
    assert machine.state == MappedExtractionState.VALIDATE_NODE

    # Simulate observation with expected controls present
    observation = {
        "nodes": [],
        "readable_content": "Usage and Spend dashboard",
        "url_params": {},
        "clicked_ids": set(),
        "vision_hints": "",
    }

    # First transition: VALIDATE_NODE → should wait then move to VALIDATE_SCOPE
    action1, _ = machine.transition(observation)
    print(f"  Transition 1: state={machine.state.value}, action={action1.tool}")

    # Second transition: should move to SET_FILTERS (not LOCATE_ENTITY)
    action2, _ = machine.transition(observation)
    print(f"  Transition 2: state={machine.state.value}, action={action2.tool}")

    # Verify we entered SET_FILTERS state
    if machine.state == MappedExtractionState.SET_FILTERS:
        print("  ✓ PASS: Entered SET_FILTERS state")
        return True
    else:
        print(f"  ✗ FAIL: Expected SET_FILTERS, got {machine.state.value}")
        return False


def test_set_filters_uses_vision_hints():
    """Test that SET_FILTERS uses vision hints to identify date picker."""
    context = MappedTerminalContext(
        url="https://console.groq.com/dashboard/usage",
        current_node_id="usage_section",
        goal_filters={"date_range": "last_7_days"},
        is_mapped=True,
    )

    machine = MappedExtractionStateMachine(context, max_attempts=15)
    machine.state = MappedExtractionState.SET_FILTERS  # Force into SET_FILTERS

    # Vision hints with date picker target
    observation = {
        "nodes": [],
        "readable_content": "",
        "url_params": {},
        "clicked_ids": set(),
        "vision_hints": "Vision bootstrap: identified t-123abc (Date Range Picker) for time filter",
    }

    action, _ = machine.transition(observation)

    # Should generate click_element for date picker
    if action.tool == "click_element" and action.target_id == "t-123abc":
        print("  ✓ PASS: Used vision hint for date picker click")
        return True
    else:
        print(f"  ✗ FAIL: Expected click_element with t-123abc, got {action.tool} target={action.target_id}")
        return False


def test_set_filters_uses_site_map_controls():
    """Test that SET_FILTERS uses expected_controls from site map."""
    context = MappedTerminalContext(
        url="https://console.groq.com/dashboard/usage",
        current_node_id="usage_section",
        goal_filters={"date_range": "last_7_days"},
        expected_controls=["Date Picker", "Model Filter", "Activity Tab", "Cost Tab"],
        is_mapped=True,
    )

    machine = MappedExtractionStateMachine(context, max_attempts=15)
    machine.state = MappedExtractionState.SET_FILTERS

    # Mock DOM nodes with date picker
    class MockNode:
        def __init__(self, id, text, interactive=True):
            self.id = id
            self.text = text
            self.interactive = interactive

    observation = {
        "nodes": [
            MockNode("t-xyz1", "Dashboard"),
            MockNode("t-xyz2", "Date Range", interactive=True),  # Should match "Date Picker"
            MockNode("t-xyz3", "Model Filter", interactive=True),
        ],
        "readable_content": "",
        "url_params": {},
        "clicked_ids": set(),
        "vision_hints": "",  # No vision hints
    }

    action, _ = machine.transition(observation)

    # Should find and click date-related control
    if action.tool == "click_element":
        print(f"  ✓ PASS: Found date picker from site map (target={action.target_id})")
        return True
    else:
        print(f"  ✗ FAIL: Expected click_element, got {action.tool}")
        return False


def test_locate_metric_uses_vision_hints():
    """Test that LOCATE_METRIC uses vision hints for metric tab."""
    context = MappedTerminalContext(
        url="https://console.groq.com/dashboard/usage",
        current_node_id="usage_section",
        goal_metric="tokens",
        is_mapped=True,
    )

    machine = MappedExtractionStateMachine(context, max_attempts=15)
    machine.state = MappedExtractionState.LOCATE_METRIC

    # Vision hints with metric tab target
    observation = {
        "nodes": [],
        "readable_content": "Usage data",  # No metric visible yet
        "url_params": {},
        "clicked_ids": set(),
        "vision_hints": "Vision bootstrap: click t-tab456 (Activity tab) for token usage view",
    }

    action, _ = machine.transition(observation)

    # Should generate click_element for metric tab
    if action.tool == "click_element" and action.target_id == "t-tab456":
        print("  ✓ PASS: Used vision hint for metric tab click")
        return True
    else:
        print(f"  ✗ FAIL: Expected click_element with t-tab456, got {action.tool} target={action.target_id}")
        return False


def test_full_sequence_with_filters():
    """Test full state sequence: VALIDATE_NODE → SET_FILTERS → LOCATE_ENTITY → LOCATE_METRIC."""
    context = MappedTerminalContext(
        url="https://console.groq.com/dashboard/usage",
        current_node_id="usage_section",
        goal_entity="Whisper",
        goal_metric="tokens",
        goal_filters={"date_range": "last_7_days"},
        expected_controls=["Date Picker", "Model Filter"],
        is_mapped=True,
    )

    machine = MappedExtractionStateMachine(context, max_attempts=15)

    # Mock DOM nodes
    class MockNode:
        def __init__(self, id, text, interactive=True):
            self.id = id
            self.text = text
            self.interactive = interactive

    nodes = [
        MockNode("t-date1", "Date Range", interactive=True),
        MockNode("t-model1", "Show all Models", interactive=True),
        MockNode("t-usage1", "Whisper token usage: 1.5M"),
    ]

    # Vision hints from bootstrap
    vision_hints = """
    Vision Strategic Brief:
    - Date picker: click t-date1 for time range selection
    - Model filter: click t-model1 to expand model list
    - Metrics visible: Activity tab shows token usage
    """

    states_visited = []
    actions_taken = []

    for i in range(10):  # Max iterations
        observation = {
            "nodes": nodes,
            "readable_content": "Whisper token usage: 1.5M in last 7 days",
            "url_params": {},
            "clicked_ids": set(),
            "vision_hints": vision_hints,
        }

        action, is_terminal = machine.transition(observation)
        states_visited.append(machine.state.value)
        actions_taken.append(action.tool)

        if is_terminal:
            break

        # Simulate clicking by adding target to clicked_ids
        if action.target_id:
            observation["clicked_ids"].add(action.target_id)

        if machine.state == MappedExtractionState.COMPLETE:
            break

    print(f"  States visited: {' → '.join(states_visited)}")
    print(f"  Actions taken: {actions_taken[:5]}")

    # Verify we went through SET_FILTERS
    if "set_filters" in states_visited:
        print("  ✓ PASS: Visited SET_FILTERS state")
        return True
    else:
        print(f"  ✗ FAIL: Never visited SET_FILTERS")
        return False


def run_tests():
    """Run all filter-related tests."""
    print("\n" + "="*70)
    print("MAPPED MODE FILTER CLICKING TESTS")
    print("="*70)

    tests = [
        ("Validate Scope → Set Filters", test_validate_scope_enters_set_filters),
        ("Set Filters Uses Vision Hints", test_set_filters_uses_vision_hints),
        ("Set Filters Uses Site Map", test_set_filters_uses_site_map_controls),
        ("Locate Metric Uses Vision", test_locate_metric_uses_vision_hints),
        ("Full Sequence with Filters", test_full_sequence_with_filters),
    ]

    results = []
    for name, test_fn in tests:
        print(f"\n{name}:")
        try:
            passed = test_fn()
            results.append((name, passed))
        except Exception as e:
            print(f"  ✗ FAIL: Exception - {e}")
            results.append((name, False))

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    passed = sum(1 for _, p in results if p)
    total = len(results)
    print(f"  Passed: {passed}/{total}")

    if passed == total:
        print("\n  🎉 All tests passed!")
        return 0
    else:
        print(f"\n  ⚠️  {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(run_tests())

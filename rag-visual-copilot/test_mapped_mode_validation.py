#!/usr/bin/env python3
"""
Mapped Mode Validation Test Suite

This script validates the Priority 1 rollout of mapped mode:
1. MapGuard: Enable mapped mode & verify prerequisites
2. LastMile Surgeon: Test mapped extraction state machine
3. Toolsmith: Test completion gates & intent resolution

Usage:
    python3 test_mapped_mode_validation.py

Environment:
    export KNOWN_SITE_MAPPED_MODE_ENABLED=true
    export MAPPED_LAST_MILE_ENABLED=true
    export MAPPED_COMPLETION_CONTRACT_ENABLED=true
    export MAPPED_INTENT_GROUP_RESOLUTION_ENABLED=true
"""

import os
import sys
import json
from typing import Dict, Any, List, Tuple

# Set feature flags for testing
os.environ["KNOWN_SITE_MAPPED_MODE_ENABLED"] = "true"
os.environ["MAPPED_LAST_MILE_ENABLED"] = "true"
os.environ["MAPPED_COMPLETION_CONTRACT_ENABLED"] = "true"
os.environ["MAPPED_INTENT_GROUP_RESOLUTION_ENABLED"] = "true"

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ═══════════════════════════════════════════════════════════════════════
# Test 1: MapGuard - Site Map Validation
# ═══════════════════════════════════════════════════════════════════════

def test_site_map_loaded() -> Tuple[bool, str]:
    """Test 1.1: Verify site map loads correctly."""
    from visual_copilot.navigation.site_map_validator import SiteMapValidator

    try:
        validator = SiteMapValidator()
        domain = validator.site_map.get("site_metadata", {}).get("domain", "unknown")
        node_count = len(validator.node_index)

        if node_count > 0:
            return True, f"Site map loaded: domain={domain}, nodes={node_count}"
        return False, "Site map loaded but no nodes found"
    except Exception as e:
        return False, f"Site map failed to load: {e}"


def test_usage_node_recognized() -> Tuple[bool, str]:
    """Test 1.2: Verify usage_section node is recognized."""
    from visual_copilot.navigation.site_map_validator import SiteMapValidator

    try:
        validator = SiteMapValidator()
        test_url = "https://console.groq.com/dashboard/usage"
        node = validator.get_node_for_url(test_url)

        if node:
            node_id = node.get("node_id", "unknown")
            controls = node.get("expected_controls", [])
            required = node.get("required_controls", [])
            caps = node.get("terminal_capabilities", [])

            return True, (
                f"usage_section recognized: node_id={node_id}, "
                f"expected_controls={len(controls)}, required={len(required)}, "
                f"capabilities={len(caps)}"
            )
        return False, "usage_section node not found for test URL"
    except Exception as e:
        return False, f"Node recognition failed: {e}"


def test_mapped_terminal_context_creation() -> Tuple[bool, str]:
    """Test 1.3: Verify MappedTerminalContext creation."""
    from visual_copilot.models.contracts import MappedTerminalContext

    try:
        ctx = MappedTerminalContext(
            mapped_mode=True,
            expected_node_id="usage_section",
            mapped_terminal_node="Usage and Spend",
            required_controls=["Date Picker", "Model Filter"],
            control_groups={
                "date_filter": [{"id": "t-1", "text": "Last 7 days"}],
                "entity_filter": [{"id": "t-2", "text": "Whisper"}],
                "metric_tabs": [{"id": "t-3", "text": "Usage"}, {"id": "t-4", "text": "Cost"}],
            },
            allowed_terminal_capabilities=["read_usage_tokens", "read_cost"],
        )

        is_valid = ctx.is_valid_terminal()
        ctx_dict = ctx.to_dict()

        if is_valid and ctx_dict.get("mapped_mode"):
            return True, f"MappedTerminalContext created: valid={is_valid}, node={ctx.expected_node_id}"
        return False, "MappedTerminalContext validation failed"
    except Exception as e:
        return False, f"Context creation failed: {e}"


def test_feature_flags_enabled() -> Tuple[bool, str]:
    """Test 1.4: Verify all feature flags are enabled."""
    flags = {
        "KNOWN_SITE_MAPPED_MODE_ENABLED": os.getenv("KNOWN_SITE_MAPPED_MODE_ENABLED", "false"),
        "MAPPED_LAST_MILE_ENABLED": os.getenv("MAPPED_LAST_MILE_ENABLED", "false"),
        "MAPPED_COMPLETION_CONTRACT_ENABLED": os.getenv("MAPPED_COMPLETION_CONTRACT_ENABLED", "false"),
        "MAPPED_INTENT_GROUP_RESOLUTION_ENABLED": os.getenv("MAPPED_INTENT_GROUP_RESOLUTION_ENABLED", "false"),
    }

    all_enabled = all(v.lower() in ("true", "1", "yes") for v in flags.values())

    if all_enabled:
        return True, f"All 4 feature flags enabled"
    return False, f"Some flags disabled: {flags}"


# ═══════════════════════════════════════════════════════════════════════
# Test 2: LastMile Surgeon - State Machine Validation
# ═══════════════════════════════════════════════════════════════════════

def test_state_machine_creation() -> Tuple[bool, str]:
    """Test 2.1: Verify MappedExtractionStateMachine creation."""
    from visual_copilot.mission.last_mile_tools import (
        MappedTerminalContext,
        MappedExtractionStateMachine,
        MappedExtractionState,
    )

    try:
        context = MappedTerminalContext(
            url="https://console.groq.com/dashboard/usage",
            current_node_id="usage_section",
            goal_entity="Whisper",
            goal_metric="tokens",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context, max_attempts=15)
        initial_state = machine.state.value

        if initial_state == "validate_node":
            return True, f"StateMachine created: initial_state={initial_state}"
        return False, f"Unexpected initial state: {initial_state}"
    except Exception as e:
        return False, f"StateMachine creation failed: {e}"


def test_state_transitions() -> Tuple[bool, str]:
    """Test 2.2: Verify state machine transitions correctly."""
    from visual_copilot.mission.last_mile_tools import (
        MappedTerminalContext,
        MappedExtractionStateMachine,
        MappedExtractionState,
    )

    try:
        context = MappedTerminalContext(
            url="https://console.groq.com/dashboard/usage",
            current_node_id="usage_section",
            goal_entity="Whisper",
            goal_metric="tokens",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context, max_attempts=15)

        # Simulate observation with readable content
        observation = {
            "nodes": [],
            "readable_content": "Whisper token usage: 15,234 tokens",
            "url_params": {"model": "whisper"},
            "clicked_ids": set(),
        }

        # First transition: VALIDATE_NODE → VALIDATE_SCOPE
        action1, terminal1 = machine.transition(observation)
        state1 = machine.state.value

        # Second transition: should progress to LOCATE_ENTITY
        action2, terminal2 = machine.transition(observation)
        state2 = machine.state.value

        # Third transition: should progress to LOCATE_METRIC
        action3, terminal3 = machine.transition(observation)
        state3 = machine.state.value

        states_traversed = [
            "validate_node",  # initial
            state1,
            state2,
            state3,
        ]

        # Verify we're progressing through states
        unique_states = len(set(states_traversed))

        if unique_states >= 2:
            return True, f"States traversed: {' → '.join(states_traversed[:4])}"
        return False, f"State machine not progressing: stuck at {state1}"
    except Exception as e:
        return False, f"State transition failed: {e}"


def test_url_only_rejection() -> Tuple[bool, str]:
    """Test 2.3: Verify URL-only evidence doesn't allow completion."""
    from visual_copilot.mission.last_mile_tools import (
        MappedTerminalContext,
        MappedExtractionStateMachine,
    )

    try:
        context = MappedTerminalContext(
            url="https://console.groq.com/dashboard/usage?model=whisper&days=7",
            current_node_id="usage_section",
            goal_entity="Whisper",
            goal_metric="tokens",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context, max_attempts=15)

        # Observation with URL params but no page content
        observation = {
            "nodes": [],
            "readable_content": "",  # No readable content
            "url_params": {"model": "whisper", "days": "7"},
            "clicked_ids": set(),
        }

        # Run multiple transitions
        for i in range(5):
            action, is_terminal = machine.transition(observation)

            # Should NOT complete with URL-only evidence
            if is_terminal and action.type == "complete":
                return False, "FAIL: URL-only evidence allowed completion"

            # Should eventually escalate or require content
            if action.type == "escalate":
                return True, f"Correctly rejected URL-only: reason={action.why[:100]}"

        return True, f"URL-only rejection working: state={machine.state.value}, action={action.type}"
    except Exception as e:
        return False, f"URL-only rejection test failed: {e}"


# ═══════════════════════════════════════════════════════════════════════
# Test 3: Toolsmith - Completion Contract Validation
# ═══════════════════════════════════════════════════════════════════════

def test_completion_contract_creation() -> Tuple[bool, str]:
    """Test 3.1: Verify CompletionContract creation."""
    from visual_copilot.mission.tool_executor import CompletionContract

    try:
        contract = CompletionContract(
            expected_node_id="usage_section",
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
            allow_url_scope_only=True,
            allow_url_answer=False,
            allow_vision_only=False,
        )

        return True, (
            f"CompletionContract created: entity={contract.required_entity}, "
            f"metric={contract.required_metric_family}, "
            f"require_value={contract.require_numeric_value}"
        )
    except Exception as e:
        return False, f"Contract creation failed: {e}"


def test_3_anchor_validation() -> Tuple[bool, str]:
    """Test 3.2: Verify 3-anchor validation works."""
    from visual_copilot.mission.tool_executor import CompletionContract

    try:
        contract = CompletionContract(
            expected_node_id="usage_section",
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
            allow_url_scope_only=True,
            allow_url_answer=False,
            allow_vision_only=False,
        )

        # Test case 1: All 3 anchors present
        response = "Whisper usage: 15,234 tokens in last 7 days"
        evidence_refs = "Model: Whisper | Metric: tokens | Value: 15,234"

        is_valid, rejection, evidence = contract.validate(
            response=response,
            evidence_refs=evidence_refs,
            url_evidence={"model": "whisper"},
            nodes=[],
            vision_hints=None,
        )

        if is_valid:
            return True, (
                f"3-anchor validation passed: has_entity={evidence.get('has_entity')}, "
                f"has_metric={evidence.get('has_metric')}, "
                f"has_numeric_value={evidence.get('has_numeric_value')}"
            )
        return False, f"Valid 3-anchor response rejected: {rejection[:100]}"
    except Exception as e:
        return False, f"3-anchor validation failed: {e}"


def test_missing_entity_rejected() -> Tuple[bool, str]:
    """Test 3.3: Verify missing entity anchor is rejected."""
    from visual_copilot.mission.tool_executor import CompletionContract

    try:
        contract = CompletionContract(
            expected_node_id="usage_section",
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
            allow_url_scope_only=True,
            allow_url_answer=False,
            allow_vision_only=False,
        )

        # Test case: Entity missing
        response = "Usage: 15,234 tokens in last 7 days"  # No "Whisper"
        evidence_refs = "Metric: tokens | Value: 15,234"

        is_valid, rejection, _ = contract.validate(
            response=response,
            evidence_refs=evidence_refs,
            url_evidence={},
            nodes=[],
            vision_hints=None,
        )

        if not is_valid and "Entity" in rejection:
            return True, f"Correctly rejected missing entity: {rejection[:80]}"
        return False, f"Should reject missing entity but got: valid={is_valid}"
    except Exception as e:
        return False, f"Missing entity test failed: {e}"


def test_url_only_answer_rejected() -> Tuple[bool, str]:
    """Test 3.4: Verify URL-only answers are rejected."""
    from visual_copilot.mission.tool_executor import CompletionContract

    try:
        contract = CompletionContract(
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
            allow_url_scope_only=True,
            allow_url_answer=False,
            allow_vision_only=False,
        )

        # Test case: Answer derived only from URL
        response = "URL shows whisper model and 7 days"
        evidence_refs = "Based on the URL parameters"

        is_valid, rejection, _ = contract.validate(
            response=response,
            evidence_refs=evidence_refs,
            url_evidence={"model": "whisper", "days": "7"},
            nodes=[],
            vision_hints=None,
        )

        if not is_valid:
            return True, f"Correctly rejected URL-only answer: {rejection[:80]}"
        return False, f"Should reject URL-only answer but got: valid={is_valid}"
    except Exception as e:
        return False, f"URL-only rejection test failed: {e}"


def test_intent_group_resolution() -> Tuple[bool, str]:
    """Test 3.5: Verify node-scoped intent resolution."""
    from visual_copilot.mission.tool_executor import _resolve_intent_in_control_groups

    try:
        control_groups = {
            "date_filter": [
                {"id": "t-1", "text": "Last 7 days", "tag": "button", "interactive": True},
                {"id": "t-2", "text": "Last 30 days", "tag": "button", "interactive": True},
            ],
            "entity_filter": [
                {"id": "t-3", "text": "Whisper", "tag": "select", "interactive": True},
                {"id": "t-4", "text": "Llama-3", "tag": "select", "interactive": True},
            ],
        }

        # Test: Resolve "Whisper" in entity_filter group
        target_id, reason = _resolve_intent_in_control_groups(
            intent={"text_label": "Whisper", "element_type": "select"},
            control_groups=control_groups,
            target_group="entity_filter",
            excluded_ids=set(),
        )

        if target_id == "t-3":
            return True, f"Intent resolved correctly: target_id={target_id}, reason={reason[:50]}"
        return False, f"Wrong target resolved: got {target_id}, expected t-3"
    except Exception as e:
        return False, f"Intent group resolution failed: {e}"


# ═══════════════════════════════════════════════════════════════════════
# Main Test Runner
# ═══════════════════════════════════════════════════════════════════════

def run_tests() -> Dict[str, List[Tuple[str, bool, str]]]:
    """Run all validation tests and return results."""

    results = {
        "MapGuard": [],
        "LastMile Surgeon": [],
        "Toolsmith": [],
    }

    # MapGuard Tests
    print("\n" + "="*70)
    print("MAPGUARD VALIDATION")
    print("="*70)

    mapguard_tests = [
        ("Site Map Loaded", test_site_map_loaded),
        ("Usage Node Recognized", test_usage_node_recognized),
        ("MappedTerminalContext Created", test_mapped_terminal_context_creation),
        ("Feature Flags Enabled", test_feature_flags_enabled),
    ]

    for name, test_fn in mapguard_tests:
        passed, message = test_fn()
        results["MapGuard"].append((name, passed, message))
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}: {name}")
        print(f"         {message}")

    # LastMile Surgeon Tests
    print("\n" + "="*70)
    print("LASTMILE SURGEON VALIDATION")
    print("="*70)

    lastmile_tests = [
        ("StateMachine Created", test_state_machine_creation),
        ("State Transitions", test_state_transitions),
        ("URL-Only Rejection", test_url_only_rejection),
    ]

    for name, test_fn in lastmile_tests:
        passed, message = test_fn()
        results["LastMile Surgeon"].append((name, passed, message))
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}: {name}")
        print(f"         {message}")

    # Toolsmith Tests
    print("\n" + "="*70)
    print("TOOLSMITH VALIDATION")
    print("="*70)

    toolsmith_tests = [
        ("CompletionContract Created", test_completion_contract_creation),
        ("3-Anchor Validation", test_3_anchor_validation),
        ("Missing Entity Rejected", test_missing_entity_rejected),
        ("URL-Only Answer Rejected", test_url_only_answer_rejected),
        ("Intent Group Resolution", test_intent_group_resolution),
    ]

    for name, test_fn in toolsmith_tests:
        passed, message = test_fn()
        results["Toolsmith"].append((name, passed, message))
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}: {name}")
        print(f"         {message}")

    return results


def summary(results: Dict[str, List[Tuple[str, bool, str]]]) -> str:
    """Generate summary report."""
    total_tests = 0
    total_passed = 0

    lines = ["\n" + "="*70, "VALIDATION SUMMARY", "="*70]

    for category, tests in results.items():
        passed = sum(1 for _, p, _ in tests if p)
        total = len(tests)
        total_tests += total
        total_passed += passed

        pct = (passed / total * 100) if total > 0 else 0
        lines.append(f"  {category}: {passed}/{total} ({pct:.0f}%)")

    lines.append("-"*70)
    overall_pct = (total_passed / total_tests * 100) if total_tests > 0 else 0
    lines.append(f"  OVERALL: {total_passed}/{total_tests} ({overall_pct:.0f}%)")

    if overall_pct == 100:
        lines.append("\n  🎉 ALL VALIDATION TESTS PASSED - Ready for live testing!")
    elif overall_pct >= 80:
        lines.append("\n  ⚠️  Most tests passed - Review failures before live testing")
    else:
        lines.append("\n  ❌ Critical failures detected - Fix before proceeding")

    return "\n".join(lines)


if __name__ == "__main__":
    print("\n" + "="*70)
    print("MAPPED MODE PRIORITY 1 VALIDATION")
    print("="*70)
    print("Testing: MapGuard + LastMile Surgeon + Toolsmith")
    print("Feature Flags: All set to 'true' via environment")

    results = run_tests()
    print(summary(results))

    # Exit with error code if any tests failed
    total_failed = sum(1 for tests in results.values() for _, p, _ in tests if not p)
    sys.exit(1 if total_failed > 0 else 0)

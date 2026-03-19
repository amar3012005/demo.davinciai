"""
Regression Tests for Mapped Last-Mile Extraction

These tests verify that:
1. Known-site last-mile cannot terminate from URL/date evidence alone
2. complete_mission cannot fire on mapped pages without actual page-grounded extraction
3. Exploratory one-call loop remains available for unknown sites (fallback)
4. MAPPED_LAST_MILE_ENABLED flag properly gates the mapped path
"""

import pytest
from unittest.mock import MagicMock, patch
import os


class TestMappedLastMileInvariants:
    """Test that mapped-mode completion invariants are enforced."""

    def test_url_evidence_cannot_answer_truth(self):
        """
        Test 1: URL evidence may confirm filter scope, never answer truth.

        Even if the URL contains 'usage?model=whisper&days=7',
        this should NOT be treated as evidence that Whisper tokens exist.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedTerminalContext,
            MappedExtractionStateMachine,
        )

        # Create context for a mapped page
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage?model=whisper&days=7",
            current_node_id="usage_dashboard",
            current_node_title="Usage Dashboard",
            goal_entity="Whisper",
            goal_metric="tokens",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context)

        # Simulate observation with URL-like readable content
        observation = {
            "nodes": [],
            "readable_content": "Usage Dashboard - Model: whisper, Days: 7",
            "url_params": {"model": "whisper", "days": "7"},
            "clicked_ids": set(),
        }

        # Try to "complete" with URL-only evidence
        action, is_terminal = machine.transition(observation)

        # Should NOT allow completion - needs entity+metric+value from readable content
        assert action.type != "complete", "URL-only evidence should not allow completion"

    def test_vision_suggests_not_authorizes(self):
        """
        Test 2: Vision may suggest controls, not authorize completion.

        Vision bootstrap might say "data is visible in table",
        but this alone should not trigger complete_mission.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedTerminalContext,
            MappedExtractionStateMachine,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            current_node_id="usage_dashboard",
            goal_entity="GPT-4",
            goal_metric="cost",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context)

        # Vision hints saying answer is visible
        observation = {
            "nodes": [],
            "readable_content": "Vision says: data table with GPT-4 cost visible",
            "url_params": {},
            "clicked_ids": set(),
            "vision_hints": "Answer visible: yes",
        }

        action, is_terminal = machine.transition(observation)

        # Should still require actual extraction
        assert not is_terminal or action.type != "complete", \
            "Vision hints alone should not authorize completion"

    def test_completion_requires_all_invariants(self):
        """
        Test 3: Completion requires all four invariants:
        - Entity anchor
        - Metric anchor
        - Numeric value
        - Evidence from readable content (not URL)
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedTerminalContext,
            MappedExtractionStateMachine,
            MappedExtractionState,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            current_node_id="usage_dashboard",
            goal_entity="Whisper",
            goal_metric="tokens",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context)

        # Test with only entity - should fail
        context.entity_anchor_found = True
        context.metric_anchor_found = False
        context.numeric_value_found = False
        passed, reason = machine._validate_mapped_completion(
            context, "some content", {}
        )
        assert not passed, "Should fail without metric anchor"
        assert "metric" in reason.lower()

        # Test with entity+metric but no value - should fail
        context.entity_anchor_found = True
        context.metric_anchor_found = True
        context.numeric_value_found = False
        passed, reason = machine._validate_mapped_completion(
            context, "some content", {}
        )
        assert not passed, "Should fail without numeric value"
        assert "numeric" in reason.lower() or "value" in reason.lower()

        # Test with all invariants - should pass
        context.entity_anchor_found = True
        context.metric_anchor_found = True
        context.numeric_value_found = True
        context.evidence_text = "Whisper usage: 1.2M tokens this month"
        passed, reason = machine._validate_mapped_completion(
            context, "Whisper usage: 1.2M tokens this month", {}
        )
        assert passed, "Should pass with all invariants satisfied"

    def test_url_only_evidence_detection(self):
        """
        Test 4: Evidence derived only from URL parameters is rejected.

        If evidence_text just echoes URL params, it's URL-only evidence.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedExtractionStateMachine,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(url="https://example.com/usage?model=whisper")
        machine = MappedExtractionStateMachine(context)

        # Evidence that just echoes URL params
        evidence = "model=whisper"
        url_params = {"model": "whisper"}

        is_url_only = machine._is_url_only_evidence(evidence, url_params)
        assert is_url_only, "Should detect URL-only evidence"

        # Real page content evidence
        real_evidence = "Whisper model usage statistics: 1.2M tokens processed"
        is_url_only = machine._is_url_only_evidence(real_evidence, url_params)
        assert not is_url_only, "Should not flag real content as URL-only"


class TestMappedVsExploratoryPaths:
    """Test that mapped and exploratory paths are properly split."""

    @patch.dict(os.environ, {"MAPPED_LAST_MILE_ENABLED": "true"})
    def test_mapped_path_used_for_known_sites(self):
        """
        Test 5: When MAPPED_LAST_MILE_ENABLED=true and URL matches site map,
        use the mapped deterministic path.
        """
        from visual_copilot.mission.last_mile_tools import is_mapped_site

        # Mock the site map validator
        with patch("visual_copilot.mission.last_mile_tools.SiteMapValidator") as MockValidator:
            mock_validator = MagicMock()
            mock_validator.get_node_for_url.return_value = {
                "node_id": "usage_dashboard",
                "title": "Usage Dashboard",
            }
            MockValidator.return_value = mock_validator

            result = is_mapped_site("https://platform.openai.com/usage")
            assert result is True, "Should detect mapped site"

    @patch.dict(os.environ, {"MAPPED_LAST_MILE_ENABLED": "false"})
    def test_exploratory_path_used_when_disabled(self):
        """
        Test 6: When MAPPED_LAST_MILE_ENABLED=false, always use exploratory path.
        """
        from visual_copilot.mission.last_mile import MAPPED_LAST_MILE_ENABLED

        assert MAPPED_LAST_MILE_ENABLED is False, "Flag should be False when env var is 'false'"

    @patch.dict(os.environ, {"MAPPED_LAST_MILE_ENABLED": "true"})
    def test_exploratory_fallback_for_unknown_sites(self):
        """
        Test 7: For URLs not in site map, fall back to exploratory path.
        """
        from visual_copilot.mission.last_mile_tools import is_mapped_site

        with patch("visual_copilot.mission.last_mile_tools.SiteMapValidator") as MockValidator:
            mock_validator = MagicMock()
            mock_validator.get_node_for_url.return_value = None
            MockValidator.return_value = mock_validator

            result = is_mapped_site("https://unknown-site.com/page")
            assert result is False, "Should not detect unmapped site"


class TestExploratoryPathPreserved:
    """Test that exploratory path remains available and functional."""

    def test_exploratory_features_preserved(self):
        """
        Test 8: Exploratory path should retain:
        - Semantic stagnation detection
        - Visit graph tracking
        - Retry scaffolding
        - One-call reasoning
        """
        from visual_copilot.mission.last_mile import (
            LastMileState,
            COMPOUND_MAX_INTERNAL_ITERATIONS,
            SEMANTIC_REPEAT_MAX,
        )

        # Verify these constants still exist (exploratory features)
        assert COMPOUND_MAX_INTERNAL_ITERATIONS > 0
        assert SEMANTIC_REPEAT_MAX > 0

        # Verify state tracking
        state = LastMileState()
        assert state.semantic_repeat_streak == 0
        assert hasattr(state, "visit_graph")
        assert hasattr(state, "page_state_history")


class TestPrematureCompletionBlocked:
    """Test specific scenarios where premature completion should be blocked."""

    def test_date_in_url_not_answer(self):
        """
        Test 9: URL like /usage?date=2024-01 should not allow
        completion with "January 2024" as the answer.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedTerminalContext,
            MappedExtractionStateMachine,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/usage?date=2024-01",
            current_node_id="usage_dashboard",
            goal_entity="Total",
            goal_metric="usage",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context)

        # Try to complete with just date from URL
        observation = {
            "nodes": [],
            "readable_content": "Usage Dashboard - January 2024",  # From URL date
            "url_params": {"date": "2024-01"},
        }

        # Should require actual usage data, not just date
        is_url_derived = machine._is_url_only_evidence(
            "January 2024", {"date": "2024-01"}
        )
        assert is_url_derived, "Should detect URL-derived date evidence"

    def test_model_in_url_not_entity_anchor(self):
        """
        Test 10: URL like /usage?model=whisper should not count
        as having found the Whisper entity anchor.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedTerminalContext,
            MappedExtractionStateMachine,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/usage?model=whisper",
            current_node_id="usage_dashboard",
            goal_entity="Whisper",
            goal_metric="tokens",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context)

        # Readable content that only echoes URL
        readable = "Model selected: whisper"
        found, text = machine._find_entity_anchor(readable)

        # The _find_entity_anchor looks for the entity in readable content
        # But _validate_mapped_completion checks if evidence is URL-only
        assert "whisper" in readable.lower(), "Entity name appears in readable"

        # However, if we check if this is URL-only evidence...
        is_url_only = machine._is_url_only_evidence(
            "model selected: whisper",
            {"model": "whisper"}
        )
        # This should be detected as potentially URL-derived
        assert is_url_only, "Should detect URL-derived entity reference"


class TestStateMachineTransitions:
    """Test state machine transition logic."""

    def test_state_progression(self):
        """
        Test 11: State machine must progress through all required states.

        VALIDATE_NODE -> VALIDATE_SCOPE -> SET_FILTERS ->
        LOCATE_ENTITY -> LOCATE_METRIC -> EXTRACT_VALUE -> COMPLETE
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedExtractionState,
            MappedTerminalContext,
            MappedExtractionStateMachine,
        )

        context = MappedTerminalContext(
            url="https://example.com/usage",
            current_node_id="usage",
            goal_entity="GPT-4",
            goal_metric="cost",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context)

        # Should start at VALIDATE_NODE
        assert machine.state == MappedExtractionState.VALIDATE_NODE

        # After validation, should move to scope validation
        observation = {
            "nodes": [],
            "readable_content": "Usage page loaded",
            "url_params": {},
        }
        action, _ = machine.transition(observation)

        # State should have transitioned
        assert machine.state != MappedExtractionState.VALIDATE_NODE

    def test_cannot_skip_to_complete(self):
        """
        Test 12: Cannot jump directly to COMPLETE state.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedExtractionState,
            MappedTerminalContext,
            MappedExtractionStateMachine,
        )

        context = MappedTerminalContext(
            url="https://example.com/usage",
            is_mapped=True,
        )

        machine = MappedExtractionStateMachine(context)

        # Try to force complete state
        machine.state = MappedExtractionState.COMPLETE
        machine.context.entity_anchor_found = False
        machine.context.metric_anchor_found = False
        machine.context.numeric_value_found = False

        # Validation should fail
        passed, _ = machine._validate_mapped_completion(
            machine.context, "some content", {}
        )
        assert not passed, "Should not allow completion without invariants"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestCompletionContract:
    """Test CompletionContract validation for mapped extraction tasks."""

    def test_contract_rejects_missing_entity(self):
        """
        Test 13: CompletionContract rejects when entity anchor is missing.
        """
        from visual_copilot.mission.tool_executor import CompletionContract

        contract = CompletionContract(
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
        )

        response = "The usage shows 1.2M tokens this month"  # Missing "Whisper"
        evidence_refs = ""

        valid, reason, summary = contract.validate(
            response=response,
            evidence_refs=evidence_refs,
            url_evidence={},
            nodes=[],
        )

        assert not valid, "Should reject missing entity"
        assert "entity" in reason.lower(), f"Reason should mention entity: {reason}"
        assert summary["has_entity"] is False

    def test_contract_rejects_wrong_metric(self):
        """
        Test 14: CompletionContract rejects when metric mismatch (e.g., cost vs tokens).
        """
        from visual_copilot.mission.tool_executor import CompletionContract

        contract = CompletionContract(
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
        )

        response = "Whisper cost is $50 this month"  # Cost, not tokens
        evidence_refs = ""

        valid, reason, summary = contract.validate(
            response=response,
            evidence_refs=evidence_refs,
            url_evidence={},
            nodes=[],
        )

        assert not valid, "Should reject wrong metric"
        assert "metric" in reason.lower(), f"Reason should mention metric: {reason}"
        assert summary["has_metric"] is False

    def test_contract_rejects_missing_numeric_value(self):
        """
        Test 15: CompletionContract rejects when no numeric value present.
        """
        from visual_copilot.mission.tool_executor import CompletionContract

        contract = CompletionContract(
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
        )

        response = "Whisper has token usage this month"  # No number
        evidence_refs = ""

        valid, reason, summary = contract.validate(
            response=response,
            evidence_refs=evidence_refs,
            url_evidence={},
            nodes=[],
        )

        assert not valid, "Should reject missing numeric value"
        assert "value" in reason.lower() or "numeric" in reason.lower(), f"Reason should mention value: {reason}"
        assert summary["has_numeric_value"] is False

    def test_contract_rejects_url_only_evidence(self):
        """
        Test 16: CompletionContract rejects URL-only evidence.
        """
        from visual_copilot.mission.tool_executor import CompletionContract

        contract = CompletionContract(
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
            allow_url_answer=False,
        )

        # Answer derived only from URL parameters
        response = "Based on the URL parameters, the model is whisper and usage is shown"
        evidence_refs = "URL shows model=whisper"

        valid, reason, summary = contract.validate(
            response=response,
            evidence_refs=evidence_refs,
            url_evidence={"model_filter": "whisper"},
            nodes=[],
        )

        assert not valid, "Should reject URL-only evidence"
        assert "url" in reason.lower(), f"Reason should mention URL: {reason}"

    def test_contract_accepts_valid_extraction(self):
        """
        Test 17: CompletionContract accepts valid extraction with all anchors.
        """
        from visual_copilot.mission.tool_executor import CompletionContract

        contract = CompletionContract(
            required_entity="Whisper",
            required_metric_family="tokens",
            require_numeric_value=True,
        )

        response = "Whisper used 1,234,567 tokens this billing period"
        evidence_refs = "Usage table row: Whisper | 1,234,567 tokens"

        # Mock nodes with entity in content
        class MockNode:
            def __init__(self, text, zone="main"):
                self.text = text
                self.zone = zone

        nodes = [
            MockNode("Whisper usage statistics"),
            MockNode("1,234,567 tokens"),
        ]

        valid, reason, summary = contract.validate(
            response=response,
            evidence_refs=evidence_refs,
            url_evidence={},
            nodes=nodes,
        )

        assert valid, f"Should accept valid extraction: {reason}"
        assert summary["has_entity"] is True
        assert summary["has_metric"] is True
        assert summary["has_numeric_value"] is True


class TestEvidenceSummary:
    """Test EvidenceSummary separation of navigation vs answer evidence."""

    def test_navigation_evidence_insufficient(self):
        """
        Test 18: Navigation evidence alone cannot complete.
        """
        from visual_copilot.mission.tool_executor import EvidenceSummary

        summary = EvidenceSummary(
            navigation_evidence={
                "model_filter": "whisper",
                "date_range": {"days": 7},
                "matches_goal": True,
            },
            answer_evidence={
                "has_entity": False,
                "has_metric": False,
                "has_numeric_value": False,
            },
        )

        assert not summary.can_complete(), "Navigation-only evidence should not allow completion"
        assert summary.get_rejection_reason() == "missing_entity_anchor"

    def test_answer_evidence_allows_completion(self):
        """
        Test 19: Answer evidence with all anchors allows completion.
        """
        from visual_copilot.mission.tool_executor import EvidenceSummary

        summary = EvidenceSummary(
            navigation_evidence={
                "model_filter": "whisper",
                "date_range": {"days": 7},
            },
            answer_evidence={
                "has_entity": True,
                "has_metric": True,
                "has_numeric_value": True,
            },
        )

        assert summary.can_complete(), "Answer evidence with all anchors should allow completion"
        assert summary.get_rejection_reason() is None

    def test_structured_rejection_reasons(self):
        """
        Test 20: Rejection reasons are structured for retry logic.
        """
        from visual_copilot.mission.tool_executor import EvidenceSummary

        # Missing metric
        summary = EvidenceSummary(
            navigation_evidence={},
            answer_evidence={
                "has_entity": True,
                "has_metric": False,
                "has_numeric_value": True,
            },
        )
        assert summary.get_rejection_reason() == "missing_metric_anchor"

        # Missing value
        summary = EvidenceSummary(
            navigation_evidence={},
            answer_evidence={
                "has_entity": True,
                "has_metric": True,
                "has_numeric_value": False,
            },
        )
        assert summary.get_rejection_reason() == "missing_value_anchor"


class TestIntentControlGroupResolution:
    """Test intent resolution within control groups."""

    def test_resolve_in_entity_filter_group(self):
        """
        Test 21: Intent resolution bounded to entity_filter control group.
        """
        from visual_copilot.mission.tool_executor import _resolve_intent_in_control_groups

        control_groups = {
            "entity_filter": [
                {"id": "t-101", "text": "Whisper", "interactive": True, "tag": "div"},
                {"id": "t-102", "text": "GPT-4", "interactive": True, "tag": "div"},
                {"id": "t-103", "text": "Llama-3", "interactive": True, "tag": "div"},
            ],
            "date_filter": [
                {"id": "t-201", "text": "Last 7 days", "interactive": True, "tag": "button"},
                {"id": "t-202", "text": "Last 30 days", "interactive": True, "tag": "button"},
            ],
        }

        intent = {"text_label": "Whisper", "element_type": "div"}

        resolved_id, reason = _resolve_intent_in_control_groups(
            intent=intent,
            control_groups=control_groups,
            target_group="entity_filter",
            excluded_ids=set(),
        )

        assert resolved_id == "t-101", f"Should resolve to Whisper: {resolved_id}"
        assert "entity_filter" in reason, f"Reason should mention control group: {reason}"

    def test_resolve_in_date_filter_group(self):
        """
        Test 22: Intent resolution bounded to date_filter control group.
        """
        from visual_copilot.mission.tool_executor import _resolve_intent_in_control_groups

        control_groups = {
            "entity_filter": [
                {"id": "t-101", "text": "Whisper", "interactive": True, "tag": "div"},
            ],
            "date_filter": [
                {"id": "t-201", "text": "Last 7 days", "interactive": True, "tag": "button"},
                {"id": "t-202", "text": "Last 30 days", "interactive": True, "tag": "button"},
            ],
        }

        intent = {"text_label": "Last 7 days", "element_type": "button"}

        resolved_id, reason = _resolve_intent_in_control_groups(
            intent=intent,
            control_groups=control_groups,
            target_group="date_filter",
            excluded_ids=set(),
        )

        assert resolved_id == "t-201", f"Should resolve to Last 7 days: {resolved_id}"
        assert "date_filter" in reason, f"Reason should mention control group: {reason}"

    def test_excluded_ids_not_considered(self):
        """
        Test 23: Excluded IDs are not considered in control group resolution.
        """
        from visual_copilot.mission.tool_executor import _resolve_intent_in_control_groups

        control_groups = {
            "entity_filter": [
                {"id": "t-101", "text": "Whisper", "interactive": True, "tag": "div"},
                {"id": "t-102", "text": "GPT-4", "interactive": True, "tag": "div"},
            ],
        }

        intent = {"text_label": "Whisper", "element_type": "div"}

        resolved_id, reason = _resolve_intent_in_control_groups(
            intent=intent,
            control_groups=control_groups,
            target_group="entity_filter",
            excluded_ids={"t-101"},  # Whisper is excluded
        )

        assert resolved_id is None, "Should not resolve excluded ID"
        assert "no_match" in reason.lower(), f"Reason should indicate no match: {reason}"

    def test_nonexistent_control_group(self):
        """
        Test 24: Resolution fails gracefully for nonexistent control group.
        """
        from visual_copilot.mission.tool_executor import _resolve_intent_in_control_groups

        control_groups = {
            "entity_filter": [{"id": "t-101", "text": "Whisper", "interactive": True}],
        }

        intent = {"text_label": "Whisper"}

        resolved_id, reason = _resolve_intent_in_control_groups(
            intent=intent,
            control_groups=control_groups,
            target_group="nonexistent_group",
            excluded_ids=set(),
        )

        assert resolved_id is None
        assert "not_found" in reason, f"Reason should indicate group not found: {reason}"

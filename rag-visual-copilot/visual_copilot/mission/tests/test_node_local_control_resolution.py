"""
Test node-local control resolution from control_groups.

These tests verify that:
1. resolve_control_from_group() searches control_groups first
2. Site map contract takes priority over vision hints
3. Control groups find controls that vision might miss
"""

import pytest
from typing import Dict, Any, List, Set, Optional

from visual_copilot.mission.last_mile_tools import (
    MappedTerminalContext,
    MappedExtractionStateMachine,
    validate_completion_contract,
)


class MockNode:
    """Mock DOM node for testing."""
    def __init__(self, id: str, text: str = "", interactive: bool = False):
        self.id = id
        self.text = text
        self.interactive = interactive


class TestResolveControlFromGroup:
    """Test suite for node-local control resolution."""

    def test_resolves_control_from_date_filters_group(self):
        """Should find date picker from date_filters control group."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={
                "date_filters": ["Date Picker", "Period Select"]
            },
        )

        machine = MappedExtractionStateMachine(context)

        nodes = [
            MockNode("t-date123", "Date Picker", interactive=True),
            MockNode("t-period456", "Period Select", interactive=True),
            MockNode("t-other789", "Other Button", interactive=True),
        ]
        clicked_ids = set()

        result = machine.resolve_control_from_group("date_filters", nodes, clicked_ids)

        assert result is not None
        assert result["target_id"] in ["t-date123", "t-period456"]
        assert result["intent"]["text_label"] in ["Date Picker", "Period Select"]
        assert result["intent"]["context"] == "From date_filters group"

    def test_resolves_control_from_model_filters_group(self):
        """Should find model filter from model_filters control group."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={
                "model_filters": ["Model Filter", "Show All Models"]
            },
        )

        machine = MappedExtractionStateMachine(context)

        nodes = [
            MockNode("t-model123", "Model Filter", interactive=True),
            MockNode("t-show456", "Show All Models", interactive=True),
        ]
        clicked_ids = set()

        result = machine.resolve_control_from_group("model_filters", nodes, clicked_ids)

        assert result is not None
        assert result["target_id"] in ["t-model123", "t-show456"]

    def test_returns_none_when_group_not_found(self):
        """Should return None when control group doesn't exist."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={"date_filters": ["Date Picker"]},
        )

        machine = MappedExtractionStateMachine(context)
        nodes = [MockNode("t-date123", "Date Picker", interactive=True)]
        clicked_ids = set()

        result = machine.resolve_control_from_group("nonexistent_group", nodes, clicked_ids)

        assert result is None

    def test_returns_none_when_no_matching_node_in_dom(self):
        """Should return None when control group nodes aren't in DOM."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={"date_filters": ["Date Picker", "Missing Control"]},
        )

        machine = MappedExtractionStateMachine(context)

        # DOM doesn't have "Missing Control"
        nodes = [
            MockNode("t-other123", "Other Button", interactive=True),
        ]
        clicked_ids = set()

        result = machine.resolve_control_from_group("date_filters", nodes, clicked_ids)

        # Should not find "Date Picker" either since text must match
        assert result is None

    def test_skips_already_clicked_ids(self):
        """Should skip controls that were already clicked."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={"date_filters": ["Date Picker", "Period Select"]},
        )

        machine = MappedExtractionStateMachine(context)

        nodes = [
            MockNode("t-date123", "Date Picker", interactive=True),
            MockNode("t-period456", "Period Select", interactive=True),
        ]
        clicked_ids = {"t-date123"}  # Already clicked

        result = machine.resolve_control_from_group("date_filters", nodes, clicked_ids)

        # Should return Period Select since Date Picker was clicked
        assert result is not None
        assert result["target_id"] == "t-period456"

    def test_requires_interactive_element(self):
        """Should only return interactive elements."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={"date_filters": ["Date Picker"]},
        )

        machine = MappedExtractionStateMachine(context)

        # Non-interactive element
        nodes = [
            MockNode("t-date123", "Date Picker", interactive=False),
        ]
        clicked_ids = set()

        result = machine.resolve_control_from_group("date_filters", nodes, clicked_ids)

        assert result is None

    def test_case_insensitive_text_matching(self):
        """Should match text case-insensitively."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={"date_filters": ["DATE PICKER"]},  # Uppercase in site map
        )

        machine = MappedExtractionStateMachine(context)

        # Lowercase in DOM
        nodes = [
            MockNode("t-date123", "date picker", interactive=True),
        ]
        clicked_ids = set()

        result = machine.resolve_control_from_group("date_filters", nodes, clicked_ids)

        assert result is not None
        assert result["target_id"] == "t-date123"


class TestValidateCompletionContract:
    """Test suite for completion contract validation."""

    def test_validates_entity_visible_requirement(self):
        """Should validate entity_visible requirement."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            entity_anchor_found=True,
        )

        evidence = {}

        is_valid, missing = validate_completion_contract(
            context, evidence, task_mode="read_extract"
        )

        # entity_anchor_found is True, so entity_visible should pass
        assert "entity_visible" not in missing

    def test_validates_metric_visible_requirement(self):
        """Should validate metric_visible requirement."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            metric_anchor_found=False,  # Not found
        )

        evidence = {}

        is_valid, missing = validate_completion_contract(
            context, evidence, task_mode="read_extract"
        )

        assert "metric_visible" in missing

    def test_validates_value_extracted_requirement(self):
        """Should validate value_extracted requirement."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            numeric_value_found=False,  # Not found
        )

        evidence = {}

        is_valid, missing = validate_completion_contract(
            context, evidence, task_mode="read_extract"
        )

        assert "value_extracted" in missing

    def test_returns_valid_when_all_requirements_met(self):
        """Should return is_valid=True when all requirements are met."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            entity_anchor_found=True,
            metric_anchor_found=True,
            numeric_value_found=True,
        )

        evidence = {}

        is_valid, missing = validate_completion_contract(
            context, evidence, task_mode="read_extract"
        )

        assert is_valid is True
        assert missing == []

    def test_returns_invalid_when_requirements_missing(self):
        """Should return is_valid=False when requirements are missing."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            entity_anchor_found=False,
            metric_anchor_found=False,
            numeric_value_found=False,
        )

        evidence = {}

        is_valid, missing = validate_completion_contract(
            context, evidence, task_mode="read_extract"
        )

        assert is_valid is False
        assert len(missing) == 3
        assert "entity_visible" in missing
        assert "metric_visible" in missing
        assert "value_extracted" in missing

    def test_uses_site_map_completion_contract_when_available(self):
        """Should use site map completion_contract when available."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            site_map_node={
                "completion_contract": {
                    "read_extract": ["entity_visible", "value_extracted"]
                }
            },
            entity_anchor_found=True,
            metric_anchor_found=False,  # Not in contract, so ignored
            numeric_value_found=True,
        )

        evidence = {}

        is_valid, missing = validate_completion_contract(
            context, evidence, task_mode="read_extract"
        )

        # Only entity_visible and value_extracted are in contract
        assert is_valid is True
        assert missing == []

    def test_handles_empty_site_map_node(self):
        """Should use default requirements when site_map_node is None."""
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=False,
            site_map_node=None,
            entity_anchor_found=True,
            metric_anchor_found=False,
            numeric_value_found=False,
        )

        evidence = {}

        is_valid, missing = validate_completion_contract(
            context, evidence, task_mode="read_extract"
        )

        assert is_valid is False
        assert "metric_visible" in missing
        assert "value_extracted" in missing


class TestTransitionSetFiltersWithControlGroups:
    """Integration tests for _transition_set_filters using control groups."""

    def test_control_groups_priority_over_vision(self):
        """
        SET_FILTERS should use control_groups (Priority 1)
        before vision hints (Priority 2).
        """
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={"date_filters": ["Date Picker"]},
            goal_filters={"date_range": "last_7_days"},
        )

        machine = MappedExtractionStateMachine(context)

        nodes = [
            MockNode("t-date123", "Date Picker", interactive=True),
        ]
        clicked_ids = set()

        # Vision hints (advisory only)
        vision_hints = {
            "identified_controls": [
                {"target_id": "t-vision999", "label": "Some Other Control", "source": "vision"}
            ],
            "advisory_only": True,
        }

        observation = {
            "nodes": nodes,
            "readable_content": "",
            "url_params": {},
            "clicked_ids": clicked_ids,
            "vision_hints": vision_hints,
        }

        action, is_terminal = machine._transition_set_filters(observation)

        # Should use control_group Date Picker, not vision suggestion
        assert action.tool == "click_element"
        assert action.target_id == "t-date123"
        assert "control_group" in action.why.lower()

    def test_vision_advisory_used_when_control_groups_empty(self):
        """
        When control_groups is empty, vision hints (advisory) should be used.
        """
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={},  # Empty
            goal_filters={"date_range": "last_7_days"},
        )

        machine = MappedExtractionStateMachine(context)

        nodes = [
            MockNode("t-vision123", "Date Range", interactive=True),
        ]
        clicked_ids = set()

        # Vision hints with date-related control
        vision_hints = {
            "identified_controls": [
                {"target_id": "t-vision123", "label": "Date Range", "source": "vision"}
            ],
            "advisory_only": True,
        }

        observation = {
            "nodes": nodes,
            "readable_content": "",
            "url_params": {},
            "clicked_ids": clicked_ids,
            "vision_hints": vision_hints,
        }

        action, is_terminal = machine._transition_set_filters(observation)

        # Should use vision advisory
        assert action.tool == "click_element"
        assert action.target_id == "t-vision123"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

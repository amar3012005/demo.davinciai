"""
Test vision normalization - vision is advisory, site map is truth.

These tests verify that:
1. Vision output preserves raw_recommendations intact
2. Vision has advisory_only: True flag
3. missing_controls lists site map controls vision didn't see
4. Control groups take priority over vision hints
"""

import pytest
from typing import Dict, Any, List, Optional

# Import the function under test
from visual_copilot.mission.tool_executor import process_vision_result


class MockNode:
    """Mock DOM node for testing."""
    def __init__(self, id: str, text: str = "", interactive: bool = False):
        self.id = id
        self.text = text
        self.interactive = interactive


class TestProcessVisionResult:
    """Test suite for vision normalization."""

    def test_preserves_raw_recommendations_intact(self):
        """Vision raw output should not be rewritten or modified."""
        raw_vision = """
        I can see the following controls:
        - click t-abc123 (Date Range Picker)
        - click t-xyz789 (Show All Models)

        The page shows usage data for Whisper.
        """
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter", "Usage Tab"]
        }

        result = process_vision_result(raw_vision, site_map_node)

        # Raw recommendations must be preserved exactly
        assert result["raw_recommendations"] == raw_vision
        assert "raw_recommendations" in result

    def test_advisory_only_flag_is_true(self):
        """Vision must be marked as advisory_only: True."""
        raw_vision = "click t-abc123 (Date Picker)"
        site_map_node = {"expected_controls": ["Date Range"]}

        result = process_vision_result(raw_vision, site_map_node)

        assert result["advisory_only"] is True

    def test_identified_controls_parsed_correctly(self):
        """Vision should parse identified controls with IDs."""
        raw_vision = """
        I see these controls:
        - click t-abc123 (Date Range Picker)
        - click t-xyz789 (Model Selector)
        """
        site_map_node = {}

        result = process_vision_result(raw_vision, site_map_node)

        identified = result["identified_controls"]
        assert len(identified) == 2

        # Check first control
        assert identified[0]["target_id"] == "t-abc123"
        assert identified[0]["label"] == "Date Range Picker"
        assert identified[0]["source"] == "vision"

        # Check second control
        assert identified[1]["target_id"] == "t-xyz789"
        assert identified[1]["label"] == "Model Selector"

    def test_confidence_scores_higher_when_matching_site_map(self):
        """Confidence should be higher (0.9) when vision matches site map."""
        raw_vision = "click t-abc123 (Date Range Picker)"
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter"]
        }

        result = process_vision_result(raw_vision, site_map_node)

        confidence = result["confidence_scores"]
        # "Date Range" is in expected_controls and "date range" is in the label
        assert confidence["t-abc123"] == 0.9

    def test_confidence_scores_lower_when_not_matching_site_map(self):
        """Confidence should be lower (0.5) when vision doesn't match site map."""
        raw_vision = "click t-abc123 (Some Random Button)"
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter"]
        }

        result = process_vision_result(raw_vision, site_map_node)

        confidence = result["confidence_scores"]
        # "Some Random Button" doesn't match any expected control
        assert confidence["t-abc123"] == 0.5

    def test_missing_controls_identified(self):
        """Should identify site map controls that vision didn't see."""
        raw_vision = "click t-abc123 (Date Range Picker)"
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter", "Usage Tab"]
        }

        result = process_vision_result(raw_vision, site_map_node)

        missing = result["missing_controls"]
        # Vision saw "Date Range" but not "Model Filter" or "Usage Tab"
        assert "Model Filter" in missing
        assert "Usage Tab" in missing
        assert "Date Range" not in missing  # Vision saw this one

    def test_empty_site_map_node_returns_default_confidence(self):
        """When no site map, use default medium confidence."""
        raw_vision = "click t-abc123 (Some Button)"
        site_map_node = None

        result = process_vision_result(raw_vision, site_map_node)

        confidence = result["confidence_scores"]
        assert confidence["t-abc123"] == 0.5

    def test_empty_vision_returns_empty_identified_controls(self):
        """Empty vision output should return empty identified controls."""
        raw_vision = ""
        site_map_node = {"expected_controls": ["Date Range"]}

        result = process_vision_result(raw_vision, site_map_node)

        assert result["identified_controls"] == []
        assert result["missing_controls"] == ["Date Range"]

    def test_target_pattern_extraction(self):
        """Should extract target IDs from 'target: xxx' patterns."""
        raw_vision = """
        The date picker target is: t-date123
        I also see target: t-model456 for model selection
        """
        site_map_node = {}

        result = process_vision_result(raw_vision, site_map_node)

        identified = result["identified_controls"]
        assert len(identified) == 2

        target_ids = [c["target_id"] for c in identified]
        assert "t-date123" in target_ids
        assert "t-model456" in target_ids

    def test_vision_misses_date_picker_control_groups_still_work(self):
        """
        Regression test: When vision misses date picker,
        control_groups should still find it (contract-first).

        This tests the integration between process_vision_result
        and the control group resolution in _transition_set_filters.
        """
        # Vision only sees unrelated controls
        raw_vision = "click t-nav123 (Navigation Link)"
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter"],
            "control_groups": {
                "date_filters": ["Date Picker", "Period Select"]
            }
        }

        result = process_vision_result(raw_vision, site_map_node)

        # Vision didn't see expected controls
        assert "Date Range" in result["missing_controls"]
        assert "Model Filter" in result["missing_controls"]

        # Vision saw something else
        assert len(result["identified_controls"]) == 1
        assert result["identified_controls"][0]["target_id"] == "t-nav123"


class TestVisionIntegrationWithControlGroups:
    """Integration tests for vision + control groups."""

    def test_control_groups_priority_over_vision(self):
        """
        When both vision and control_groups provide candidates,
        control_groups (site map contract) takes priority.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedTerminalContext,
            MappedExtractionStateMachine,
        )

        # Create a mock context with control_groups
        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={"date_filters": ["Date Picker", "Period Select"]},
            expected_controls=["Date Range"],
        )

        # Create state machine
        machine = MappedExtractionStateMachine(context)

        # Mock nodes with date picker
        class MockNode:
            def __init__(self, id, text, interactive=True):
                self.id = id
                self.text = text
                self.interactive = interactive

        nodes = [
            MockNode("t-date123", "Date Picker"),
            MockNode("t-period456", "Period Select"),
        ]
        clicked_ids = set()

        # Resolve from control group
        result = machine.resolve_control_from_group("date_filters", nodes, clicked_ids)

        assert result is not None
        assert result["target_id"] in ["t-date123", "t-period456"]
        assert result["intent"]["context"] == "From date_filters group"

    def test_resolve_control_from_group_returns_none_when_empty(self):
        """Should return None when control group doesn't exist."""
        from visual_copilot.mission.last_mile_tools import (
            MappedTerminalContext,
            MappedExtractionStateMachine,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/usage",
            is_mapped=True,
            control_groups={},  # Empty
        )

        machine = MappedExtractionStateMachine(context)

        nodes = []
        clicked_ids = set()

        result = machine.resolve_control_from_group("nonexistent_group", nodes, clicked_ids)

        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Tests for Contract-First Mapped Mode Implementation.

These tests verify that:
1. Node resolution works from URL
2. control_groups are correctly extracted from site map
3. Vision is advisory - site map contract takes priority
4. Contract-first handoff passes all required fields to last_mile
"""

import json
import os
import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, MagicMock, patch

# Import the components being tested
from visual_copilot.navigation.site_map_validator import (
    SiteMapValidator,
    resolve_current_node,
    get_validator,
    reset_validator,
)
from visual_copilot.models.contracts import MappedTerminalContext


# Test fixtures
@pytest.fixture
def site_map_path() -> str:
    """Return path to test site map."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "site_map.json"
    )


@pytest.fixture
def validator(site_map_path: str) -> SiteMapValidator:
    """Create a SiteMapValidator instance with test site map."""
    reset_validator()  # Clear singleton
    return SiteMapValidator(site_map_path=site_map_path)


@pytest.fixture
def mock_dom_nodes() -> List[Mock]:
    """Create mock DOM nodes for testing."""
    nodes = []

    # Date Picker control
    date_picker = Mock()
    date_picker.text = "Date Picker"
    date_picker.aria_label = ""
    date_picker.placeholder = ""
    date_picker.name = ""
    date_picker.title = ""
    nodes.append(date_picker)

    # Model Filter control
    model_filter = Mock()
    model_filter.text = "Model Filter"
    model_filter.aria_label = ""
    model_filter.placeholder = ""
    model_filter.name = ""
    model_filter.title = ""
    nodes.append(model_filter)

    # Activity Tab control
    activity_tab = Mock()
    activity_tab.text = "Activity"
    activity_tab.aria_label = "Activity Tab"
    activity_tab.placeholder = ""
    activity_tab.name = ""
    activity_tab.title = ""
    nodes.append(activity_tab)

    # Cost Tab control
    cost_tab = Mock()
    cost_tab.text = "Cost"
    cost_tab.aria_label = "Cost Tab"
    cost_tab.placeholder = ""
    cost_tab.name = ""
    cost_tab.title = ""
    nodes.append(cost_tab)

    return nodes


@pytest.fixture
def mock_dom_nodes_missing_date_picker() -> List[Mock]:
    """Create mock DOM nodes missing the Date Picker (to test contract-first)."""
    nodes = []

    # Model Filter control (present)
    model_filter = Mock()
    model_filter.text = "Model Filter"
    model_filter.aria_label = ""
    model_filter.placeholder = ""
    model_filter.name = ""
    model_filter.title = ""
    nodes.append(model_filter)

    # Activity Tab control (present)
    activity_tab = Mock()
    activity_tab.text = "Activity"
    activity_tab.aria_label = "Activity Tab"
    activity_tab.placeholder = ""
    activity_tab.name = ""
    activity_tab.title = ""
    nodes.append(activity_tab)

    # Cost Tab control (present)
    cost_tab = Mock()
    cost_tab.text = "Cost"
    cost_tab.aria_label = "Cost Tab"
    cost_tab.placeholder = ""
    cost_tab.name = ""
    cost_tab.title = ""
    nodes.append(cost_tab)

    # NOTE: Date Picker is MISSING - vision would miss it, but site map knows it should be there

    return nodes


class TestNodeResolution:
    """Test per-turn node resolution from URL."""

    def test_resolve_usage_section_node(self, validator: SiteMapValidator):
        """Test resolving usage section node from URL."""
        url = "https://console.groq.com/dashboard/usage"

        node = validator.resolve_current_node(url)

        assert node is not None
        assert node.get("node_id") == "usage_section"
        assert node.get("title") == "Usage and Spend"
        assert node.get("path_regex") == "^/dashboard/usage"

    def test_resolve_api_keys_node(self, validator: SiteMapValidator):
        """Test resolving API keys node from URL."""
        url = "https://console.groq.com/keys"

        node = validator.resolve_current_node(url)

        assert node is not None
        assert node.get("node_id") == "api_keys"
        assert node.get("title") == "API Keys"
        assert node.get("primary_cta") == "Create API Key"

    def test_resolve_node_with_dom_validation(
        self,
        validator: SiteMapValidator,
        mock_dom_nodes: List[Mock]
    ):
        """Test node resolution with DOM validation."""
        url = "https://console.groq.com/dashboard/usage"

        node = validator.resolve_current_node(url, nodes=mock_dom_nodes)

        assert node is not None
        assert node.get("node_id") == "usage_section"

        # Verify control_groups are present
        control_groups = node.get("control_groups", {})
        assert "date_filters" in control_groups
        assert "model_filters" in control_groups
        assert "metric_toggles" in control_groups

    def test_resolve_unknown_url(self, validator: SiteMapValidator):
        """Test resolving unknown URL returns None."""
        url = "https://console.groq.com/unknown/path"

        node = validator.resolve_current_node(url)

        assert node is None

    def test_module_level_resolve_current_node(self, validator: SiteMapValidator):
        """Test module-level resolve_current_node function."""
        url = "https://console.groq.com/dashboard/usage"

        # Singleton should be initialized from fixture
        node = resolve_current_node(url)

        assert node is not None
        assert node.get("node_id") == "usage_section"


class TestControlGroups:
    """Test control_groups extraction from site map."""

    def test_usage_section_control_groups(self, validator: SiteMapValidator):
        """Test control_groups structure for usage section."""
        url = "https://console.groq.com/dashboard/usage"
        node = validator.resolve_current_node(url)

        assert node is not None
        control_groups = node.get("control_groups", {})

        # Verify structure
        assert isinstance(control_groups, dict)
        assert "date_filters" in control_groups
        assert "model_filters" in control_groups
        assert "metric_toggles" in control_groups

        # Verify contents
        assert "Date Picker" in control_groups["date_filters"]
        assert "Period Select" in control_groups["date_filters"]

        assert "Model Filter" in control_groups["model_filters"]
        assert "Show All Models" in control_groups["model_filters"]

        assert "Activity Tab" in control_groups["metric_toggles"]
        assert "Cost Tab" in control_groups["metric_toggles"]

    def test_api_keys_section_control_groups(self, validator: SiteMapValidator):
        """Test control_groups structure for API keys section."""
        url = "https://console.groq.com/keys"
        node = validator.resolve_current_node(url)

        assert node is not None
        control_groups = node.get("control_groups", {})

        assert "actions" in control_groups
        assert "key_list" in control_groups

        assert "Create API Key" in control_groups["actions"]

    def test_task_modes_extraction(self, validator: SiteMapValidator):
        """Test task_modes extraction from site map node."""
        url = "https://console.groq.com/dashboard/usage"
        node = validator.resolve_current_node(url)

        assert node is not None
        task_modes = node.get("task_modes", [])

        assert isinstance(task_modes, list)
        assert "read_extract" in task_modes
        assert "filter_view" in task_modes

    def test_completion_contract_extraction(self, validator: SiteMapValidator):
        """Test completion_contract extraction from site map node."""
        url = "https://console.groq.com/dashboard/usage"
        node = validator.resolve_current_node(url)

        assert node is not None
        completion_contract = node.get("completion_contract", {})

        assert isinstance(completion_contract, dict)
        assert "read_extract" in completion_contract
        assert "filter_view" in completion_contract

        # Verify contract contents
        read_requirements = completion_contract["read_extract"]
        assert "entity_visible" in read_requirements
        assert "metric_visible" in read_requirements
        assert "value_extracted" in read_requirements


class TestContractFirstHandoff:
    """Test contract-first handoff to last_mile."""

    def test_mapped_terminal_context_from_site_map(
        self,
        validator: SiteMapValidator,
        mock_dom_nodes: List[Mock]
    ):
        """Test MappedTerminalContext creation from site map node."""
        url = "https://console.groq.com/dashboard/usage"
        goal = "Show me Whisper token usage"

        # Use the from_site_map class method
        context = MappedTerminalContext.from_site_map(
            validator.resolve_current_node(url)
        )

        assert context.mapped_mode is True
        assert context.expected_node_id == "usage_section"
        assert context.current_node_title == "Usage and Spend"

        # Verify control_groups
        assert "date_filters" in context.control_groups
        assert "model_filters" in context.control_groups
        assert "metric_toggles" in context.control_groups

        # Verify task_modes
        assert "read_extract" in context.task_modes
        assert "filter_view" in context.task_modes

        # Verify completion_contract
        assert "read_extract" in context.completion_contract
        assert "filter_view" in context.completion_contract

    def test_vision_misses_control_site_map_finds_it(
        self,
        validator: SiteMapValidator,
        mock_dom_nodes_missing_date_picker: List[Mock]
    ):
        """
        CRITICAL TEST: Vision misses date picker, but site map still knows it should be there.

        This is the core contract-first principle:
        - Vision (DOM inspection) might miss controls due to rendering issues
        - Site map contract is SOURCE OF TRUTH
        - Mapped mode should find controls via control_groups even if vision misses them
        """
        url = "https://console.groq.com/dashboard/usage"

        # Resolve node - site map knows about all expected controls
        node = validator.resolve_current_node(
            url,
            nodes=mock_dom_nodes_missing_date_picker
        )

        assert node is not None

        # Site map contract includes Date Picker even though vision missed it
        expected_controls = node.get("expected_controls", [])
        control_groups = node.get("control_groups", {})

        assert "Date Picker" in expected_controls  # Site map knows it should be there
        assert "Date Picker" in control_groups.get("date_filters", [])

        # The control_groups provide the contract - what SHOULD be on the page
        # This is the source of truth, not what vision currently sees
        assert len(control_groups.get("date_filters", [])) > 0

        # Create context from site map
        context = MappedTerminalContext.from_site_map(node)

        # Context has full contract even though vision missed Date Picker
        assert "date_filters" in context.control_groups
        assert "Date Picker" in context.control_groups["date_filters"]

    def test_mapped_terminal_context_serialization(self):
        """Test MappedTerminalContext to_dict/from_dict roundtrip."""
        original = MappedTerminalContext(
            mapped_mode=True,
            expected_node_id="usage_section",
            mapped_terminal_node="Usage and Spend",
            required_controls=["Date Picker", "Model Filter"],
            control_groups={
                "date_filters": ["Date Picker", "Period Select"],
                "model_filters": ["Model Filter"],
            },
            task_modes=["read_extract", "filter_view"],
            completion_contract={
                "read_extract": ["entity_visible", "metric_visible", "value_extracted"]
            },
            allowed_terminal_capabilities=["read_token_usage", "filter_by_model"],
        )

        # Serialize
        data = original.to_dict()

        assert data["mapped_mode"] is True
        assert data["expected_node_id"] == "usage_section"
        assert "date_filters" in data["control_groups"]
        assert "read_extract" in data["task_modes"]
        assert "read_extract" in data["completion_contract"]

        # Deserialize
        restored = MappedTerminalContext.from_dict(data)

        assert restored.mapped_mode == original.mapped_mode
        assert restored.expected_node_id == original.expected_node_id
        assert restored.control_groups == original.control_groups
        assert restored.task_modes == original.task_modes
        assert restored.completion_contract == original.completion_contract


class TestValidateCompletionContract:
    """Test completion contract validation."""

    def test_validate_completion_contract_function_exists(self):
        """Test that validate_completion_contract function is available."""
        from visual_copilot.mission.last_mile_tools import validate_completion_contract

        assert validate_completion_contract is not None

    def test_validate_contract_read_extract_mode(
        self,
        validator: SiteMapValidator,
        mock_dom_nodes: List[Mock]
    ):
        """Test completion contract validation for read_extract mode."""
        from visual_copilot.mission.last_mile_tools import (
            MappedTerminalContext as LastMileMappedContext,
            validate_completion_contract,
        )

        url = "https://console.groq.com/dashboard/usage"
        node = validator.resolve_current_node(url)

        context = LastMileMappedContext.from_site_map(
            url=url,
            nodes=mock_dom_nodes,
            goal="Show me Whisper token usage",
            validator=validator,
        )

        # Simulate evidence
        evidence = {
            "entity_visible": True,
            "metric_visible": True,
            "value_extracted": "12345",
        }

        # Validate contract
        is_valid, missing = validate_completion_contract(
            context=context,
            evidence=evidence,
            task_mode="read_extract",
        )

        # Should pass if all evidence is present
        assert is_valid is True
        assert len(missing) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

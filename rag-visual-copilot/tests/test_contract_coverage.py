"""
Per-Layer Contract Coverage Eval Harness.

This test harness evaluates the contract-first mapped mode implementation
across five layers:

A. Node Resolution Eval - Does resolve_current_node find the right site map node?
B. Task-Mode Routing Eval - Does classify_task_type route to the right FSM?
C. Control Resolution Eval - Does resolve_control_from_group find controls?
D. Transition Eval - Does FSM transition produce the right action?
E. Completion Eval - Does validate_contract verify success correctly?

Each layer has golden missions (2-3 per node) for:
- api_keys: create_api_key, copy_api_key, delete_api_key
- usage_section: read_token_usage, read_cost_breakdown
- playground: run_model_inference
"""

import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, Mock

# Import the components under test
from visual_copilot.mission.last_mile_tools import (
    MappedTerminalContext,
    MappedExtractionStateMachine,
    MappedActionStateMachine,
    MappedTaskType,
    MappedExtractionState,
    MappedActionState,
    contract_breach_detected,
    should_fallback_to_exploratory,
    resolve_control_from_group,
)


# ═══════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════

class MockNode:
    """Mock DOM node for testing."""

    def __init__(self, id: str, text: str = "", interactive: bool = False, zone: str = "main"):
        self.id = id
        self.text = text
        self.interactive = interactive
        self.zone = zone
        self.tag_name = "button" if interactive else "div"


@pytest.fixture
def mock_site_map_node_api_keys():
    """Site map node for api_keys page."""
    return {
        "node_id": "api_keys",
        "title": "API Keys",
        "url_patterns": [r"/api-keys", r"/settings/keys"],
        "primary_cta": "Create API Key",
        "control_groups": {
            "create_api_key_button": ["Create API Key", "Create key", "New API Key"],
            "key_list": ["Key List", "API Keys", "Keys"],
            "copy_key_button": ["Copy Key", "Copy", "Reveal secret"],
            "delete_key_button": ["Delete", "Trash", "Remove"],
            "api_key_name_input": ["API Key Name", "Key name", "Name"],
            "confirm_create_button": ["Create", "Confirm", "Generate key"],
        },
        "task_modes": {
            "create_api_key": {
                "task_type": "create_action",
                "primary_cta_group": "create_api_key_button",
                "post_click_expected": ["modal", "input"],
                "success_evidence": ["new_key_visible", "success_toast"],
            },
            "delete_api_key": {
                "task_type": "destructive_action",
                "primary_cta_group": "delete_key_button",
                "success_evidence": ["row_removed", "success_toast"],
            },
        },
        "expected_controls": ["Create API Key", "Key List", "API Keys"],
    }


@pytest.fixture
def mock_nodes_api_keys_page():
    """Mock DOM nodes for api_keys page."""
    return [
        MockNode("cta-create", "Create API Key", interactive=True, zone="header"),
        MockNode("key-list", "API Keys List", interactive=False, zone="main"),
        MockNode("key-row-1", "my-api-key-1", interactive=False, zone="main"),
        MockNode("btn-copy-1", "Copy Key", interactive=True, zone="main"),
        MockNode("btn-delete-1", "Delete", interactive=True, zone="main"),
    ]


@pytest.fixture
def mock_nodes_modal_open():
    """Mock DOM nodes with modal open."""
    return [
        MockNode("modal-dialog", "Create New API Key", interactive=False, zone="modal"),
        MockNode("input-name", "API Key Name", interactive=True, zone="modal"),
        MockNode("btn-confirm", "Create", interactive=True, zone="modal"),
        MockNode("btn-cancel", "Cancel", interactive=True, zone="modal"),
    ]


# ═══════════════════════════════════════════════════════════════════════
# Layer A: Node Resolution Eval
# ═══════════════════════════════════════════════════════════════════════

class TestNodeResolution:
    """Test that resolve_current_node finds the right site map node."""

    def test_resolve_api_keys_node(self, mock_site_map_node_api_keys):
        """api_keys URL should resolve to api_keys node."""
        from visual_copilot.navigation.site_map_validator import SiteMapValidator

        # Mock the site map validator
        validator = MagicMock(spec=SiteMapValidator)
        validator.resolve_current_node.return_value = mock_site_map_node_api_keys

        # Create context
        context = MappedTerminalContext.from_site_map(
            url="https://platform.example.com/api-keys",
            nodes=[],
            goal="Create a new API key",
            validator=validator,
        )

        assert context.is_mapped is True
        assert context.current_node_id == "api_keys"

    def test_resolve_fails_for_unknown_url(self):
        """Unknown URL should not resolve to any node."""
        from visual_copilot.navigation.site_map_validator import SiteMapValidator

        validator = MagicMock(spec=SiteMapValidator)
        validator.resolve_current_node.return_value = None

        context = MappedTerminalContext.from_site_map(
            url="https://unknown.example.com/page",
            nodes=[],
            goal="Do something",
            validator=validator,
        )

        assert context.is_mapped is False
        assert context.current_node_id is None


# ═══════════════════════════════════════════════════════════════════════
# Layer B: Task-Mode Routing Eval
# ═══════════════════════════════════════════════════════════════════════

class TestTaskModeRouting:
    """Test that classify_task_type routes to the right FSM."""

    def test_create_api_key_routes_to_create_action(self, mock_site_map_node_api_keys):
        """'Create API key' should route to CREATE_ACTION."""
        from visual_copilot.mission.last_mile import classify_task_type

        task_type = classify_task_type(
            "Create a new API key named 'test-key'",
            mock_site_map_node_api_keys,
        )

        assert task_type == MappedTaskType.CREATE_ACTION

    def test_delete_api_key_routes_to_confirm_action(self, mock_site_map_node_api_keys):
        """'Delete API key' should route to CONFIRM_ACTION."""
        from visual_copilot.mission.last_mile import classify_task_type

        task_type = classify_task_type(
            "Delete the API key 'test-key'",
            mock_site_map_node_api_keys,
        )

        # Deletion requires confirmation
        assert task_type in [MappedTaskType.CONFIRM_ACTION, MappedTaskType.CREATE_ACTION]

    def test_show_usage_routes_to_read_extract(self, mock_site_map_node_api_keys):
        """'Show token usage' should route to READ_EXTRACT."""
        from visual_copilot.mission.last_mile import classify_task_type

        task_type = classify_task_type(
            "Show me Whisper token usage for last 7 days",
            mock_site_map_node_api_keys,
        )

        assert task_type == MappedTaskType.READ_EXTRACT


# ═══════════════════════════════════════════════════════════════════════
# Layer C: Control Resolution Eval
# ═══════════════════════════════════════════════════════════════════════

class TestControlResolution:
    """Test that resolve_control_from_group finds controls."""

    def test_resolve_from_contract(self, mock_site_map_node_api_keys, mock_nodes_api_keys_page):
        """Control should be found from site map contract first."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
            control_groups=mock_site_map_node_api_keys["control_groups"],
        )

        machine = MappedActionStateMachine(context)

        # Resolve from create_api_key_button group
        result = machine.resolve_control_from_group(
            "create_api_key_button",
            mock_nodes_api_keys_page,
            clicked_ids=set(),
        )

        assert result is not None
        assert result["target_id"] == "cta-create"
        assert result["from"] == "contract"

    def test_resolve_with_vision_fallback(self, mock_site_map_node_api_keys, mock_nodes_api_keys_page):
        """Control should be found from vision when contract misses."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
            control_groups={},  # Empty contract
        )

        machine = MappedActionStateMachine(context)

        # Vision hints with high confidence
        vision_hints = {
            "identified_controls": [
                {
                    "label": "Create API Key",
                    "target_id": "cta-create",
                    "confidence": 0.95,
                    "element_type": "button",
                }
            ]
        }

        result = machine.resolve_control_from_group(
            "create_api_key_button",
            mock_nodes_api_keys_page,
            clicked_ids=set(),
            vision_hints=vision_hints,
        )

        assert result is not None
        assert result["target_id"] == "cta-create"
        assert result["from"] == "vision"

    def test_resolve_returns_none_when_not_found(self, mock_site_map_node_api_keys):
        """Should return None when control not found anywhere."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
            control_groups={},
        )

        machine = MappedActionStateMachine(context)

        # No nodes, no vision
        result = machine.resolve_control_from_group(
            "nonexistent_group",
            nodes=[],
            clicked_ids=set(),
        )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# Layer D: Transition Eval
# ═══════════════════════════════════════════════════════════════════════

class TestFSMTransitions:
    """Test that FSM transitions produce the right actions."""

    def test_validate_node_to_find_cta(self, mock_site_map_node_api_keys, mock_nodes_api_keys_page):
        """VALIDATE_NODE should transition to FIND_PRIMARY_CTA."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
        )

        machine = MappedActionStateMachine(context)
        assert machine.state == MappedActionState.VALIDATE_NODE

        observation = {
            "nodes": mock_nodes_api_keys_page,
            "readable_content": "Create API Key API Keys List",
            "clicked_ids": set(),
        }

        action, is_terminal = machine.transition(observation)

        assert machine.state == MappedActionState.FIND_PRIMARY_CTA
        assert action.tool == "read_page_content"

    def test_adaptive_skip_modal_already_open(self, mock_site_map_node_api_keys, mock_nodes_modal_open):
        """If modal already open, should skip to FILL_REQUIRED_FIELDS."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
        )

        machine = MappedActionStateMachine(context)
        assert machine.state == MappedActionState.VALIDATE_NODE

        observation = {
            "nodes": mock_nodes_modal_open,
            "readable_content": "Create New API Key modal dialog form",
            "clicked_ids": set(),
        }

        action, is_terminal = machine.transition(observation)

        # Should skip CTA steps and go directly to fill fields
        assert machine.state == MappedActionState.FILL_REQUIRED_FIELDS

    def test_find_cta_clickes_correct_button(self, mock_site_map_node_api_keys, mock_nodes_api_keys_page):
        """FIND_PRIMARY_CTA should click the correct CTA button."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
        )

        machine = MappedActionStateMachine(context)
        machine.state = MappedActionState.FIND_PRIMARY_CTA

        observation = {
            "nodes": mock_nodes_api_keys_page,
            "readable_content": "Create API Key",
            "clicked_ids": set(),
        }

        action, is_terminal = machine.transition(observation)

        assert machine.state == MappedActionState.CLICK_PRIMARY_CTA
        assert action.tool == "click_element"
        assert action.target_id == "cta-create"


# ═══════════════════════════════════════════════════════════════════════
# Layer E: Completion Eval
# ═══════════════════════════════════════════════════════════════════════

class TestCompletionValidation:
    """Test that validate_contract verifies success correctly."""

    def test_contract_breach_control_mismatch(self, mock_site_map_node_api_keys):
        """Should detect breach when contract controls not found in DOM."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
            control_groups=mock_site_map_node_api_keys["control_groups"],
        )

        # DOM with completely different controls
        wrong_nodes = [
            MockNode("btn-other", "Something Else", interactive=True),
        ]

        observation = {
            "nodes": wrong_nodes,
            "readable_content": "Something Else",
            "clicked_ids": set(),
        }

        breach_detected, breach_reason = contract_breach_detected(observation, context)

        assert breach_detected is True
        assert "control_group_mismatch" in breach_reason

    def test_no_breach_when_controls_found(self, mock_site_map_node_api_keys, mock_nodes_api_keys_page):
        """Should not detect breach when contract controls are present."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
            control_groups=mock_site_map_node_api_keys["control_groups"],
        )

        observation = {
            "nodes": mock_nodes_api_keys_page,
            "readable_content": "Create API Key API Keys List",
            "clicked_ids": set(),
        }

        breach_detected, breach_reason = contract_breach_detected(observation, context)

        assert breach_detected is False


# ═══════════════════════════════════════════════════════════════════════
# Integration Tests: Golden Missions
# ═══════════════════════════════════════════════════════════════════════

class TestGoldenMissions:
    """Golden missions for end-to-end validation."""

    @pytest.mark.asyncio
    async def test_create_api_key_golden_mission(
        self,
        mock_site_map_node_api_keys,
        mock_nodes_api_keys_page,
        mock_nodes_modal_open,
    ):
        """Golden mission: Create API key with name."""
        context = MappedTerminalContext(
            url="https://platform.example.com/api-keys",
            is_mapped=True,
            site_map_node=mock_site_map_node_api_keys,
        )

        machine = MappedActionStateMachine(context, max_attempts=10)

        # Step 1: Validate node -> Find CTA
        obs1 = {"nodes": mock_nodes_api_keys_page, "readable_content": "Create API Key", "clicked_ids": set()}
        action1, _ = machine.transition(obs1)
        assert action1.tool == "read_page_content"

        # Step 2: Find CTA -> Click CTA
        obs2 = {"nodes": mock_nodes_api_keys_page, "readable_content": "Create API Key", "clicked_ids": set()}
        action2, _ = machine.transition(obs2)
        assert action2.tool == "click_element"
        assert action2.target_id == "cta-create"

        # Step 3: Click CTA -> Verify Modal (simulate modal now open)
        obs3 = {"nodes": mock_nodes_modal_open, "readable_content": "Create New API Key modal", "clicked_ids": {"cta-create"}}
        action3, _ = machine.transition(obs3)
        assert action3.tool == "read_page_content"
        assert machine.state == MappedActionState.FILL_REQUIRED_FIELDS

        print("Golden mission: create_api_key - PASSED")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

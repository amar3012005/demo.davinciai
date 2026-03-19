"""
Test for Action FSM - API Key Creation Goal

This test verifies that:
1. "Create API key" goal routes to CREATE_ACTION task type (not READ_EXTRACT)
2. Action FSM follows action workflow: validate → find_cta → click_cta → verify_modal → fill_fields → confirm → success
3. Extraction FSM is still used for "Show me usage" goals
"""

import pytest
from unittest.mock import MagicMock, patch


class TestTaskTypeClassification:
    """Test that user goals are correctly classified into task types."""

    def test_create_api_key_routes_to_create_action(self):
        """
        Test 1: "Create API key" routes to CREATE_ACTION, not READ_EXTRACT.
        """
        from visual_copilot.mission.last_mile import classify_task_type
        from visual_copilot.mission.last_mile_tools import MappedTaskType

        user_goal = "Create API key"
        current_node = {
            "node_id": "api_keys_page",
            "title": "API Keys",
            "primary_cta": "Create new key",
        }

        task_type = classify_task_type(user_goal, current_node)

        assert task_type == MappedTaskType.CREATE_ACTION, \
            f"Expected CREATE_ACTION, got {task_type}"

    def test_show_usage_routes_to_read_extract(self):
        """
        Test 2: "Show me Whisper usage" routes to READ_EXTRACT.
        """
        from visual_copilot.mission.last_mile import classify_task_type
        from visual_copilot.mission.last_mile_tools import MappedTaskType

        user_goal = "Show me Whisper token usage for last 7 days"
        current_node = {
            "node_id": "usage_dashboard",
            "title": "Usage Dashboard",
        }

        task_type = classify_task_type(user_goal, current_node)

        assert task_type == MappedTaskType.READ_EXTRACT, \
            f"Expected READ_EXTRACT, got {task_type}"

    def test_update_profile_routes_to_form_fill(self):
        """
        Test 3: "Update my profile" routes to FORM_FILL.
        """
        from visual_copilot.mission.last_mile import classify_task_type
        from visual_copilot.mission.last_mile_tools import MappedTaskType

        user_goal = "Update my profile information"
        current_node = {
            "node_id": "profile_settings",
            "title": "Profile Settings",
        }

        task_type = classify_task_type(user_goal, current_node)

        assert task_type == MappedTaskType.FORM_FILL, \
            f"Expected FORM_FILL, got {task_type}"

    def test_confirm_delete_routes_to_confirm_action(self):
        """
        Test 4: "Yes, delete this project" routes to CONFIRM_ACTION.
        """
        from visual_copilot.mission.last_mile import classify_task_type
        from visual_copilot.mission.last_mile_tools import MappedTaskType

        user_goal = "Yes, delete this project"
        current_node = {
            "node_id": "delete_confirm",
            "title": "Confirm Deletion",
        }

        task_type = classify_task_type(user_goal, current_node)

        assert task_type == MappedTaskType.CONFIRM_ACTION, \
            f"Expected CONFIRM_ACTION, got {task_type}"

    def test_generate_new_key_routes_to_create_action(self):
        """
        Test 5: "Generate a new secret key" routes to CREATE_ACTION.
        """
        from visual_copilot.mission.last_mile import classify_task_type
        from visual_copilot.mission.last_mile_tools import MappedTaskType

        user_goal = "Generate a new secret key"
        current_node = {}

        task_type = classify_task_type(user_goal, current_node)

        assert task_type == MappedTaskType.CREATE_ACTION, \
            f"Expected CREATE_ACTION, got {task_type}"


class TestActionStateMachine:
    """Test Action FSM state transitions for API key creation."""

    def test_action_fsm_initial_state(self):
        """
        Test 6: Action FSM starts at VALIDATE_NODE state.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            current_node_id="api_keys_page",
            site_map_node={
                "node_id": "api_keys_page",
                "title": "API Keys",
                "primary_cta": "Create new key",
                "required_fields": ["name"],
            },
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)

        assert machine.state == MappedActionState.VALIDATE_NODE, \
            f"Expected VALIDATE_NODE, got {machine.state}"

    def test_action_fsm_validates_node(self):
        """
        Test 7: Action FSM validates node and moves to FIND_PRIMARY_CTA.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            site_map_node={
                "primary_cta": "Create new key",
            },
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)
        observation = {
            "nodes": [],
            "readable_content": "API Keys page",
            "clicked_ids": set(),
            "vision_hints": "",
        }

        action, is_terminal = machine.transition(observation)

        assert machine.state == MappedActionState.FIND_PRIMARY_CTA, \
            f"Expected FIND_PRIMARY_CTA, got {machine.state}"
        assert action.tool == "read_page_content"
        assert action.type == "validate"

    def test_action_fsm_finds_cta_from_site_map(self):
        """
        Test 8: Action FSM finds CTA from site map contract.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            site_map_node={
                "primary_cta": "Create new key",
            },
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)
        machine.state = MappedActionState.FIND_PRIMARY_CTA

        # Mock node with CTA text
        class MockNode:
            def __init__(self, id, text, interactive=True):
                self.id = id
                self.text = text
                self.interactive = interactive

        observation = {
            "nodes": [
                MockNode("t-100", "Your API Keys", interactive=False),
                MockNode("t-101", "Create new key", interactive=True),
                MockNode("t-102", "Documentation", interactive=True),
            ],
            "readable_content": "API Keys - Create new key button visible",
            "clicked_ids": set(),
            "vision_hints": "",
        }

        action, is_terminal = machine.transition(observation)

        assert machine.state == MappedActionState.CLICK_PRIMARY_CTA, \
            f"Expected CLICK_PRIMARY_CTA, got {machine.state}"
        assert action.tool == "click_element"
        assert action.target_id == "t-101"
        assert action.intent["text_label"] == "Create new key"

    def test_action_fsm_clicks_cta_and_verifies_modal(self):
        """
        Test 9: After clicking CTA, FSM waits and verifies modal appeared.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            site_map_node={"primary_cta": "Create new key"},
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)
        machine.state = MappedActionState.CLICK_PRIMARY_CTA

        observation = {
            "nodes": [],
            "readable_content": "Creating new key...",
            "clicked_ids": {"t-101"},
            "vision_hints": "",
        }

        action, is_terminal = machine.transition(observation)

        assert machine.state == MappedActionState.VERIFY_MODAL_OR_FORM, \
            f"Expected VERIFY_MODAL_OR_FORM, got {machine.state}"
        assert action.tool == "wait_for_ui"

    def test_action_fsm_verifies_modal_and_fills_fields(self):
        """
        Test 10: Action FSM verifies modal and transitions to fill fields.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            site_map_node={
                "primary_cta": "Create new key",
                "required_fields": ["name"],
            },
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)
        machine.state = MappedActionState.VERIFY_MODAL_OR_FORM

        observation = {
            "nodes": [],
            "readable_content": "Create New API Key - Enter key name to continue",
            "clicked_ids": {"t-101"},
            "vision_hints": "",
        }

        action, is_terminal = machine.transition(observation)

        assert machine.state == MappedActionState.FILL_REQUIRED_FIELDS, \
            f"Expected FILL_REQUIRED_FIELDS, got {machine.state}"

    def test_action_fsm_fills_required_fields(self):
        """
        Test 11: Action FSM fills required fields from site map.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            site_map_node={
                "primary_cta": "Create new key",
                "required_fields": ["name"],
            },
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)
        machine.state = MappedActionState.FILL_REQUIRED_FIELDS

        class MockNode:
            def __init__(self, id, text, interactive=True, tag_name="input"):
                self.id = id
                self.text = text
                self.interactive = interactive
                self.tag_name = tag_name

        observation = {
            "nodes": [
                MockNode("t-200", "Key Name", interactive=True, tag_name="input"),
                MockNode("t-201", "Create", interactive=True, tag_name="button"),
            ],
            "readable_content": "Create New Key - Key Name field visible",
            "clicked_ids": {"t-101"},
            "vision_hints": "",
        }

        action, is_terminal = machine.transition(observation)

        assert action.tool == "type_text"
        assert action.target_id == "t-200"
        assert "name" in machine.filled_fields

    def test_action_fsm_clicks_confirm_after_filling(self):
        """
        Test 12: After filling fields, FSM clicks confirm button.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            site_map_node={
                "primary_cta": "Create new key",
                "required_fields": ["name"],
            },
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)
        machine.state = MappedActionState.FILL_REQUIRED_FIELDS
        machine.filled_fields = {"name"}

        class MockNode:
            def __init__(self, id, text, interactive=True):
                self.id = id
                self.text = text
                self.interactive = interactive

        observation = {
            "nodes": [
                MockNode("t-201", "Create", interactive=True),
                MockNode("t-202", "Cancel", interactive=True),
            ],
            "readable_content": "All fields filled",
            "clicked_ids": {"t-101"},
            "vision_hints": "",
        }

        action, is_terminal = machine.transition(observation)

        assert machine.state == MappedActionState.VERIFY_SUCCESS, \
            f"Expected VERIFY_SUCCESS, got {machine.state}"
        assert action.tool == "click_element"
        assert action.target_id == "t-201"

    def test_action_fsm_verifies_success(self):
        """
        Test 13: Action FSM verifies success message and completes.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            site_map_node={"primary_cta": "Create new key"},
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)
        machine.state = MappedActionState.VERIFY_SUCCESS

        observation = {
            "nodes": [],
            "readable_content": "Success! API key created successfully",
            "clicked_ids": {"t-101", "t-201"},
            "vision_hints": "",
        }

        action, is_terminal = machine.transition(observation)

        assert machine.state == MappedActionState.COMPLETE, \
            f"Expected COMPLETE, got {machine.state}"

    def test_action_fsm_completes(self):
        """
        Test 14: Action FSM returns complete action with is_terminal=True.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedActionStateMachine,
            MappedActionState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/api-keys",
            site_map_node={"primary_cta": "Create new key"},
            is_mapped=True,
        )

        machine = MappedActionStateMachine(context)
        machine.state = MappedActionState.COMPLETE

        observation = {
            "nodes": [],
            "readable_content": "Success!",
            "clicked_ids": set(),
        }

        action, is_terminal = machine.transition(observation)

        assert action.type == "complete"
        assert action.tool == "answer"
        assert is_terminal is True


class TestMappedLastMileRouting:
    """Test that _run_mapped_last_mile routes to correct FSM."""

    @pytest.mark.asyncio
    async def test_create_api_key_routes_to_action_fsm(self):
        """
        Test 15: "Create API key" goal routes to Action FSM.
        """
        from visual_copilot.mission.last_mile import classify_task_type
        from visual_copilot.mission.last_mile_tools import MappedTaskType

        # Verify classification
        task_type = classify_task_type("Create API key", {})
        assert task_type == MappedTaskType.CREATE_ACTION

    @pytest.mark.asyncio
    async def test_show_usage_routes_to_extract_fsm(self):
        """
        Test 16: "Show me usage" goal routes to Read/Extract FSM.
        """
        from visual_copilot.mission.last_mile import classify_task_type
        from visual_copilot.mission.last_mile_tools import MappedTaskType

        # Verify classification
        task_type = classify_task_type("Show me Whisper usage", {})
        assert task_type == MappedTaskType.READ_EXTRACT


class TestFormFillStateMachine:
    """Test Form Fill FSM state transitions."""

    def test_form_fill_fsm_initial_state(self):
        """
        Test 17: Form Fill FSM starts at VALIDATE_NODE state.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedFormFillStateMachine,
            MappedFormFillState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/settings/profile",
            site_map_node={
                "node_id": "profile_settings",
                "title": "Profile Settings",
                "required_fields": ["name", "email"],
            },
            is_mapped=True,
        )

        machine = MappedFormFillStateMachine(context)

        assert machine.state == MappedFormFillState.VALIDATE_NODE, \
            f"Expected VALIDATE_NODE, got {machine.state}"

    def test_form_fill_fsm_locates_form(self):
        """
        Test 18: Form Fill FSM locates form and moves to FILL_FIELD.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedFormFillStateMachine,
            MappedFormFillState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/settings/profile",
            site_map_node={"required_fields": ["name"]},
            is_mapped=True,
        )

        machine = MappedFormFillStateMachine(context)

        class MockNode:
            def __init__(self, id, text, interactive=True, tag_name="div"):
                self.id = id
                self.text = text
                self.interactive = interactive
                self.tag_name = tag_name

        observation = {
            "nodes": [
                MockNode("form-1", "Profile Settings Form", interactive=True, tag_name="form"),
            ],
            "readable_content": "Profile Settings - Update your information",
            "clicked_ids": set(),
        }

        action, is_terminal = machine.transition(observation)

        # Should validate node first, then locate form
        assert machine.state in [MappedFormFillState.LOCATE_FORM, MappedFormFillState.FILL_FIELD], \
            f"Expected form location state, got {machine.state}"

    def test_form_fill_fsm_fills_fields(self):
        """
        Test 19: Form Fill FSM fills required fields.
        """
        from visual_copilot.mission.last_mile_tools import (
            MappedFormFillStateMachine,
            MappedFormFillState,
            MappedTerminalContext,
        )

        context = MappedTerminalContext(
            url="https://platform.openai.com/settings/profile",
            site_map_node={"required_fields": ["name"]},
            is_mapped=True,
        )

        machine = MappedFormFillStateMachine(context)
        machine.state = MappedFormFillState.FILL_FIELD

        class MockNode:
            def __init__(self, id, text, interactive=True, tag_name="input"):
                self.id = id
                self.text = text
                self.interactive = interactive
                self.tag_name = tag_name

        observation = {
            "nodes": [
                MockNode("input-name", "Name", interactive=True, tag_name="input"),
            ],
            "readable_content": "Profile form with name field",
            "clicked_ids": set(),
        }

        action, is_terminal = machine.transition(observation)

        assert action.tool == "type_text"
        assert "name" in machine.filled_fields


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

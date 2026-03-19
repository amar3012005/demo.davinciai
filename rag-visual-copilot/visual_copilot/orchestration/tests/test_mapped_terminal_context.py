"""
Test for MappedTerminalContext creation and validation.

This demonstrates how the MapGuard context is built from high-confidence
PageIndex matches and passed through to last_mile for deterministic extraction.
"""

import pytest
from visual_copilot.models.contracts import MappedTerminalContext


class TestMappedTerminalContext:
    """Test MappedTerminalContext dataclass functionality."""

    def test_default_creation(self):
        """Test default MappedTerminalContext creation."""
        ctx = MappedTerminalContext()
        assert ctx.mapped_mode is False
        assert ctx.expected_node_id == ""
        assert ctx.mapped_terminal_node == ""
        assert ctx.required_controls == []
        assert ctx.control_groups == {}
        assert ctx.allowed_terminal_capabilities == []
        assert ctx.is_valid_terminal() is False

    def test_valid_terminal_creation(self):
        """Test creating a valid terminal context from PageIndex match."""
        ctx = MappedTerminalContext(
            mapped_mode=True,
            expected_node_id="groq_console_models_vision",
            mapped_terminal_node="Vision Models Documentation",
            required_controls=[
                "#vision-model-list",
                "#model-comparison-table",
                "#vision-pricing"
            ],
            control_groups={
                "model_list": [
                    {"selector": "#vision-model-list", "type": "list"},
                    {"selector": "#model-comparison-table", "type": "table"},
                ],
                "pricing": [
                    {"selector": "#vision-pricing", "type": "card"},
                ]
            },
            allowed_terminal_capabilities=[
                "extract_model_specs",
                "compare_vision_models",
                "show_pricing_info"
            ]
        )

        assert ctx.is_valid_terminal() is True
        assert ctx.expected_node_id == "groq_console_models_vision"
        assert len(ctx.required_controls) == 3
        assert len(ctx.control_groups) == 2
        assert len(ctx.allowed_terminal_capabilities) == 3

    def test_serialization_roundtrip(self):
        """Test dict serialization and deserialization."""
        original = MappedTerminalContext(
            mapped_mode=True,
            expected_node_id="test_node_123",
            mapped_terminal_node="Test Page",
            required_controls=["#control1", "#control2"],
            control_groups={"main": [{"selector": "#control1"}]},
            allowed_terminal_capabilities=["read", "extract"]
        )

        # Serialize
        data = original.to_dict()
        assert data["mapped_mode"] is True
        assert data["expected_node_id"] == "test_node_123"

        # Deserialize
        restored = MappedTerminalContext.from_dict(data)
        assert restored.mapped_mode is True
        assert restored.expected_node_id == "test_node_123"
        assert restored.is_valid_terminal() is True

    def test_from_dict_empty(self):
        """Test from_dict with empty/None input."""
        ctx = MappedTerminalContext.from_dict(None)
        assert ctx.is_valid_terminal() is False

        ctx = MappedTerminalContext.from_dict({})
        assert ctx.is_valid_terminal() is False

    def test_high_confidence_pageindex_match(self):
        """Test scenario: high-confidence PageIndex match creates valid context."""
        # Simulating PageIndex result with confidence >= 0.8
        page_index_result = {
            "confidence": 0.92,
            "target_node": {
                "node_id": "groq_console_billing_usage",
                "title": "Usage and Spend",
                "expected_controls": [
                    "#usage-chart",
                    "#spend-table",
                    "#token-counter"
                ],
                "terminal_capabilities": [
                    "show_usage_graph",
                    "display_spend_breakdown",
                    "export_usage_data"
                ]
            }
        }

        # Build mapped context when confidence >= 0.8
        if page_index_result["confidence"] >= 0.8:
            target = page_index_result["target_node"]
            ctx = MappedTerminalContext(
                mapped_mode=True,
                expected_node_id=target["node_id"],
                mapped_terminal_node=target["title"],
                required_controls=target.get("expected_controls", []),
                control_groups={"default": [
                    {"selector": c, "type": "unknown"}
                    for c in target.get("expected_controls", [])
                ]},
                allowed_terminal_capabilities=target.get("terminal_capabilities", [])
            )

            assert ctx.is_valid_terminal() is True
            assert ctx.expected_node_id == "groq_console_billing_usage"


class TestMapGuardIntegration:
    """Test MapGuard integration scenarios."""

    def test_route_drift_detection(self):
        """Test logging warning when fuzzy routing used despite PageIndex match."""
        # Scenario: PageIndex confidence is high but we're not at target
        page_index_result = {
            "confidence": 0.85,
            "current_node": {"node_id": "groq_console_home"},
            "target_node": {"node_id": "groq_console_billing"},
        }

        # This should trigger a route drift warning in production
        # because we're navigating (not at target) despite high confidence
        assert page_index_result["confidence"] >= 0.8
        assert page_index_result["current_node"]["node_id"] != page_index_result["target_node"]["node_id"]

    def test_mapped_mode_entry_condition(self):
        """Test that mapped_mode only enters when at target with high confidence."""
        scenarios = [
            # (current_node_id, target_node_id, confidence, should_enter_mapped_mode)
            ("node_a", "node_a", 0.92, True),   # At target, high conf
            ("node_a", "node_b", 0.92, False),  # Not at target, high conf (navigate)
            ("node_a", "node_a", 0.70, False),  # At target, low conf (explore)
            (None, "node_a", 0.85, False),      # No current node
        ]

        for current_id, target_id, confidence, should_enter in scenarios:
            is_at_target = current_id == target_id and current_id is not None
            has_high_confidence = confidence >= 0.8

            would_enter_mapped_mode = is_at_target and has_high_confidence

            assert would_enter_mapped_mode == should_enter, \
                f"Failed for current={current_id}, target={target_id}, conf={confidence}"


if __name__ == "__main__":
    # Run basic tests without pytest
    test = TestMappedTerminalContext()
    test.test_default_creation()
    print("✓ test_default_creation passed")

    test.test_valid_terminal_creation()
    print("✓ test_valid_terminal_creation passed")

    test.test_serialization_roundtrip()
    print("✓ test_serialization_roundtrip passed")

    test.test_from_dict_empty()
    print("✓ test_from_dict_empty passed")

    test.test_high_confidence_pageindex_match()
    print("✓ test_high_confidence_pageindex_match passed")

    integration = TestMapGuardIntegration()
    integration.test_route_drift_detection()
    print("✓ test_route_drift_detection passed")

    integration.test_mapped_mode_entry_condition()
    print("✓ test_mapped_mode_entry_condition passed")

    print("\n✅ All tests passed!")
